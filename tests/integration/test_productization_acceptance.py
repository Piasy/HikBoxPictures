from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hikbox_pictures.product.audit.service import AssignmentAuditInput, AuditSamplingService
from hikbox_pictures.product.config import WorkspaceLayout, initialize_workspace
from hikbox_pictures.product.engine.frozen_v5 import FROZEN_V5_STAGE_SEQUENCE
from hikbox_pictures.product.export import ensure_export_schema
from hikbox_pictures.product.export.run_service import ExportRunService
from hikbox_pictures.product.scan.assignment_stage import (
    AssignmentCandidate,
    AssignmentStageService,
    FaceEmbeddingRecord,
)
from hikbox_pictures.product.scan.detect_stage import DetectStageRepository, build_scan_runtime_defaults
from hikbox_pictures.product.scan.metadata_stage import MetadataStage
from hikbox_pictures.product.people.repository import SQLitePeopleRepository
from hikbox_pictures.product.people.service import PeopleService
from hikbox_pictures.web.app import ServiceContainer, create_app

NOW = "2026-04-22T00:00:00+00:00"

# AC01-AC22 对照表（AC 编号 -> 测试函数 -> 断言来源 + spec 条目）
AC_MATRIX: dict[str, tuple[str, str]] = {
    "AC01": ("test_ac01_db_schema_constraints_from_sqlite_pragma", "DB（sqlite3 + PRAGMA/真实表结构），spec §17-01"),
    "AC02": ("test_ac02_artifact_layout_on_filesystem", "文件系统（真实目录结构），spec §17-02"),
    "AC03": ("test_ac03_detect_defaults_persisted_in_db", "DB（detect 批次真实入库结果），spec §17-03"),
    "AC04": ("test_ac04_stage_execution_modes", "DB（detect 阶段唯一 claim/ack 约束），spec §17-04"),
    "AC05": ("test_ac05_embeddings_written_to_embedding_db", "DB（embedding.db 真实查询），spec §17-05"),
    "AC06": ("test_ac06_person_uuid_and_merge_tie_break_rule", "DB（person/merge_operation），spec §17-06"),
    "AC07": ("test_ac07_assignment_source_and_noise_rules_from_db", "DB（assignment_source + noise 不落库），spec §17-07"),
    "AC08": ("test_ac08_active_assignment_uniqueness", "DB（active 唯一约束），spec §17-08"),
    "AC09": ("test_ac09_assignment_run_snapshot_from_db", "DB（assignment_run），spec §17-09"),
    "AC10": ("test_ac10_param_snapshot_fields", "DB（param_snapshot_json 字段），spec §17-10"),
    "AC11": ("test_ac11_frozen_pipeline_stage_order", "DB（阶段序快照），spec §17-11"),
    "AC12": ("test_ac12_live_photo_pairing_written_in_metadata", "DB（photo_asset.live_mov_*），spec §17-12"),
    "AC13": ("test_ac13_homepage_sections_visible", "API（TestClient GET /），spec §17-13"),
    "AC14": ("test_ac14_nav_items_removed", "API（TestClient GET /），spec §17-14"),
    "AC15": ("test_ac15_exclusion_marks_pending_reassign_for_next_scan", "CLI + DB（真实命令+真实表），spec §17-15"),
    "AC16": ("test_ac16_homepage_has_merge_actions", "API（TestClient GET /），spec §17-16"),
    "AC17": ("test_ac17_merge_and_undo_restore_exclusion_delta", "CLI + DB（merge_operation_*_delta），spec §17-17"),
    "AC18": ("test_ac18_export_run_layout_and_collision", "CLI + 文件系统 + DB，spec §17-18"),
    "AC19": (
        "test_ac19_api_cli_contract_routes_and_commands + test_ac19_api_data_fields_and_db_side_effect_matrix",
        "API + CLI 合同面（字段矩阵 + DB 联动），spec §17-19",
    ),
    "AC20": ("test_ac20_audit_items_three_types", "API + DB（scan_audit_item），spec §17-20"),
    "AC21": ("test_ac21_cli_lock_and_conflict_codes", "CLI（真实退出码与输出），spec §17-21"),
    "AC22": ("test_ac22_db_schema_doc_migration_text", "文档文本（docs/db_schema.md），spec §17-22"),
}


@pytest.fixture
def workspace_layout(tmp_path: Path) -> WorkspaceLayout:
    return initialize_workspace(tmp_path / "workspace", tmp_path / "external")


@pytest.fixture
def app_client(workspace_layout: WorkspaceLayout) -> TestClient:
    app = create_app(ServiceContainer.from_library_db(workspace_layout.library_db_path))
    return TestClient(app)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _cli_python() -> Path:
    repo_root = _project_root()
    direct_candidate = repo_root / ".venv" / "bin" / "python"
    if direct_candidate.exists():
        return direct_candidate

    git_common = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--git-common-dir"],
        text=True,
        capture_output=True,
        check=False,
    )
    if git_common.returncode == 0:
        common_dir_raw = git_common.stdout.strip()
        if common_dir_raw:
            common_dir = Path(common_dir_raw)
            if not common_dir.is_absolute():
                common_dir = (repo_root / common_dir).resolve()
            repo_root_candidate = common_dir.parent / ".venv" / "bin" / "python"
            if repo_root_candidate.exists():
                return repo_root_candidate
    raise AssertionError("未找到可用 .venv/bin/python")


def _run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_cli_python()), "-m", "hikbox_pictures.cli", *args],
        text=True,
        capture_output=True,
        check=False,
        cwd=cwd,
    )


def _json_stdout(proc: subprocess.CompletedProcess[str]) -> dict[str, object]:
    text = proc.stdout.strip()
    return json.loads(text) if text else {}


def _insert_scan_session(db_path: Path, *, status: str, run_kind: str = "scan_full") -> int:
    with sqlite3.connect(db_path) as conn:
        cursor = _insert_scan_session_in_conn(conn, status=status, run_kind=run_kind)
        conn.commit()
        return int(cursor.lastrowid)


def _insert_scan_session_in_conn(conn: sqlite3.Connection, *, status: str, run_kind: str = "scan_full") -> sqlite3.Cursor:
    return conn.execute(
        """
        INSERT INTO scan_session(
          run_kind,
          status,
          triggered_by,
          resume_from_session_id,
          started_at,
          finished_at,
          last_error,
          created_at,
          updated_at
        )
        VALUES (?, ?, 'manual_cli', NULL, ?, NULL, NULL, ?, ?)
        """,
        (run_kind, status, NOW, NOW, NOW),
    )


def _insert_source(conn: sqlite3.Connection, *, root_path: str = "/tmp/photos", label: str = "源") -> int:
    cursor = conn.execute(
        """
        INSERT INTO library_source(root_path, label, enabled, status, last_discovered_at, created_at, updated_at)
        VALUES (?, ?, 1, 'active', NULL, ?, ?)
        """,
        (root_path, label, NOW, NOW),
    )
    return int(cursor.lastrowid)


def _insert_photo(
    conn: sqlite3.Connection,
    *,
    source_id: int,
    primary_path: str,
    capture_datetime: str = "2026-03-14T12:00:00+08:00",
    capture_month: str = "2026-03",
    mtime_ns: int = 1710000000000000000,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO photo_asset(
          library_source_id,
          primary_path,
          primary_fingerprint,
          fingerprint_algo,
          file_size,
          mtime_ns,
          capture_datetime,
          capture_month,
          is_live_photo,
          live_mov_path,
          live_mov_size,
          live_mov_mtime_ns,
          asset_status,
          created_at,
          updated_at
        )
        VALUES (?, ?, ?, 'sha256', 100, ?, ?, ?, 0, NULL, NULL, NULL, 'active', ?, ?)
        """,
        (source_id, primary_path, f"fp-{primary_path}", mtime_ns, capture_datetime, capture_month, NOW, NOW),
    )
    return int(cursor.lastrowid)


def _insert_face(conn: sqlite3.Connection, *, photo_id: int, face_index: int) -> int:
    cursor = conn.execute(
        """
        INSERT INTO face_observation(
          photo_asset_id,
          face_index,
          crop_relpath,
          aligned_relpath,
          context_relpath,
          bbox_x1,
          bbox_y1,
          bbox_x2,
          bbox_y2,
          detector_confidence,
          face_area_ratio,
          magface_quality,
          quality_score,
          active,
          inactive_reason,
          pending_reassign,
          created_at,
          updated_at
        )
        VALUES (?, ?, 'crop/a.jpg', 'aligned/a.jpg', 'context/a.jpg', 0.1, 0.1, 0.9, 0.9, 0.98, 0.2, 30.0, 0.95, 1, NULL, 0, ?, ?)
        """,
        (photo_id, face_index, NOW, NOW),
    )
    return int(cursor.lastrowid)


def _insert_person(conn: sqlite3.Connection, *, person_uuid: str, display_name: str | None, is_named: int) -> int:
    cursor = conn.execute(
        """
        INSERT INTO person(person_uuid, display_name, is_named, status, merged_into_person_id, created_at, updated_at)
        VALUES (?, ?, ?, 'active', NULL, ?, ?)
        """,
        (person_uuid, display_name, is_named, NOW, NOW),
    )
    return int(cursor.lastrowid)


def _insert_assignment_run(conn: sqlite3.Connection, *, scan_session_id: int, param_snapshot_json: str = "{}") -> int:
    cursor = conn.execute(
        """
        INSERT INTO assignment_run(
          scan_session_id,
          algorithm_version,
          param_snapshot_json,
          run_kind,
          started_at,
          finished_at,
          status
        )
        VALUES (?, 'v5.2026-04-21', ?, 'scan_full', ?, ?, 'completed')
        """,
        (scan_session_id, param_snapshot_json, NOW, NOW),
    )
    return int(cursor.lastrowid)


def _insert_assignment(
    conn: sqlite3.Connection,
    *,
    person_id: int,
    face_observation_id: int,
    assignment_run_id: int,
    assignment_source: str = "hdbscan",
    active: int = 1,
    margin: float | None = 0.2,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO person_face_assignment(
          person_id,
          face_observation_id,
          assignment_run_id,
          assignment_source,
          active,
          confidence,
          margin,
          created_at,
          updated_at
        )
        VALUES (?, ?, ?, ?, ?, 0.9, ?, ?, ?)
        """,
        (person_id, face_observation_id, assignment_run_id, assignment_source, active, margin, NOW, NOW),
    )
    return int(cursor.lastrowid)


def _seed_people_scene(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        source_id = _insert_source(conn)
        p1 = _insert_photo(conn, source_id=source_id, primary_path="a.heic")
        p2 = _insert_photo(conn, source_id=source_id, primary_path="b.heic")
        p3 = _insert_photo(conn, source_id=source_id, primary_path="c.heic")
        f1 = _insert_face(conn, photo_id=p1, face_index=0)
        f2 = _insert_face(conn, photo_id=p2, face_index=1)
        f3 = _insert_face(conn, photo_id=p3, face_index=2)
        person_1 = _insert_person(conn, person_uuid="00000000-0000-0000-0000-000000000101", display_name="甲", is_named=1)
        person_2 = _insert_person(conn, person_uuid="00000000-0000-0000-0000-000000000102", display_name="乙", is_named=1)
        person_3 = _insert_person(conn, person_uuid="00000000-0000-0000-0000-000000000103", display_name=None, is_named=0)
        session_row = _insert_scan_session_in_conn(conn, status="completed")
        session_id = int(session_row.lastrowid)
        run_id = _insert_assignment_run(conn, scan_session_id=session_id)
        _insert_assignment(conn, person_id=person_1, face_observation_id=f1, assignment_run_id=run_id)
        _insert_assignment(conn, person_id=person_1, face_observation_id=f2, assignment_run_id=run_id)
        _insert_assignment(conn, person_id=person_2, face_observation_id=f3, assignment_run_id=run_id)
        conn.commit()
    return {
        "person_1": person_1,
        "person_2": person_2,
        "person_3": person_3,
        "face_1": f1,
        "face_2": f2,
        "face_3": f3,
        "scan_session_id": session_id,
        "assignment_run_id": run_id,
    }


def test_ac01_db_schema_constraints_from_sqlite_pragma(workspace_layout: WorkspaceLayout) -> None:
    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        indexes = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
        }

    assert "scan_session" in tables
    assert "person_face_assignment" in tables
    assert "uq_scan_session_single_active" in indexes
    assert "uq_person_face_assignment_active_face" in indexes


def test_ac02_artifact_layout_on_filesystem(workspace_layout: WorkspaceLayout) -> None:
    assert workspace_layout.crops_root.exists()
    assert workspace_layout.aligned_root.exists()
    assert workspace_layout.context_root.exists()
    assert workspace_layout.logs_root.exists()
    assert (workspace_layout.external_root / "artifacts" / "thumbs").exists() is False
    assert (workspace_layout.external_root / "artifacts" / "ann").exists() is False


def test_ac03_detect_defaults_persisted_in_db(workspace_layout: WorkspaceLayout) -> None:
    defaults = build_scan_runtime_defaults(cpu_count=os.cpu_count() or 1)
    session_id = _insert_scan_session(workspace_layout.library_db_path, status="running")

    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        source_id = _insert_source(conn)
        photo_ids = [_insert_photo(conn, source_id=source_id, primary_path=f"p-{i}.jpg") for i in range(8)]
        conn.commit()

    repo = DetectStageRepository(workspace_layout.library_db_path)
    batch_ids = repo.seed_detect_batches(
        scan_session_id=session_id,
        photo_asset_ids=photo_ids,
        workers=defaults.workers,
        batch_size=defaults.batch_size,
    )

    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        rows = conn.execute(
            """
            SELECT sb.worker_slot, COUNT(*)
            FROM scan_batch_item sbi
            JOIN scan_batch sb ON sb.id = sbi.scan_batch_id
            WHERE sbi.scan_batch_id IN ({})
            GROUP BY sbi.scan_batch_id, sb.worker_slot
            """.format(",".join("?" for _ in batch_ids)),
            batch_ids,
        ).fetchall()
    assert defaults.det_size == 640
    assert defaults.batch_size == 300
    assert defaults.workers == max(1, (os.cpu_count() or 1) // 2)
    assert all(int(row[1]) <= defaults.batch_size for row in rows)


def test_ac04_stage_execution_modes(workspace_layout: WorkspaceLayout) -> None:
    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        session_id = _insert_scan_session(workspace_layout.library_db_path, status="running")
        cursor = conn.execute(
            """
            INSERT INTO scan_batch(scan_session_id, stage, worker_slot, claim_token, status, retry_count, claimed_at, started_at, acked_at, error_message)
            VALUES (?, 'detect', 0, 'ac04-token', 'claimed', 0, ?, NULL, NULL, NULL)
            """,
            (session_id, NOW),
        )
        batch_id = int(cursor.lastrowid)
        conn.execute(
            "INSERT INTO scan_batch_item(scan_batch_id, photo_asset_id, item_order, status, error_message, updated_at) VALUES (?, 1, 0, 'pending', NULL, ?)",
            (batch_id, NOW),
        )
        conn.commit()

    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO scan_batch(scan_session_id, stage, worker_slot, claim_token, status, retry_count, claimed_at, started_at, acked_at, error_message)
                VALUES (?, 'embed', 1, 'ac04-invalid', 'claimed', 0, ?, NULL, NULL, NULL)
                """,
                (session_id, NOW),
            )


def test_ac05_embeddings_written_to_embedding_db(workspace_layout: WorkspaceLayout) -> None:
    session_id = _insert_scan_session(workspace_layout.library_db_path, status="running")
    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        source_id = _insert_source(conn)
        photo_id = _insert_photo(conn, source_id=source_id, primary_path="embedding.heic")
        face_id = _insert_face(conn, photo_id=photo_id, face_index=0)
        conn.commit()

    service = AssignmentStageService(workspace_layout.library_db_path, workspace_layout.embedding_db_path)
    service.start_assignment_run(scan_session_id=session_id, run_kind="scan_full")
    service.persist_face_embeddings(
        [
            FaceEmbeddingRecord(
                face_observation_id=face_id,
                main_embedding=[0.1] * 512,
                flip_embedding=[0.2] * 512,
            )
        ]
    )

    with sqlite3.connect(workspace_layout.embedding_db_path) as conn:
        rows = conn.execute(
            "SELECT variant, dim, dtype FROM face_embedding WHERE face_observation_id=? ORDER BY variant",
            (face_id,),
        ).fetchall()
    assert rows == [("flip", 512, "float32"), ("main", 512, "float32")]


def test_ac06_person_uuid_and_merge_tie_break_rule(workspace_layout: WorkspaceLayout) -> None:
    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        source_id = _insert_source(conn)
        p1 = _insert_photo(conn, source_id=source_id, primary_path="m1.jpg")
        p2 = _insert_photo(conn, source_id=source_id, primary_path="m2.jpg")
        f1 = _insert_face(conn, photo_id=p1, face_index=0)
        f2 = _insert_face(conn, photo_id=p2, face_index=0)
        person_1 = _insert_person(conn, person_uuid="00000000-0000-0000-0000-000000000201", display_name="A", is_named=1)
        person_2 = _insert_person(conn, person_uuid="00000000-0000-0000-0000-000000000202", display_name="B", is_named=1)
        session_id = int(_insert_scan_session_in_conn(conn, status="completed").lastrowid)
        run_id = _insert_assignment_run(conn, scan_session_id=session_id)
        _insert_assignment(conn, person_id=person_1, face_observation_id=f1, assignment_run_id=run_id)
        _insert_assignment(conn, person_id=person_2, face_observation_id=f2, assignment_run_id=run_id)
        conn.commit()

    service = PeopleService(SQLitePeopleRepository(workspace_layout.library_db_path))
    merge = service.merge_people(selected_person_ids=[person_2, person_1])

    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        winner_uuid = conn.execute("SELECT person_uuid FROM person WHERE id=?", (merge.winner_person_id,)).fetchone()
    assert merge.winner_person_id == person_2
    assert winner_uuid == ("00000000-0000-0000-0000-000000000202",)


def test_ac07_assignment_source_and_noise_rules_from_db(workspace_layout: WorkspaceLayout) -> None:
    session_id = _insert_scan_session(workspace_layout.library_db_path, status="running")
    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        source_id = _insert_source(conn)
        p1 = _insert_photo(conn, source_id=source_id, primary_path="s1.jpg")
        p2 = _insert_photo(conn, source_id=source_id, primary_path="s2.jpg")
        f1 = _insert_face(conn, photo_id=p1, face_index=0)
        f2 = _insert_face(conn, photo_id=p2, face_index=0)
        person = _insert_person(conn, person_uuid="00000000-0000-0000-0000-000000000301", display_name="P", is_named=1)
        conn.commit()

    service = AssignmentStageService(workspace_layout.library_db_path, workspace_layout.embedding_db_path)
    run = service.run_assignment(
        scan_session_id=session_id,
        run_kind="scan_full",
        candidates=[
            AssignmentCandidate(face_observation_id=f1, person_id=person, assignment_source="hdbscan", similarity=0.91),
            AssignmentCandidate(face_observation_id=f2, person_id=person, assignment_source="noise", similarity=0.51),
        ],
    )

    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='person_face_assignment'"
        ).fetchone()
        rows = conn.execute(
            "SELECT face_observation_id, assignment_source FROM person_face_assignment WHERE assignment_run_id=? AND active=1 ORDER BY face_observation_id",
            (run.id,),
        ).fetchall()

    assert sql is not None
    assert "assignment_source IN ('hdbscan', 'person_consensus', 'recall', 'merge', 'undo')" in str(sql[0])
    assert "noise" not in str(sql[0])
    assert rows == [(f1, "hdbscan")]


def test_ac08_active_assignment_uniqueness(workspace_layout: WorkspaceLayout) -> None:
    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        source_id = _insert_source(conn)
        photo_id = _insert_photo(conn, source_id=source_id, primary_path="uniq.jpg")
        face_id = _insert_face(conn, photo_id=photo_id, face_index=0)
        person_1 = _insert_person(conn, person_uuid="00000000-0000-0000-0000-000000000401", display_name="X", is_named=1)
        person_2 = _insert_person(conn, person_uuid="00000000-0000-0000-0000-000000000402", display_name="Y", is_named=1)
        session_id = int(_insert_scan_session_in_conn(conn, status="completed").lastrowid)
        run_id = _insert_assignment_run(conn, scan_session_id=session_id)
        _insert_assignment(conn, person_id=person_1, face_observation_id=face_id, assignment_run_id=run_id, active=1)
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            _insert_assignment(conn, person_id=person_2, face_observation_id=face_id, assignment_run_id=run_id, active=1)


def test_ac09_assignment_run_snapshot_from_db(workspace_layout: WorkspaceLayout) -> None:
    session_id = _insert_scan_session(workspace_layout.library_db_path, status="running")
    service = AssignmentStageService(workspace_layout.library_db_path, workspace_layout.embedding_db_path)
    run = service.start_assignment_run(scan_session_id=session_id, run_kind="scan_full")

    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        row = conn.execute(
            "SELECT scan_session_id, algorithm_version, param_snapshot_json FROM assignment_run WHERE id=?",
            (run.id,),
        ).fetchone()

    assert row is not None
    assert int(row[0]) == session_id
    assert str(row[1]) == "v5.2026-04-21"
    assert isinstance(json.loads(str(row[2])), dict)


def test_ac10_param_snapshot_fields(workspace_layout: WorkspaceLayout) -> None:
    session_id = _insert_scan_session(workspace_layout.library_db_path, status="running")
    service = AssignmentStageService(workspace_layout.library_db_path, workspace_layout.embedding_db_path)
    run = service.start_assignment_run(scan_session_id=session_id, run_kind="scan_full")

    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        row = conn.execute("SELECT param_snapshot_json FROM assignment_run WHERE id=?", (run.id,)).fetchone()
    assert row is not None
    snapshot = json.loads(str(row[0]))
    assert snapshot["preview_max_side"] == 480
    assert "embedding_flip_weight" not in snapshot


def test_ac11_frozen_pipeline_stage_order(workspace_layout: WorkspaceLayout) -> None:
    session_id = _insert_scan_session(workspace_layout.library_db_path, status="running")
    service = AssignmentStageService(workspace_layout.library_db_path, workspace_layout.embedding_db_path)
    run = service.start_assignment_run(scan_session_id=session_id, run_kind="scan_full")

    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        row = conn.execute("SELECT param_snapshot_json FROM assignment_run WHERE id=?", (run.id,)).fetchone()
    assert row is not None
    snapshot = json.loads(str(row[0]))
    assert snapshot["stage_sequence"] == list(FROZEN_V5_STAGE_SEQUENCE)


def test_ac12_live_photo_pairing_written_in_metadata(workspace_layout: WorkspaceLayout, tmp_path: Path) -> None:
    source_root = tmp_path / "source-live"
    source_root.mkdir(parents=True, exist_ok=True)
    still = source_root / "IMG_7379.HEIF"
    mov = source_root / ".IMG_7379.HEIF_1771856408349261.MOV"
    still.write_bytes(b"still")
    mov.write_bytes(b"mov")

    fixed_mtime_ns = 1704614400000000000
    os.utime(still, ns=(fixed_mtime_ns, fixed_mtime_ns))

    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        source_id = _insert_source(conn, root_path=str(source_root), label="live")
        _insert_photo(
            conn,
            source_id=source_id,
            primary_path="IMG_7379.HEIF",
            capture_datetime="2026-01-07T00:00:00+00:00",
            capture_month="2026-01",
            mtime_ns=fixed_mtime_ns,
        )
        conn.commit()

    summary = MetadataStage(workspace_layout.library_db_path).run(source_id=source_id, source_root=source_root)
    assert summary.processed_assets == 1

    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        row = conn.execute(
            """
            SELECT is_live_photo, live_mov_path, live_mov_size, live_mov_mtime_ns
            FROM photo_asset
            WHERE library_source_id=? AND primary_path='IMG_7379.HEIF'
            """,
            (source_id,),
        ).fetchone()
    assert row == (1, ".IMG_7379.HEIF_1771856408349261.MOV", mov.stat().st_size, mov.stat().st_mtime_ns)


def test_ac13_homepage_sections_visible(app_client: TestClient, workspace_layout: WorkspaceLayout) -> None:
    _seed_people_scene(workspace_layout.library_db_path)
    resp = app_client.get("/")
    assert resp.status_code == 200
    assert "已命名人物" in resp.text
    assert "匿名人物" in resp.text
    assert "搜索" not in resp.text


def test_ac14_nav_items_removed(app_client: TestClient) -> None:
    resp = app_client.get("/")
    assert resp.status_code == 200
    assert "待审核" not in resp.text
    assert "Identity Run" not in resp.text


def test_ac15_exclusion_marks_pending_reassign_for_next_scan(workspace_layout: WorkspaceLayout) -> None:
    workspace = workspace_layout.workspace_root
    _seed_people_scene(workspace_layout.library_db_path)

    people = _run_cli("--json", "people", "list", "--workspace", str(workspace))
    assert people.returncode == 0
    person_id = int(_json_stdout(people)["data"]["items"][0]["person_id"])

    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        face_id = int(
            conn.execute(
                "SELECT face_observation_id FROM person_face_assignment WHERE person_id=? AND active=1 ORDER BY id LIMIT 1",
                (person_id,),
            ).fetchone()[0]
        )

    exclude = _run_cli(
        "--json",
        "people",
        "exclude",
        str(person_id),
        "--face-observation-id",
        str(face_id),
        "--workspace",
        str(workspace),
    )
    assert exclude.returncode == 0

    start = _run_cli("--json", "scan", "start-or-resume", "--workspace", str(workspace))
    assert start.returncode == 0

    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        pending = conn.execute("SELECT pending_reassign FROM face_observation WHERE id=?", (face_id,)).fetchone()
        exclusion = conn.execute(
            """
            SELECT active
            FROM person_face_exclusion
            WHERE person_id=? AND face_observation_id=?
            """,
            (person_id, face_id),
        ).fetchone()
        active_assignment = conn.execute(
            """
            SELECT COUNT(*)
            FROM person_face_assignment
            WHERE person_id=? AND face_observation_id=? AND active=1
            """,
            (person_id, face_id),
        ).fetchone()
        latest_scan = conn.execute(
            """
            SELECT run_kind, status
            FROM scan_session
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    assert pending == (1,)
    assert exclusion == (1,)
    assert active_assignment == (0,)
    assert latest_scan == ("scan_full", "running")


def test_ac16_homepage_has_merge_actions(app_client: TestClient, workspace_layout: WorkspaceLayout) -> None:
    ids = _seed_people_scene(workspace_layout.library_db_path)
    resp = app_client.get("/")
    assert resp.status_code == 200
    assert "批量合并" in resp.text
    assert "撤销最近一次合并" in resp.text

    merge_resp = app_client.post(
        "/people/actions/merge-batch",
        data={"selected_person_ids": f"{ids['person_1']},{ids['person_2']}"},
        follow_redirects=False,
    )
    assert merge_resp.status_code == 303
    assert merge_resp.headers.get("location") == "/?merge_ok=1"

    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        merge_row = conn.execute(
            "SELECT id, status FROM merge_operation ORDER BY id DESC LIMIT 1",
        ).fetchone()
    assert merge_row is not None
    merge_operation_id = int(merge_row[0])
    assert str(merge_row[1]) == "applied"

    undo_resp = app_client.post("/people/actions/undo-last-merge", follow_redirects=False)
    assert undo_resp.status_code == 303
    assert undo_resp.headers.get("location") == "/?undo_ok=1"

    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        undone_row = conn.execute(
            "SELECT status FROM merge_operation WHERE id=?",
            (merge_operation_id,),
        ).fetchone()
    assert undone_row == ("undone",)


def test_ac17_merge_and_undo_restore_exclusion_delta(workspace_layout: WorkspaceLayout) -> None:
    workspace = workspace_layout.workspace_root
    ids = _seed_people_scene(workspace_layout.library_db_path)

    exclude = _run_cli(
        "--json",
        "people",
        "exclude",
        str(ids["person_2"]),
        "--face-observation-id",
        str(ids["face_3"]),
        "--workspace",
        str(workspace),
    )
    assert exclude.returncode == 0

    merge = _run_cli(
        "--json",
        "people",
        "merge",
        "--selected-person-ids",
        f"{ids['person_1']},{ids['person_2']}",
        "--workspace",
        str(workspace),
    )
    assert merge.returncode == 0
    merge_operation_id = int(_json_stdout(merge)["data"]["merge_operation_id"])

    undo = _run_cli("--json", "people", "undo-last-merge", "--workspace", str(workspace))
    assert undo.returncode == 0

    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        delta_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM merge_operation_exclusion_delta WHERE merge_operation_id=?",
                (merge_operation_id,),
            ).fetchone()[0]
        )
        status = conn.execute("SELECT status FROM merge_operation WHERE id=?", (merge_operation_id,)).fetchone()
        exclusion = conn.execute(
            "SELECT active FROM person_face_exclusion WHERE person_id=? AND face_observation_id=?",
            (ids["person_2"], ids["face_3"]),
        ).fetchone()

    assert delta_count >= 1
    assert status == ("undone",)
    assert exclusion == (1,)


def test_ac18_export_run_layout_and_collision(workspace_layout: WorkspaceLayout, tmp_path: Path) -> None:
    workspace = workspace_layout.workspace_root
    source_root = tmp_path / "source-export"
    output_root = (tmp_path / "exports").resolve()
    source_root.mkdir(parents=True, exist_ok=True)
    output_root.mkdir(parents=True, exist_ok=True)

    (source_root / "only.jpg").write_bytes(b"only")
    (source_root / "group.jpg").write_bytes(b"group")

    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        source_id = _insert_source(conn, root_path=str(source_root), label="export")
        named_person = _insert_person(
            conn,
            person_uuid="00000000-0000-0000-0000-000000000501",
            display_name="命名人物",
            is_named=1,
        )
        other_person = _insert_person(
            conn,
            person_uuid="00000000-0000-0000-0000-000000000502",
            display_name="其他人物",
            is_named=1,
        )
        only_photo = _insert_photo(conn, source_id=source_id, primary_path="only.jpg", capture_month="2026-03")
        group_photo = _insert_photo(
            conn,
            source_id=source_id,
            primary_path="group.jpg",
            capture_datetime="2026-04-01T09:00:00+08:00",
            capture_month="2026-04",
            mtime_ns=1710000001000000000,
        )
        only_face = _insert_face(conn, photo_id=only_photo, face_index=0)
        group_face_1 = _insert_face(conn, photo_id=group_photo, face_index=0)
        group_face_2 = _insert_face(conn, photo_id=group_photo, face_index=1)
        session_id = int(_insert_scan_session_in_conn(conn, status="completed").lastrowid)
        run_id = _insert_assignment_run(conn, scan_session_id=session_id)
        _insert_assignment(conn, person_id=named_person, face_observation_id=only_face, assignment_run_id=run_id)
        _insert_assignment(conn, person_id=named_person, face_observation_id=group_face_1, assignment_run_id=run_id)
        _insert_assignment(conn, person_id=other_person, face_observation_id=group_face_2, assignment_run_id=run_id)
        conn.commit()

    collision_path = output_root / "only" / "2026-03" / "only.jpg"
    collision_path.parent.mkdir(parents=True, exist_ok=True)
    collision_path.write_bytes(b"existing")

    create = _run_cli(
        "--json",
        "export",
        "template",
        "create",
        "--name",
        "模板-验收",
        "--output-root",
        str(output_root),
        "--person-ids",
        str(named_person),
        "--workspace",
        str(workspace),
    )
    assert create.returncode == 0
    template_id = int(_json_stdout(create)["data"]["template_id"])

    run = _run_cli("--json", "export", "run", str(template_id), "--workspace", str(workspace))
    assert run.returncode == 0
    run_payload = _json_stdout(run)["data"]
    assert run_payload["status"] == "running"
    # 当前 CLI run 仅负责启动 run，真实投递由服务执行。
    finished_run = ExportRunService(workspace_layout.library_db_path).execute_export(template_id=template_id)
    run_id = finished_run.id

    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        rows = conn.execute(
            """
            SELECT bucket, month_key, destination_path, delivery_status
            FROM export_delivery
            WHERE export_run_id=?
            ORDER BY id
            """,
            (run_id,),
        ).fetchall()

    assert rows == [
        ("only", "2026-03", str(output_root / "only" / "2026-03" / "only.jpg"), "skipped_exists"),
        ("group", "2026-04", str(output_root / "group" / "2026-04" / "group.jpg"), "exported"),
    ]
    assert (output_root / "group" / "2026-04" / "group.jpg").exists()


def test_ac19_api_cli_contract_routes_and_commands(workspace_layout: WorkspaceLayout) -> None:
    app = create_app(ServiceContainer.from_library_db(workspace_layout.library_db_path))
    route_method_pairs = set()
    for route in app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if not methods or not path:
            continue
        for method in methods:
            route_method_pairs.add((str(path), str(method)))

    expected_pairs = {
        ("/api/scan/start_or_resume", "POST"),
        ("/api/scan/start_new", "POST"),
        ("/api/scan/abort", "POST"),
        ("/api/people/{person_id}/actions/rename", "POST"),
        ("/api/people/{person_id}/actions/exclude-assignment", "POST"),
        ("/api/people/{person_id}/actions/exclude-assignments", "POST"),
        ("/api/people/actions/merge-batch", "POST"),
        ("/api/people/actions/undo-last-merge", "POST"),
        ("/api/export/templates", "GET"),
        ("/api/export/templates", "POST"),
        ("/api/export/templates/{template_id}", "PUT"),
        ("/api/export/templates/{template_id}/actions/run", "POST"),
        ("/api/scan/{session_id}/audit-items", "GET"),
    }
    assert expected_pairs.issubset(route_method_pairs)

    help_output = _run_cli("--help")
    assert help_output.returncode == 0
    help_text = help_output.stdout + help_output.stderr
    for token in [
        "init",
        "config",
        "source",
        "scan",
        "serve",
        "people",
        "export",
        "logs",
        "audit",
        "db",
    ]:
        assert token in help_text

    people_merge_help = _run_cli("people", "merge", "--help")
    assert people_merge_help.returncode == 0
    people_merge_text = people_merge_help.stdout + people_merge_help.stderr
    assert "--selected-person-ids" in people_merge_text

    scan_status_help = _run_cli("scan", "status", "--help")
    assert scan_status_help.returncode == 0
    scan_status_text = scan_status_help.stdout + scan_status_help.stderr
    assert "--latest" in scan_status_text
    assert "--session-id" in scan_status_text

    export_template_create_help = _run_cli("export", "template", "create", "--help")
    assert export_template_create_help.returncode == 0
    export_template_create_text = export_template_create_help.stdout + export_template_create_help.stderr
    assert "--name" in export_template_create_text
    assert "--output-root" in export_template_create_text


def test_ac19_api_data_fields_and_db_side_effect_matrix(workspace_layout: WorkspaceLayout) -> None:
    ids = _seed_people_scene(workspace_layout.library_db_path)
    client = TestClient(create_app(ServiceContainer.from_library_db(workspace_layout.library_db_path)))

    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        conn.execute("UPDATE scan_session SET status='interrupted' WHERE id=?", (ids["scan_session_id"],))
        conn.commit()

    start_or_resume = client.post("/api/scan/start_or_resume", json={})
    assert start_or_resume.status_code == 200
    start_or_resume_data = start_or_resume.json()["data"]
    assert set(start_or_resume_data.keys()) == {"session_id", "status", "resumed"}
    resumed_session_id = int(start_or_resume_data["session_id"])
    assert start_or_resume_data == {
        "session_id": resumed_session_id,
        "status": "running",
        "resumed": True,
    }
    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        resumed_row = conn.execute(
            "SELECT status FROM scan_session WHERE id=?",
            (resumed_session_id,),
        ).fetchone()
    assert resumed_row == ("running",)

    abort_resumed = client.post("/api/scan/abort", json={"session_id": resumed_session_id})
    assert abort_resumed.status_code == 200
    assert abort_resumed.json()["data"] == {"session_id": resumed_session_id, "status": "aborting"}
    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        conn.execute(
            "UPDATE scan_session SET status='completed', finished_at=?, updated_at=? WHERE id=?",
            (NOW, NOW, resumed_session_id),
        )
        conn.commit()

    interrupted_for_new = _insert_scan_session(workspace_layout.library_db_path, status="interrupted", run_kind="scan_resume")
    start_new = client.post("/api/scan/start_new", json={"run_kind": "scan_full"})
    assert start_new.status_code == 200
    start_new_data = start_new.json()["data"]
    assert set(start_new_data.keys()) == {"session_id", "status"}
    running_id = int(start_new_data["session_id"])
    assert start_new_data == {"session_id": running_id, "status": "running"}
    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        old_interrupted_row = conn.execute(
            "SELECT status FROM scan_session WHERE id=?",
            (interrupted_for_new,),
        ).fetchone()
        running_row = conn.execute(
            "SELECT status FROM scan_session WHERE id=?",
            (running_id,),
        ).fetchone()
    assert old_interrupted_row == ("abandoned",)
    assert running_row == ("running",)

    abort_resp = client.post("/api/scan/abort", json={"session_id": running_id})
    assert abort_resp.status_code == 200
    assert abort_resp.json()["data"] == {"session_id": running_id, "status": "aborting"}

    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        row = conn.execute("SELECT status FROM scan_session WHERE id=?", (running_id,)).fetchone()
    assert row == ("aborting",)

    rename = client.post(f"/api/people/{ids['person_3']}/actions/rename", json={"display_name": "新名字"})
    assert rename.status_code == 200
    rename_data = rename.json()["data"]
    assert set(rename_data.keys()) == {"person_id", "display_name", "is_named"}
    assert rename_data == {
        "person_id": ids["person_3"],
        "display_name": "新名字",
        "is_named": True,
    }
    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        renamed_row = conn.execute(
            "SELECT display_name, is_named FROM person WHERE id=?",
            (ids["person_3"],),
        ).fetchone()
    assert renamed_row == ("新名字", 1)

    exclude = client.post(
        f"/api/people/{ids['person_1']}/actions/exclude-assignment",
        json={"face_observation_id": ids["face_1"]},
    )
    assert exclude.status_code == 200
    exclude_data = exclude.json()["data"]
    assert set(exclude_data.keys()) == {"person_id", "face_observation_id", "pending_reassign"}
    assert exclude_data == {
        "person_id": ids["person_1"],
        "face_observation_id": ids["face_1"],
        "pending_reassign": 1,
    }
    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        exclusion_row = conn.execute(
            """
            SELECT pfa.active, fo.pending_reassign
            FROM person_face_assignment pfa
            JOIN face_observation fo ON fo.id = pfa.face_observation_id
            WHERE pfa.person_id=? AND pfa.face_observation_id=?
            ORDER BY pfa.id DESC
            LIMIT 1
            """,
            (ids["person_1"], ids["face_1"]),
        ).fetchone()
    assert exclusion_row == (0, 1)

    exclude_batch = client.post(
        f"/api/people/{ids['person_1']}/actions/exclude-assignments",
        json={"face_observation_ids": [ids["face_2"]]},
    )
    assert exclude_batch.status_code == 200
    exclude_batch_data = exclude_batch.json()["data"]
    assert set(exclude_batch_data.keys()) == {"person_id", "excluded_count"}
    assert exclude_batch_data == {"person_id": ids["person_1"], "excluded_count": 1}
    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        exclusion_count = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM person_face_exclusion
                WHERE person_id=? AND face_observation_id=? AND active=1
                """,
                (ids["person_1"], ids["face_2"]),
            ).fetchone()[0]
        )
    assert exclusion_count == 1

    merge = client.post(
        "/api/people/actions/merge-batch",
        json={"selected_person_ids": [ids["person_1"], ids["person_2"]]},
    )
    assert merge.status_code == 200
    merge_data = merge.json()["data"]
    assert set(merge_data.keys()) == {"merge_operation_id", "winner_person_id", "winner_person_uuid"}
    merge_operation_id = int(merge_data["merge_operation_id"])
    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        merge_row = conn.execute(
            "SELECT winner_person_id, winner_person_uuid, status FROM merge_operation WHERE id=?",
            (merge_operation_id,),
        ).fetchone()
    assert merge_row is not None
    assert merge_data == {
        "merge_operation_id": merge_operation_id,
        "winner_person_id": int(merge_row[0]),
        "winner_person_uuid": str(merge_row[1]),
    }
    assert str(merge_row[2]) == "applied"

    undo = client.post("/api/people/actions/undo-last-merge", json={})
    assert undo.status_code == 200
    undo_data = undo.json()["data"]
    assert set(undo_data.keys()) == {"merge_operation_id", "status"}
    assert undo_data == {"merge_operation_id": merge_operation_id, "status": "undone"}
    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        undo_row = conn.execute(
            "SELECT status FROM merge_operation WHERE id=?",
            (merge_operation_id,),
        ).fetchone()
    assert undo_row == ("undone",)

    tpl_create = client.post(
        "/api/export/templates",
        json={"name": "模板-AC19", "output_root": str((workspace_layout.workspace_root / "out").resolve()), "person_ids": [ids["person_1"]]},
    )
    assert tpl_create.status_code == 200
    tpl_create_data = tpl_create.json()["data"]
    assert set(tpl_create_data.keys()) == {"template_id"}
    template_id = int(tpl_create_data["template_id"])
    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        template_row = conn.execute(
            "SELECT name, output_root, enabled FROM export_template WHERE id=?",
            (template_id,),
        ).fetchone()
        template_person_rows = conn.execute(
            "SELECT person_id FROM export_template_person WHERE template_id=? ORDER BY person_id",
            (template_id,),
        ).fetchall()
    assert template_row is not None
    assert str(template_row[0]) == "模板-AC19"
    assert int(template_row[2]) == 1
    assert [int(row[0]) for row in template_person_rows] == [ids["person_1"]]

    tpl_update = client.put(
        f"/api/export/templates/{template_id}",
        json={"name": "模板-AC19-更新", "person_ids": [ids["person_1"], ids["person_2"]]},
    )
    assert tpl_update.status_code == 200
    tpl_update_data = tpl_update.json()["data"]
    assert set(tpl_update_data.keys()) == {"template_id", "updated"}
    assert tpl_update_data == {"template_id": template_id, "updated": True}
    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        updated_template = conn.execute(
            "SELECT name FROM export_template WHERE id=?",
            (template_id,),
        ).fetchone()
        updated_person_rows = conn.execute(
            "SELECT person_id FROM export_template_person WHERE template_id=? ORDER BY person_id",
            (template_id,),
        ).fetchall()
    assert updated_template == ("模板-AC19-更新",)
    assert [int(row[0]) for row in updated_person_rows] == [ids["person_1"], ids["person_2"]]

    tpl_list = client.get("/api/export/templates")
    assert tpl_list.status_code == 200
    tpl_list_data = tpl_list.json()["data"]
    assert set(tpl_list_data.keys()) == {"items"}
    assert isinstance(tpl_list_data["items"], list)
    list_item = next(item for item in tpl_list_data["items"] if int(item["template_id"]) == template_id)
    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        list_template_row = conn.execute(
            "SELECT name, output_root, enabled FROM export_template WHERE id=?",
            (template_id,),
        ).fetchone()
    assert list_template_row is not None
    assert list_item["name"] == str(list_template_row[0])
    assert list_item["output_root"] == str(list_template_row[1])
    assert list_item["enabled"] is bool(int(list_template_row[2]))
    assert list_item["person_ids"] == [ids["person_1"], ids["person_2"]]

    run = client.post(f"/api/export/templates/{template_id}/actions/run", json={})
    assert run.status_code == 200
    run_data = run.json()["data"]
    assert set(run_data.keys()) == {"export_run_id", "status"}
    assert run_data["status"] == "running"
    export_run_id = int(run_data["export_run_id"])

    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        db_row = conn.execute(
            "SELECT template_id, status FROM export_run WHERE id=?",
            (export_run_id,),
        ).fetchone()
    assert db_row == (template_id, "running")

    audit_service = AuditSamplingService(workspace_layout.library_db_path)
    audit_service.sample_assignment_run(
        scan_session_id=ids["scan_session_id"],
        assignment_run_id=ids["assignment_run_id"],
        assignments=[
            AssignmentAuditInput(
                face_observation_id=ids["face_3"],
                person_id=ids["person_2"],
                assignment_source="hdbscan",
                margin=0.01,
                evidence={"source": "ac19"},
            ),
        ],
    )
    audit_resp = client.get(f"/api/scan/{ids['scan_session_id']}/audit-items")
    assert audit_resp.status_code == 200
    audit_data = audit_resp.json()["data"]
    assert set(audit_data.keys()) == {"items"}
    assert isinstance(audit_data["items"], list)
    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        db_audit_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM scan_audit_item WHERE scan_session_id=?",
                (ids["scan_session_id"],),
            ).fetchone()[0]
        )
    assert db_audit_count == len(audit_data["items"])


def test_ac20_audit_items_three_types(workspace_layout: WorkspaceLayout) -> None:
    ids = _seed_people_scene(workspace_layout.library_db_path)
    service = AuditSamplingService(workspace_layout.library_db_path)
    service.sample_assignment_run(
        scan_session_id=ids["scan_session_id"],
        assignment_run_id=ids["assignment_run_id"],
        assignments=[
            AssignmentAuditInput(
                face_observation_id=ids["face_1"],
                person_id=ids["person_1"],
                assignment_source="hdbscan",
                margin=0.01,
                evidence={"k": "low"},
            ),
            AssignmentAuditInput(
                face_observation_id=ids["face_2"],
                person_id=ids["person_1"],
                assignment_source="hdbscan",
                reassign_after_exclusion=True,
                evidence={"k": "reassign"},
            ),
            AssignmentAuditInput(
                face_observation_id=ids["face_3"],
                person_id=ids["person_3"],
                assignment_source="hdbscan",
                new_anonymous_person=True,
                evidence={"k": "anonymous"},
            ),
        ],
    )

    client = TestClient(create_app(ServiceContainer.from_library_db(workspace_layout.library_db_path)))
    resp = client.get(f"/api/scan/{ids['scan_session_id']}/audit-items")
    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    audit_types = {item["audit_type"] for item in items}

    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        db_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM scan_audit_item WHERE scan_session_id=?",
                (ids["scan_session_id"],),
            ).fetchone()[0]
        )

    assert audit_types == {
        "low_margin_auto_assign",
        "reassign_after_exclusion",
        "new_anonymous_person",
    }
    assert db_count == 3


def test_ac21_cli_lock_and_conflict_codes(workspace_layout: WorkspaceLayout) -> None:
    workspace = workspace_layout.workspace_root
    ids = _seed_people_scene(workspace_layout.library_db_path)

    active_scan = _insert_scan_session(workspace_layout.library_db_path, status="running")
    serve = _run_cli("serve", "start", "--workspace", str(workspace), "--host", "127.0.0.1", "--port", "8013")
    assert serve.returncode == 7
    assert "SERVE_BLOCKED_BY_ACTIVE_SCAN" in (serve.stdout + serve.stderr)

    with sqlite3.connect(workspace_layout.library_db_path) as conn:
        conn.execute(
            "UPDATE scan_session SET status='completed', finished_at=?, updated_at=? WHERE id=?",
            (NOW, NOW, active_scan),
        )
        ensure_export_schema(conn)
        conn.execute(
            """
            INSERT INTO export_template(name, output_root, enabled, created_at, updated_at)
            VALUES ('模板-lock', '/tmp/lock', 1, ?, ?)
            """,
            (NOW, NOW),
        )
        template_id = int(conn.execute("SELECT id FROM export_template ORDER BY id DESC LIMIT 1").fetchone()[0])
        conn.execute(
            "INSERT INTO export_run(template_id, status, summary_json, started_at, finished_at) VALUES (?, 'running', '{}', ?, NULL)",
            (template_id, NOW),
        )
        conn.commit()

    rename = _run_cli(
        "--json",
        "people",
        "rename",
        str(ids["person_1"]),
        "锁冲突名字",
        "--workspace",
        str(workspace),
    )
    assert rename.returncode == 5
    assert "EXPORT_RUNNING_LOCK" in (rename.stdout + rename.stderr)


def test_ac22_db_schema_doc_migration_text() -> None:
    doc_path = _project_root() / "docs" / "db_schema.md"
    text = doc_path.read_text(encoding="utf-8")
    assert "首版（schema_version=1）" in text
    assert "后续版本（schema_version>=2）" in text
    assert "通过显式 migration 执行 schema 演进" in text


def test_ac_matrix_is_complete() -> None:
    # 额外自检：确保 AC01-AC22 映射完整且不缺失。
    expected = {f"AC{i:02d}" for i in range(1, 23)}
    assert set(AC_MATRIX.keys()) == expected
