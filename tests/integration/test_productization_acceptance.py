from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from hikbox_pictures.product.config import WorkspaceLayout, initialize_workspace
from hikbox_pictures.product.people.repository import PeopleRepository
from hikbox_pictures.product.people.service import PeopleService
from hikbox_pictures.product.service_registry import build_service_container
from hikbox_pictures.product.source.repository import SourceRepository
from hikbox_pictures.product.source.service import SourceService
from hikbox_pictures.web.app import create_app
from tests.integration import test_scan_behavior_parity_with_face_review_pipeline as parity_baseline
from tests.product.task6_test_support import create_task6_workspace, seed_face_observations


REPO_ROOT = Path(__file__).resolve().parents[2]
DB_SCHEMA_DOC = REPO_ROOT / "docs" / "db_schema.md"
LIVE_EXAMPLE_DIR = REPO_ROOT / "tests" / "data" / "live-example"
REAL_E2E_RAW_DIR = REPO_ROOT / "tests" / "data" / "e2e-face-input" / "raw"

ACCEPTANCE_CASE_MATRIX: tuple[dict[str, str], ...] = (
    {"ac": "AC01", "test": "test_ac01_db_schema_constraints_from_sqlite_pragma", "source": "DB/真实表结构", "spec": "§17-01"},
    {"ac": "AC02", "test": "test_ac02_artifact_layout_on_filesystem", "source": "文件系统/真实产物目录", "spec": "§17-02"},
    {"ac": "AC03", "test": "test_ac03_detect_defaults_persisted_in_db", "source": "DB/scan_batch + param_snapshot_json", "spec": "§17-03"},
    {"ac": "AC04", "test": "test_ac04_stage_execution_modes", "source": "DB/scan_session_source + scan_batch_item", "spec": "§17-04"},
    {"ac": "AC05", "test": "test_ac05_embeddings_written_to_embedding_db", "source": "embedding.db/真实向量落库", "spec": "§17-05"},
    {"ac": "AC06", "test": "test_ac06_person_uuid_and_merge_tie_break_rule", "source": "CLI + DB/merge_operation", "spec": "§17-06"},
    {"ac": "AC07", "test": "test_ac07_assignment_source_and_noise_rules_from_db", "source": "DB/person_face_assignment", "spec": "§17-07"},
    {"ac": "AC08", "test": "test_ac08_active_assignment_uniqueness", "source": "DB/active assignment 唯一性", "spec": "§17-08"},
    {"ac": "AC09", "test": "test_ac09_assignment_run_snapshot_from_db", "source": "DB/assignment_run", "spec": "§17-09"},
    {"ac": "AC10", "test": "test_ac10_param_snapshot_full_frozen_params", "source": "DB/冻结参数快照", "spec": "§17-10"},
    {"ac": "AC11", "test": "test_ac11_scan_main_chain_uses_frozen_v5_runtime", "source": "CLI + DB + parity 基线统计", "spec": "§17-11"},
    {"ac": "AC12", "test": "test_ac12_live_photo_pairing_written_in_metadata", "source": "DB/photo_asset.live_mov_*", "spec": "§17-12"},
    {"ac": "AC13", "test": "test_ac13_homepage_named_anonymous_sections_without_search", "source": "API/TestClient 页面响应", "spec": "§17-13"},
    {"ac": "AC14", "test": "test_ac14_nav_items_removed", "source": "API/TestClient 路由与页面文本", "spec": "§17-14"},
    {"ac": "AC15", "test": "test_ac15_exclusion_reassign_happens_in_next_scan", "source": "CLI + API + DB/排除后下一轮扫描", "spec": "§17-15"},
    {"ac": "AC16", "test": "test_ac16_homepage_has_merge_and_undo_last_merge_actions", "source": "API/TestClient 页面动作入口", "spec": "§17-16"},
    {"ac": "AC17", "test": "test_ac17_merge_and_undo_restore_exclusion_delta", "source": "CLI + DB/merge_operation_exclusion_delta", "spec": "§17-17"},
    {"ac": "AC18", "test": "test_ac18_export_run_layout_and_collision", "source": "CLI + 文件系统 + DB/export_delivery", "spec": "§17-18"},
    {"ac": "AC19", "test": "test_ac19_export_template_delete_not_exposed_in_api_or_cli", "source": "CLI help + API route 面", "spec": "§17-19"},
    {"ac": "AC20", "test": "test_ac20_audit_items_three_types_and_jump_targets", "source": "API + DB + 页面跳转", "spec": "§17-20"},
    {"ac": "AC21", "test": "test_ac21_cli_lock_and_conflict_codes", "source": "CLI returncode + stderr/stdout", "spec": "§17-21"},
    {"ac": "AC22", "test": "test_ac22_db_schema_doc_migration_text", "source": "文档文本/docs/db_schema.md", "spec": "§17-22"},
)

@pytest.fixture(scope="session")
def python_bin() -> Path:
    for candidate_root in [REPO_ROOT, *REPO_ROOT.parents]:
        candidate = candidate_root / ".venv" / "bin" / "python"
        if candidate.exists():
            return candidate
    raise AssertionError(f"找不到 Python 可执行文件: {REPO_ROOT / '.venv' / 'bin' / 'python'}")


@pytest.fixture()
def runtime_workspace(tmp_path: Path) -> dict[str, object]:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    source_root = tmp_path / "photos"
    _copy_acceptance_real_dataset(source_root)
    layout = _build_workspace_with_source(
        workspace_root=workspace_root,
        external_root=external_root,
        source_root=source_root,
        label="real-acceptance",
    )
    client = TestClient(create_app(build_service_container(layout)))
    try:
        yield {
            "workspace_root": workspace_root,
            "source_root": source_root,
            "layout": layout,
            "client": client,
        }
    finally:
        client.close()


@pytest.fixture(scope="module")
def scanned_runtime_workspace(tmp_path_factory: pytest.TempPathFactory, python_bin: Path) -> dict[str, object]:
    workspace_root = tmp_path_factory.mktemp("acceptance-real-workspace") / "workspace"
    external_root = tmp_path_factory.mktemp("acceptance-real-external")
    source_root = tmp_path_factory.mktemp("acceptance-real-source")
    _copy_acceptance_real_dataset(source_root)
    layout = _build_workspace_with_source(
        workspace_root=workspace_root,
        external_root=external_root,
        source_root=source_root,
        label="real-acceptance",
    )
    result = _run_cli_scan(
        python_bin,
        workspace_root=layout.workspace_root,
        args=["--json", "scan", "start-new", "--workspace", str(layout.workspace_root)],
    )
    assert result.returncode == 0, result.stderr
    payload = _read_cli_json_output(result.stdout)
    return {
        "workspace_root": layout.workspace_root,
        "source_root": source_root,
        "layout": layout,
        "scan_result": result,
        "scan_payload": payload,
    }


@pytest.fixture()
def scanned_live_runtime_workspace(tmp_path: Path, python_bin: Path) -> dict[str, object]:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    source_root = tmp_path / "live-photos"
    source_root.mkdir(parents=True, exist_ok=True)
    if LIVE_EXAMPLE_DIR.exists():
        shutil.copy2(LIVE_EXAMPLE_DIR / "IMG_6576.HEIC", source_root / "IMG_6576.HEIC")
        shutil.copy2(LIVE_EXAMPLE_DIR / ".IMG_6576_1771856408444916.MOV", source_root / ".IMG_6576_1771856408444916.MOV")
    layout = _build_workspace_with_source(
        workspace_root=workspace_root,
        external_root=external_root,
        source_root=source_root,
        label="live-acceptance",
    )
    result = _run_cli_scan(
        python_bin,
        workspace_root=layout.workspace_root,
        args=["--json", "scan", "start-new", "--workspace", str(layout.workspace_root)],
    )
    assert result.returncode == 0, result.stderr
    payload = _read_cli_json_output(result.stdout)
    return {
        "workspace_root": workspace_root,
        "source_root": source_root,
        "layout": layout,
        "scan_result": result,
        "scan_payload": payload,
    }


@pytest.fixture()
def seeded_ui_env(tmp_path: Path) -> dict[str, object]:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    face_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (210, 180, 160)},
            {"asset_index": 0, "color": (220, 190, 170)},
            {"asset_index": 1, "color": (180, 180, 210)},
            {"asset_index": 1, "color": (170, 170, 220), "pending_reassign": True},
        ],
    )
    seeded = _seed_ui_data(layout, session_id=session_id, face_ids=face_ids)
    _execute_sql(
        layout.library_db,
        "UPDATE scan_session SET status='completed', finished_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (session_id,),
    )
    client = TestClient(create_app(build_service_container(layout)))
    try:
        yield {
            "layout": layout,
            "workspace_root": layout.workspace_root,
            "client": client,
            "seeded": seeded,
        }
    finally:
        client.close()


def test_ac01_db_schema_constraints_from_sqlite_pragma(runtime_workspace) -> None:
    layout = runtime_workspace["layout"]
    library_tables = _list_sqlite_tables(layout.library_db)
    embedding_tables = _list_sqlite_tables(layout.embedding_db)
    face_observation_columns = _pragma_table_info(layout.library_db, "face_observation")
    assignment_run_foreign_keys = _pragma_foreign_key_list(layout.library_db, "assignment_run")
    export_delivery_indexes = _pragma_index_list(layout.library_db, "export_delivery")
    export_delivery_unique = next(
        index for index in export_delivery_indexes if str(index["name"]) == "sqlite_autoindex_export_delivery_1"
    )
    export_delivery_unique_columns = _pragma_index_columns(layout.library_db, str(export_delivery_unique["name"]))

    assert layout.library_db.exists()
    assert layout.embedding_db.exists()
    assert {"scan_session", "assignment_run", "export_template", "scan_audit_item"}.issubset(library_tables)
    assert {"face_embedding"}.issubset(embedding_tables)
    assert face_observation_columns["photo_asset_id"]["notnull"] == 1
    assert face_observation_columns["crop_relpath"]["notnull"] == 1
    assert any(
        fk["from"] == "scan_session_id" and fk["table"] == "scan_session" and fk["to"] == "id"
        for fk in assignment_run_foreign_keys
    )
    assert int(export_delivery_unique["unique"]) == 1
    assert [column["name"] for column in export_delivery_unique_columns] == [
        "export_run_id",
        "media_kind",
        "destination_path",
    ]


def test_ac02_artifact_layout_on_filesystem(scanned_runtime_workspace) -> None:
    layout = scanned_runtime_workspace["layout"]
    result = scanned_runtime_workspace["scan_result"]
    payload = scanned_runtime_workspace["scan_payload"]
    runtime_root = layout.workspace_root

    assert result.returncode == 0, result.stderr
    assert payload["data"]["status"] == "completed"
    assert (runtime_root / "artifacts" / "crops").exists()
    assert (runtime_root / "artifacts" / "aligned").exists()
    assert (runtime_root / "artifacts" / "context").exists()
    assert not (runtime_root / "artifacts" / "thumbs").exists()
    assert not (runtime_root / "artifacts" / "ann").exists()
    assert list((runtime_root / "artifacts" / "aligned").glob("*.png"))


def test_ac03_detect_defaults_persisted_in_db(scanned_runtime_workspace) -> None:
    layout = scanned_runtime_workspace["layout"]
    result = scanned_runtime_workspace["scan_result"]
    payload = scanned_runtime_workspace["scan_payload"]
    row = _fetchone(
        layout.library_db,
        """
        SELECT param_snapshot_json
        FROM assignment_run
        WHERE scan_session_id=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (payload["data"]["session_id"],),
    )
    snapshot = json.loads(str(row[0]))
    detect_batch_count = int(
        _fetchone(
            layout.library_db,
            "SELECT COUNT(*) FROM scan_batch WHERE scan_session_id=? AND stage='detect'",
            (payload["data"]["session_id"],),
        )[0]
    )
    max_worker_slot = int(
        _fetchone(
            layout.library_db,
            "SELECT COALESCE(MAX(worker_slot), -1) FROM scan_batch WHERE scan_session_id=? AND stage='detect'",
            (payload["data"]["session_id"],),
        )[0]
    )

    assert result.returncode == 0, result.stderr
    assert snapshot["det_size"] == 640
    assert snapshot["preview_max_side"] == 480
    assert detect_batch_count > 0
    assert 0 <= max_worker_slot <= 3


def test_ac04_stage_execution_modes(scanned_runtime_workspace) -> None:
    layout = scanned_runtime_workspace["layout"]
    result = scanned_runtime_workspace["scan_result"]
    payload = scanned_runtime_workspace["scan_payload"]
    stage_rows = _fetchall(
        layout.library_db,
        "SELECT stage_status_json FROM scan_session_source WHERE scan_session_id=? ORDER BY id ASC",
        (payload["data"]["session_id"],),
    )
    batch_statuses = _fetchall(
        layout.library_db,
        "SELECT DISTINCT status FROM scan_batch WHERE scan_session_id=? ORDER BY status ASC",
        (payload["data"]["session_id"],),
    )
    item_statuses = _fetchall(
        layout.library_db,
        """
        SELECT DISTINCT i.status
        FROM scan_batch_item AS i
        INNER JOIN scan_batch AS b ON b.id=i.scan_batch_id
        WHERE b.scan_session_id=?
        ORDER BY i.status ASC
        """,
        (payload["data"]["session_id"],),
    )

    assert result.returncode == 0, result.stderr
    assert stage_rows
    for row in stage_rows:
        status = json.loads(str(row[0]))
        assert status == {
            "assignment": "done",
            "cluster": "done",
            "detect": "done",
            "discover": "done",
            "embed": "done",
            "metadata": "done",
        }
    assert {str(row[0]) for row in batch_statuses} == {"acked"}
    assert {str(row[0]) for row in item_statuses} == {"done"}


def test_ac05_embeddings_written_to_embedding_db(scanned_runtime_workspace) -> None:
    layout = scanned_runtime_workspace["layout"]
    result = scanned_runtime_workspace["scan_result"]
    assert result.returncode == 0, result.stderr

    emb_conn = sqlite3.connect(layout.embedding_db)
    try:
        rows = emb_conn.execute(
            """
            SELECT face_observation_id, variant, dim, dtype
            FROM face_embedding
            ORDER BY face_observation_id ASC, variant ASC
            """
        ).fetchall()
    finally:
        emb_conn.close()

    variants = {(int(row[0]), str(row[1])) for row in rows}
    assert rows
    assert all(int(row[2]) == 512 for row in rows)
    assert all(str(row[3]) == "float32" for row in rows)
    assert any(variant == "main" for _, variant in variants)
    assert any(variant == "flip" for _, variant in variants)


def test_ac06_person_uuid_and_merge_tie_break_rule(seeded_ui_env, python_bin: Path) -> None:
    layout = seeded_ui_env["layout"]
    seeded = seeded_ui_env["seeded"]
    selected = [seeded["merge_loser_person_id"], seeded["merge_winner_person_id"]]
    result = _run_plain_cli(
        python_bin,
        [
            "--json",
            "people",
            "merge",
            "--selected-person-ids",
            ",".join(str(item) for item in selected),
            "--workspace",
            str(layout.workspace_root),
        ],
    )
    payload = _read_cli_json_output(result.stdout)
    merge_row = _fetchone(
        layout.library_db,
        """
        SELECT winner_person_id, winner_person_uuid, status
        FROM merge_operation
        WHERE id=?
        """,
        (payload["data"]["merge_operation_id"],),
    )

    assert result.returncode == 0, result.stderr
    assert payload["data"]["winner_person_id"] == selected[0]
    assert merge_row[0] == selected[0]
    assert isinstance(merge_row[1], str) and len(str(merge_row[1])) >= 16
    assert merge_row[2] == "applied"


def test_ac07_assignment_source_and_noise_rules_from_db(scanned_runtime_workspace) -> None:
    layout = scanned_runtime_workspace["layout"]
    result = scanned_runtime_workspace["scan_result"]
    assert result.returncode == 0, result.stderr

    rows = _fetchall(
        layout.library_db,
        "SELECT DISTINCT assignment_source FROM person_face_assignment ORDER BY assignment_source ASC",
    )
    sources = {str(row[0]) for row in rows}
    assert sources
    assert sources.issubset({"hdbscan", "person_consensus", "merge", "undo"})
    assert "noise" not in sources
    assert "low_quality_ignored" not in sources


def test_ac08_active_assignment_uniqueness(scanned_runtime_workspace) -> None:
    layout = scanned_runtime_workspace["layout"]
    result = scanned_runtime_workspace["scan_result"]
    assert result.returncode == 0, result.stderr

    duplicates = _fetchall(
        layout.library_db,
        """
        SELECT face_observation_id, COUNT(*)
        FROM person_face_assignment
        WHERE active=1
        GROUP BY face_observation_id
        HAVING COUNT(*) > 1
        """,
    )
    assert duplicates == []


def test_ac09_assignment_run_snapshot_from_db(scanned_runtime_workspace) -> None:
    layout = scanned_runtime_workspace["layout"]
    result = scanned_runtime_workspace["scan_result"]
    payload = scanned_runtime_workspace["scan_payload"]
    row = _fetchone(
        layout.library_db,
        """
        SELECT algorithm_version, run_kind, status, started_at, finished_at
        FROM assignment_run
        WHERE scan_session_id=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (payload["data"]["session_id"],),
    )

    assert result.returncode == 0, result.stderr
    assert row == ("frozen_v5", "scan_full", "completed", row[3], row[4])
    assert row[3] is not None
    assert row[4] is not None


def test_ac10_param_snapshot_full_frozen_params(scanned_runtime_workspace) -> None:
    layout = scanned_runtime_workspace["layout"]
    result = scanned_runtime_workspace["scan_result"]
    payload = scanned_runtime_workspace["scan_payload"]
    row = _fetchone(
        layout.library_db,
        "SELECT param_snapshot_json FROM assignment_run WHERE scan_session_id=? ORDER BY id DESC LIMIT 1",
        (payload["data"]["session_id"],),
    )
    snapshot = json.loads(str(row[0]))

    assert result.returncode == 0, result.stderr
    assert snapshot["preview_max_side"] == 480
    assert snapshot["min_cluster_size"] == 2
    assert snapshot["min_samples"] == 1
    assert snapshot["person_merge_threshold"] == 0.26
    assert snapshot["person_cluster_recall_max_rounds"] == 2
    assert snapshot["face_min_quality_for_assignment"] == 0.25
    assert "embedding_flip_weight" not in snapshot


def test_ac11_scan_main_chain_uses_frozen_v5_runtime(scanned_runtime_workspace) -> None:
    layout = scanned_runtime_workspace["layout"]
    result = scanned_runtime_workspace["scan_result"]
    payload = scanned_runtime_workspace["scan_payload"]
    photo_count = int(_fetchone(layout.library_db, "SELECT COUNT(*) FROM photo_asset WHERE asset_status='active'")[0])
    obs_count = int(_fetchone(layout.library_db, "SELECT COUNT(*) FROM face_observation WHERE active=1")[0])
    assignment_count = int(_fetchone(layout.library_db, "SELECT COUNT(*) FROM person_face_assignment WHERE active=1")[0])
    invalid_source_count = int(
        _fetchone(
            layout.library_db,
            """
            SELECT COUNT(*)
            FROM person_face_assignment
            WHERE assignment_source NOT IN ('hdbscan', 'person_consensus', 'merge', 'undo')
            """,
        )[0]
    )
    assignment_source_rows = _fetchall(
        layout.library_db,
        """
        SELECT assignment_source, COUNT(*)
        FROM person_face_assignment
        WHERE active=1
        GROUP BY assignment_source
        ORDER BY assignment_source ASC
        """,
    )
    person_count, baseline_assignment_count, source_dist = parity_baseline._load_product_stats(layout.library_db)
    baseline_person_count, expected_assignment_count = parity_baseline._build_pipeline_baseline(
        layout.library_db,
        layout.embedding_db,
    )

    assert result.returncode == 0, result.stderr
    assert payload["data"]["status"] == "completed"
    assert photo_count == 16
    assert obs_count == 13
    assert assignment_count == 10
    assert person_count == 3
    assert invalid_source_count == 0
    assert assignment_source_rows == [("hdbscan", 10)]
    assert abs(person_count - baseline_person_count) <= 1
    assert abs(baseline_assignment_count - expected_assignment_count) <= 2
    assert source_dist.issubset({"hdbscan", "person_consensus", "merge", "undo"})


def test_ac12_live_photo_pairing_written_in_metadata(scanned_live_runtime_workspace) -> None:
    layout = scanned_live_runtime_workspace["layout"]
    result = scanned_live_runtime_workspace["scan_result"]
    row = _fetchone(
        layout.library_db,
        """
        SELECT is_live_photo, live_mov_path, live_mov_size, live_mov_mtime_ns
        FROM photo_asset
        WHERE primary_path='IMG_6576.HEIC'
        """,
    )

    assert result.returncode == 0, result.stderr
    assert row is not None
    assert row[0] == 1
    assert str(row[1]).endswith(".MOV")
    assert int(row[2]) > 0
    assert int(row[3]) > 0


def test_ac13_homepage_named_anonymous_sections_without_search(seeded_ui_env) -> None:
    client = seeded_ui_env["client"]
    response = client.get("/")
    dom = BeautifulSoup(response.text, "html.parser")
    sections = dom.select("main > section")

    assert response.status_code == 200
    assert [section.get("data-testid") for section in sections] == [
        "named-people-section",
        "anonymous-people-section",
    ]
    assert dom.select_one('[data-testid="people-search-form"]') is None


def test_ac14_nav_items_removed(seeded_ui_env) -> None:
    client = seeded_ui_env["client"]
    response = client.get("/")
    page_paths = {
        route.path
        for route in client.app.routes
        if "GET" in getattr(route, "methods", set())
    }

    assert response.status_code == 200
    assert "待审核" not in response.text
    assert "Identity Run" not in response.text
    assert not any("review" in path.lower() for path in page_paths)
    assert not any("identity" in path.lower() for path in page_paths)


def test_ac15_exclusion_reassign_happens_in_next_scan(runtime_workspace, python_bin: Path) -> None:
    layout = runtime_workspace["layout"]
    first_result = _run_cli_scan(
        python_bin,
        workspace_root=layout.workspace_root,
        args=["--json", "scan", "start-new", "--workspace", str(layout.workspace_root)],
    )
    assert first_result.returncode == 0, first_result.stderr

    first_assignment = _fetchone(
        layout.library_db,
        """
        SELECT a.person_id, a.face_observation_id
        FROM person_face_assignment AS a
        WHERE a.active=1
        ORDER BY a.face_observation_id ASC
        LIMIT 1
        """,
    )
    excluded_person_id = int(first_assignment[0])
    face_observation_id = int(first_assignment[1])

    client = runtime_workspace["client"]
    exclude_response = client.post(
        f"/api/people/{excluded_person_id}/actions/exclude-assignment",
        json={"face_observation_id": face_observation_id},
    )
    assert exclude_response.status_code == 200

    second_result = _run_cli_scan(
        python_bin,
        workspace_root=layout.workspace_root,
        args=["--json", "scan", "start-new", "--workspace", str(layout.workspace_root), "--run-kind", "scan_incremental"],
    )
    assert second_result.returncode == 0, second_result.stderr

    active_row = _fetchone(
        layout.library_db,
        """
        SELECT person_id
        FROM person_face_assignment
        WHERE face_observation_id=? AND active=1
        ORDER BY id DESC
        LIMIT 1
        """,
        (face_observation_id,),
    )
    pending_row = _fetchone(
        layout.library_db,
        "SELECT pending_reassign FROM face_observation WHERE id=?",
        (face_observation_id,),
    )

    assert active_row is not None
    assert int(active_row[0]) != excluded_person_id
    assert pending_row == (0,)


def test_ac16_homepage_has_merge_and_undo_last_merge_actions(seeded_ui_env) -> None:
    client = seeded_ui_env["client"]
    response = client.get("/")
    dom = BeautifulSoup(response.text, "html.parser")

    assert response.status_code == 200
    assert dom.select_one('[data-testid="merge-selected-action"]')["data-enabled"] == "true"
    assert dom.select_one('[data-testid="undo-last-merge-action"]')["data-enabled"] == "true"


def test_ac17_merge_and_undo_restore_exclusion_delta(seeded_ui_env, python_bin: Path) -> None:
    layout = seeded_ui_env["layout"]
    seeded = seeded_ui_env["seeded"]
    loser_id = int(seeded["merge_loser_person_id"])
    winner_id = int(seeded["merge_winner_person_id"])
    face_id = int(seeded["merge_excluded_face_id"])
    _execute_sql(
        layout.library_db,
        """
        INSERT INTO person_face_exclusion(person_id, face_observation_id, reason, active, created_at, updated_at)
        VALUES (?, ?, 'manual_exclude', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (loser_id, face_id),
    )

    merge_result = _run_plain_cli(
        python_bin,
        [
            "--json",
            "people",
            "merge",
            "--selected-person-ids",
            f"{winner_id},{loser_id}",
            "--workspace",
            str(layout.workspace_root),
        ],
    )
    merge_payload = _read_cli_json_output(merge_result.stdout)
    undo_result = _run_plain_cli(
        python_bin,
        ["--json", "people", "undo-last-merge", "--workspace", str(layout.workspace_root)],
    )
    undo_payload = _read_cli_json_output(undo_result.stdout)

    exclusion_rows = _fetchall(
        layout.library_db,
        """
        SELECT person_id, face_observation_id
        FROM person_face_exclusion
        WHERE active=1
        ORDER BY person_id ASC, face_observation_id ASC
        """,
    )
    delta_count = int(
        _fetchone(
            layout.library_db,
            "SELECT COUNT(*) FROM merge_operation_exclusion_delta WHERE merge_operation_id=?",
            (merge_payload["data"]["merge_operation_id"],),
        )[0]
    )

    assert merge_result.returncode == 0, merge_result.stderr
    assert undo_result.returncode == 0, undo_result.stderr
    assert undo_payload["data"]["status"] == "undone"
    assert delta_count >= 1
    assert (loser_id, face_id) in {(int(row[0]), int(row[1])) for row in exclusion_rows}


def test_ac18_export_run_layout_and_collision(seeded_ui_env, python_bin: Path, tmp_path: Path) -> None:
    layout = seeded_ui_env["layout"]
    seeded = seeded_ui_env["seeded"]
    output_root = tmp_path / "exports"
    source_root = Path(str(_fetchone(layout.library_db, "SELECT root_path FROM library_source WHERE id=1")[0]))
    source_photo_path = source_root / "img_b.jpg"

    _execute_sql(
        layout.library_db,
        """
        UPDATE photo_asset
        SET capture_datetime='2024-03-15T09:30:00+08:00', capture_month='2024-03'
        WHERE id=2
        """,
    )
    collision_path = output_root / "group" / "2024-03" / "img_b.jpg"
    collision_path.parent.mkdir(parents=True, exist_ok=True)
    collision_path.write_bytes(b"existing")

    create_result = _run_plain_cli(
        python_bin,
        [
            "--json",
            "export",
            "template",
            "create",
            "--name",
            "acceptance",
            "--output-root",
            str(output_root),
            "--person-ids",
            str(seeded["template_person_id"]),
            "--workspace",
            str(layout.workspace_root),
        ],
    )
    create_payload = _read_cli_json_output(create_result.stdout)
    first_run_result = _run_plain_cli(
        python_bin,
        [
            "--json",
            "export",
            "run",
            str(create_payload["data"]["template_id"]),
            "--workspace",
            str(layout.workspace_root),
        ],
    )
    first_run_payload = _read_cli_json_output(first_run_result.stdout)
    first_execute_result = _run_plain_cli(
        python_bin,
        [
            "--json",
            "export",
            "execute",
            str(first_run_payload["data"]["export_run_id"]),
            "--workspace",
            str(layout.workspace_root),
        ],
    )
    first_execute_payload = _read_cli_json_output(first_execute_result.stdout)
    first_status_payload = _read_export_run_status(
        python_bin,
        export_run_id=int(first_run_payload["data"]["export_run_id"]),
        workspace_root=layout.workspace_root,
    )
    first_deliveries = _fetchall(
        layout.library_db,
        """
        SELECT photo_asset_id, media_kind, bucket, month_key, destination_path, delivery_status
        FROM export_delivery
        WHERE export_run_id=?
        ORDER BY id ASC
        """,
        (first_run_payload["data"]["export_run_id"],),
    )
    fallback_timestamp = datetime(2024, 4, 20, 8, 0, 0).timestamp()
    _execute_sql(
        layout.library_db,
        """
        UPDATE photo_asset
        SET capture_datetime=NULL, capture_month=NULL
        WHERE id=2
        """,
    )
    os.utime(source_photo_path, (fallback_timestamp, fallback_timestamp))
    fallback_path = output_root / "group" / "2024-04" / "img_b.jpg"
    second_run_result = _run_plain_cli(
        python_bin,
        [
            "--json",
            "export",
            "run",
            str(create_payload["data"]["template_id"]),
            "--workspace",
            str(layout.workspace_root),
        ],
    )
    second_run_payload = _read_cli_json_output(second_run_result.stdout)
    second_execute_result = _run_plain_cli(
        python_bin,
        [
            "--json",
            "export",
            "execute",
            str(second_run_payload["data"]["export_run_id"]),
            "--workspace",
            str(layout.workspace_root),
        ],
    )
    second_execute_payload = _read_cli_json_output(second_execute_result.stdout)
    second_status_payload = _read_export_run_status(
        python_bin,
        export_run_id=int(second_run_payload["data"]["export_run_id"]),
        workspace_root=layout.workspace_root,
    )
    second_deliveries = _fetchall(
        layout.library_db,
        """
        SELECT photo_asset_id, media_kind, bucket, month_key, destination_path, delivery_status
        FROM export_delivery
        WHERE export_run_id=?
        ORDER BY id ASC
        """,
        (second_run_payload["data"]["export_run_id"],),
    )

    assert create_result.returncode == 0, create_result.stderr
    assert first_run_result.returncode == 0, first_run_result.stderr
    assert first_execute_result.returncode == 0, first_execute_result.stderr
    assert first_status_payload["data"]["status"] == "completed"
    assert first_execute_payload["data"] == {
        "export_run_id": int(first_run_payload["data"]["export_run_id"]),
        "status": "completed",
        "exported_count": 0,
        "skipped_exists_count": 1,
        "failed_count": 0,
    }
    assert first_status_payload["data"]["summary"] == {
        "exported_count": 0,
        "skipped_exists_count": 1,
        "failed_count": 0,
    }
    assert first_deliveries == [
        (2, "photo", "group", "2024-03", str(collision_path), "skipped_exists"),
    ]
    assert collision_path.read_bytes() == b"existing"
    assert not (output_root / "group" / "2024-03" / "img_a.jpg").exists()

    assert second_run_result.returncode == 0, second_run_result.stderr
    assert second_execute_result.returncode == 0, second_execute_result.stderr
    assert second_status_payload["data"]["status"] == "completed"
    assert second_execute_payload["data"] == {
        "export_run_id": int(second_run_payload["data"]["export_run_id"]),
        "status": "completed",
        "exported_count": 1,
        "skipped_exists_count": 0,
        "failed_count": 0,
    }
    assert second_status_payload["data"]["summary"] == {
        "exported_count": 1,
        "skipped_exists_count": 0,
        "failed_count": 0,
    }
    assert second_deliveries == [
        (2, "photo", "group", "2024-04", str(fallback_path), "exported"),
    ]
    assert fallback_path.read_bytes() == source_photo_path.read_bytes()
    assert not (output_root / "group" / "2024-04" / "img_a.jpg").exists()


def test_ac19_export_template_delete_not_exposed_in_api_or_cli(seeded_ui_env, python_bin: Path) -> None:
    client = seeded_ui_env["client"]
    help_result = _run_plain_cli(python_bin, ["export", "template", "--help"])
    parse_result = _run_plain_cli(
        python_bin,
        ["export", "template", "delete", "1", "--workspace", str(seeded_ui_env["workspace_root"])],
    )
    response = client.delete("/api/export/templates/1")
    delete_routes = [
        route
        for route in client.app.routes
        if getattr(route, "path", "") == "/api/export/templates/{template_id}"
        and "DELETE" in getattr(route, "methods", set())
    ]

    assert help_result.returncode == 0
    assert "delete" not in help_result.stdout
    assert parse_result.returncode != 0
    assert response.status_code in {404, 405}
    assert delete_routes == []


def test_ac20_audit_items_three_types_and_jump_targets(seeded_ui_env) -> None:
    layout = seeded_ui_env["layout"]
    seeded = seeded_ui_env["seeded"]
    session_id = int(seeded["seeded_session_id"])
    assignment_run_id = int(seeded["assignment_run_id"])
    person_id = int(seeded["merge_winner_person_id"])
    face_ids = seeded["audit_face_ids"]
    _execute_sql(
        layout.library_db,
        """
        INSERT INTO scan_audit_item(scan_session_id, assignment_run_id, audit_type, face_observation_id, person_id, evidence_json, created_at)
        VALUES
          (?, ?, 'low_margin_auto_assign', ?, ?, '{"margin":0.02}', CURRENT_TIMESTAMP),
          (?, ?, 'new_anonymous_person', ?, ?, '{"active_face_count":2}', CURRENT_TIMESTAMP)
        """,
        (session_id, assignment_run_id, face_ids[1], person_id, session_id, assignment_run_id, face_ids[2], person_id),
    )
    client = seeded_ui_env["client"]

    api_response = client.get(f"/api/scan/{session_id}/audit-items")
    page_response = client.get(f"/sources/{session_id}/audit")
    dom = BeautifulSoup(page_response.text, "html.parser")
    jump_link = dom.select_one('[data-testid="audit-jump-to-person"]')
    types = {item["audit_type"] for item in api_response.json()["data"]["items"]}

    assert api_response.status_code == 200
    assert page_response.status_code == 200
    assert {"reassign_after_exclusion", "low_margin_auto_assign", "new_anonymous_person"}.issubset(types)
    assert jump_link is not None
    assert f"/people/{person_id}#sample-" in jump_link["href"]


def test_ac21_cli_lock_and_conflict_codes(seeded_ui_env, python_bin: Path) -> None:
    layout = seeded_ui_env["layout"]
    seeded = seeded_ui_env["seeded"]
    merge_person_ids = [int(seeded["merge_winner_person_id"]), int(seeded["merge_loser_person_id"])]

    conflict_session_id = int(
        _execute_insert(
            layout.library_db,
            """
            INSERT INTO scan_session(
              run_kind, status, triggered_by, created_at, updated_at
            ) VALUES ('scan_full', 'running', 'manual_cli', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        )
    )
    scan_conflict_result = _run_plain_cli(
        python_bin,
        ["--json", "scan", "start-new", "--workspace", str(layout.workspace_root)],
    )
    scan_conflict_payload = json.loads(scan_conflict_result.stderr)

    _execute_sql(
        layout.library_db,
        "UPDATE scan_session SET status='completed', finished_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (conflict_session_id,),
    )
    _execute_sql(
        layout.library_db,
        """
        INSERT INTO export_run(template_id, status, summary_json, started_at, finished_at)
        VALUES (1, 'running', '{"exported_count":0,"failed_count":0,"skipped_exists_count":0}', CURRENT_TIMESTAMP, NULL)
        """,
    )
    export_lock_result = _run_plain_cli(
        python_bin,
        [
            "--json",
            "people",
            "merge",
            "--selected-person-ids",
            ",".join(str(item) for item in merge_person_ids),
            "--workspace",
            str(layout.workspace_root),
        ],
    )
    export_lock_payload = json.loads(export_lock_result.stderr)

    assert scan_conflict_result.returncode == 4
    assert scan_conflict_payload["error"]["code"] == "SCAN_ACTIVE_CONFLICT"
    assert scan_conflict_payload["error"]["active_session_id"] == conflict_session_id
    assert export_lock_result.returncode == 5
    assert export_lock_payload["error"]["code"] == "EXPORT_RUNNING_LOCK"


def test_ac22_db_schema_doc_migration_text() -> None:
    doc_text = DB_SCHEMA_DOC.read_text(encoding="utf-8")
    for snippet in [
        "### 3.1 首版（schema_version=1）",
        "### 3.2 后续版本（schema_version>=2）",
        "通过显式 migration 执行 schema 演进。",
        "#### `scan_session`",
        "#### `assignment_run`",
        "#### `export_template`",
        "#### `scan_audit_item`",
        "#### `face_embedding`",
        "首版不提供模板删除 API/CLI",
    ]:
        assert snippet in doc_text


def _run_cli_scan(
    python_bin: Path,
    *,
    workspace_root: Path,
    args: list[str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(python_bin), "-m", "hikbox_pictures.cli", *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _run_plain_cli(python_bin: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(python_bin), "-m", "hikbox_pictures.cli", *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _read_cli_json_output(stdout: str) -> dict[str, object]:
    lines = [line.strip() for line in str(stdout).splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return json.loads(stdout)


def _read_export_run_status(
    python_bin: Path,
    *,
    export_run_id: int,
    workspace_root: Path,
) -> dict[str, object]:
    status_result = _run_plain_cli(
        python_bin,
        [
            "--json",
            "export",
            "run-status",
            str(export_run_id),
            "--workspace",
            str(workspace_root),
        ],
    )
    assert status_result.returncode == 0, status_result.stderr
    return _read_cli_json_output(status_result.stdout)


def _build_workspace_with_source(
    *,
    workspace_root: Path,
    external_root: Path,
    source_root: Path,
    label: str,
) -> WorkspaceLayout:
    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    SourceService(SourceRepository(layout.library_db)).add_source(str(source_root), label=label)
    return layout


def _copy_acceptance_real_dataset(destination_root: Path) -> None:
    destination_root.mkdir(parents=True, exist_ok=True)
    if not REAL_E2E_RAW_DIR.exists():
        raise AssertionError(f"缺少真实验收数据集: {REAL_E2E_RAW_DIR}")
    for pattern in ("person_a_*.jpg", "person_b_*.jpg"):
        for source_path in sorted(REAL_E2E_RAW_DIR.glob(pattern)):
            shutil.copy2(source_path, destination_root / source_path.name)


def _list_sqlite_tables(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type='table'
            ORDER BY name ASC
            """
        ).fetchall()
    finally:
        conn.close()
    return {str(row[0]) for row in rows}


def _pragma_table_info(db_path: Path, table_name: str) -> dict[str, dict[str, object]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info('{table_name}')").fetchall()
    finally:
        conn.close()
    return {
        str(row[1]): {
            "cid": int(row[0]),
            "name": str(row[1]),
            "type": str(row[2]),
            "notnull": int(row[3]),
            "default_value": row[4],
            "pk": int(row[5]),
        }
        for row in rows
    }


def _pragma_foreign_key_list(db_path: Path, table_name: str) -> list[dict[str, object]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA foreign_key_list('{table_name}')").fetchall()
    finally:
        conn.close()
    return [
        {
            "id": int(row[0]),
            "seq": int(row[1]),
            "table": str(row[2]),
            "from": str(row[3]),
            "to": str(row[4]),
            "on_update": str(row[5]),
            "on_delete": str(row[6]),
            "match": str(row[7]),
        }
        for row in rows
    ]


def _pragma_index_list(db_path: Path, table_name: str) -> list[dict[str, object]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA index_list('{table_name}')").fetchall()
    finally:
        conn.close()
    return [
        {
            "seq": int(row[0]),
            "name": str(row[1]),
            "unique": int(row[2]),
            "origin": str(row[3]),
            "partial": int(row[4]),
        }
        for row in rows
    ]


def _pragma_index_columns(db_path: Path, index_name: str) -> list[dict[str, object]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA index_info('{index_name}')").fetchall()
    finally:
        conn.close()
    return [
        {
            "seqno": int(row[0]),
            "cid": int(row[1]),
            "name": str(row[2]),
        }
        for row in rows
    ]


def _fetchone(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> tuple[object, ...] | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(sql, params).fetchone()
    finally:
        conn.close()
    return None if row is None else tuple(row)


def _fetchall(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [tuple(row) for row in rows]


def _execute_sql(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _execute_insert(db_path: Path, sql: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(sql)
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def _seed_ui_data(layout: WorkspaceLayout, session_id: int, face_ids: list[int]) -> dict[str, object]:
    conn = sqlite3.connect(layout.library_db)
    try:
        rename_person_id = _insert_person(conn, display_name="Rename Me", is_named=True)
        exclude_person_id = _insert_person(conn, display_name="Exclude One", is_named=True)
        exclude_batch_person_id = _insert_person(conn, display_name="Exclude Batch", is_named=True)
        merge_winner_person_id = _insert_person(conn, display_name="Winner", is_named=True)
        merge_loser_person_id = _insert_person(conn, display_name="Loser", is_named=True)
        template_person_id = _insert_person(conn, display_name="Template Person", is_named=True)
        _insert_person(conn, display_name="anonymous-7", is_named=False)

        assignment_run_id = int(
            conn.execute(
                """
                INSERT INTO assignment_run(
                  scan_session_id, algorithm_version, param_snapshot_json, run_kind, started_at, finished_at, status, updated_at
                ) VALUES (?, 'frozen_v5', ?, 'scan_full', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'completed', CURRENT_TIMESTAMP)
                """,
                (
                    session_id,
                    json.dumps({"det_size": 640, "workers": 4, "batch_size": 300}, ensure_ascii=False),
                ),
            ).lastrowid
        )
        conn.executemany(
            """
            INSERT INTO person_face_assignment(
              person_id, face_observation_id, assignment_run_id, assignment_source, active, confidence, margin, created_at, updated_at
            ) VALUES (?, ?, ?, 'hdbscan', 1, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            [
                (exclude_person_id, face_ids[0], assignment_run_id, 0.93, 0.11),
                (exclude_batch_person_id, face_ids[1], assignment_run_id, 0.94, 0.12),
                (exclude_batch_person_id, face_ids[2], assignment_run_id, 0.95, 0.13),
                (merge_winner_person_id, face_ids[3], assignment_run_id, 0.96, 0.14),
            ],
        )

        merge_extra_face_id = int(
            conn.execute(
                """
                INSERT INTO face_observation(
                  photo_asset_id, face_index, crop_relpath, aligned_relpath, context_relpath,
                  bbox_x1, bbox_y1, bbox_x2, bbox_y2,
                  detector_confidence, face_area_ratio, magface_quality, quality_score,
                  active, inactive_reason, pending_reassign, created_at, updated_at
                ) VALUES (2, 99, 'artifacts/crops/m99.jpg', 'artifacts/aligned/m99.png', 'artifacts/context/m99.jpg',
                  10, 10, 80, 80, 0.98, 0.3, 1.2, 0.9, 1, NULL, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            ).lastrowid
        )
        template_face_id = int(
            conn.execute(
                """
                INSERT INTO face_observation(
                  photo_asset_id, face_index, crop_relpath, aligned_relpath, context_relpath,
                  bbox_x1, bbox_y1, bbox_x2, bbox_y2,
                  detector_confidence, face_area_ratio, magface_quality, quality_score,
                  active, inactive_reason, pending_reassign, created_at, updated_at
                ) VALUES (2, 100, 'artifacts/crops/t100.jpg', 'artifacts/aligned/t100.png', 'artifacts/context/t100.jpg',
                  18, 18, 58, 58, 0.97, 0.18, 1.1, 0.82, 1, NULL, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO person_face_assignment(
              person_id, face_observation_id, assignment_run_id, assignment_source, active, confidence, margin, created_at, updated_at
            ) VALUES (?, ?, ?, 'hdbscan', 1, 0.97, 0.15, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (merge_loser_person_id, merge_extra_face_id, assignment_run_id),
        )
        conn.execute(
            """
            INSERT INTO person_face_assignment(
              person_id, face_observation_id, assignment_run_id, assignment_source, active, confidence, margin, created_at, updated_at
            ) VALUES (?, ?, ?, 'hdbscan', 1, 0.92, 0.08, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (template_person_id, template_face_id, assignment_run_id),
        )
        template_id = int(
            conn.execute(
                """
                INSERT INTO export_template(name, output_root, enabled, created_at, updated_at)
                VALUES ('模板一', ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (str(layout.workspace_root / "exports-template"),),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO export_template_person(template_id, person_id, created_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            """,
            (template_id, template_person_id),
        )
        conn.execute(
            """
            INSERT INTO scan_audit_item(
              scan_session_id, assignment_run_id, audit_type, face_observation_id, person_id, evidence_json, created_at
            ) VALUES (?, ?, 'reassign_after_exclusion', ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                session_id,
                assignment_run_id,
                face_ids[0],
                merge_winner_person_id,
                json.dumps({"person_id": merge_winner_person_id, "face_observation_id": face_ids[0]}, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "seeded_session_id": session_id,
        "assignment_run_id": assignment_run_id,
        "rename_person_id": rename_person_id,
        "exclude_person_id": exclude_person_id,
        "exclude_batch_person_id": exclude_batch_person_id,
        "merge_winner_person_id": merge_winner_person_id,
        "merge_loser_person_id": merge_loser_person_id,
        "merge_excluded_face_id": face_ids[1],
        "template_person_id": template_person_id,
        "template_face_id": template_face_id,
        "template_id": template_id,
        "audit_face_ids": face_ids,
    }


def _insert_person(conn: sqlite3.Connection, *, display_name: str, is_named: bool) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO person(person_uuid, display_name, is_named, status, created_at, updated_at)
            VALUES (?, ?, ?, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (str(uuid.uuid4()), display_name, 1 if is_named else 0),
        ).lastrowid
    )
