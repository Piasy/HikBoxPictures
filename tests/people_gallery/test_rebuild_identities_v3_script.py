from __future__ import annotations

import sqlite3
import sys
import time
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from hikbox_pictures.db.connection import connect_db

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_FIXTURE_SPEC = spec_from_file_location("people_gallery_fixtures_workspace_task5_script", _FIXTURE_PATH)
if _FIXTURE_SPEC is None or _FIXTURE_SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_FIXTURE_MODULE = module_from_spec(_FIXTURE_SPEC)
sys.modules[_FIXTURE_SPEC.name] = _FIXTURE_MODULE
_FIXTURE_SPEC.loader.exec_module(_FIXTURE_MODULE)
build_seed_workspace_with_mock_embeddings = _FIXTURE_MODULE.build_seed_workspace_with_mock_embeddings
seed_active_identity_threshold_profile = _FIXTURE_MODULE.seed_active_identity_threshold_profile

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "rebuild_identities_v3.py"
_SCRIPT_SPEC = spec_from_file_location("task5_rebuild_identities_v3_script", _SCRIPT_PATH)
if _SCRIPT_SPEC is None or _SCRIPT_SPEC.loader is None:
    raise RuntimeError(f"无法加载重建脚本: {_SCRIPT_PATH}")
_SCRIPT_MODULE = module_from_spec(_SCRIPT_SPEC)
sys.modules[_SCRIPT_SPEC.name] = _SCRIPT_MODULE
_SCRIPT_SPEC.loader.exec_module(_SCRIPT_MODULE)
rebuild_main = _SCRIPT_MODULE.main


def _count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
    assert row is not None
    return int(row["c"])


def _active_profile_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM identity_threshold_profile
        WHERE active = 1
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return int(row["id"])


def test_rebuild_script_dry_run_reports_scope_and_backup_without_destructive_write(tmp_path: Path) -> None:
    workspace = tmp_path / "task5-script-dry"
    build_seed_workspace_with_mock_embeddings(workspace)

    conn = sqlite3.connect(workspace / ".hikbox" / "library.db")
    conn.row_factory = sqlite3.Row
    try:
        seed_active_identity_threshold_profile(conn)
        before = {
            "person": _count(conn, "person"),
            "person_face_assignment": _count(conn, "person_face_assignment"),
            "export_template": _count(conn, "export_template"),
        }
    finally:
        conn.close()

    rc = rebuild_main(["--workspace", str(workspace), "--dry-run", "--backup-db"])
    assert rc == 0

    conn2 = sqlite3.connect(workspace / ".hikbox" / "library.db")
    conn2.row_factory = sqlite3.Row
    try:
        after = {
            "person": _count(conn2, "person"),
            "person_face_assignment": _count(conn2, "person_face_assignment"),
            "export_template": _count(conn2, "export_template"),
        }
    finally:
        conn2.close()

    assert after == before

    summary_path = workspace / ".tmp" / "rebuild-identities-v3" / "last-summary.json"
    assert summary_path.exists()
    summary = _FIXTURE_MODULE.read_json(summary_path)
    assert summary["dry_run"] is True
    assert summary["phase1_order"] == [
        "profile_resolve",
        "clear_identity_export_layers",
        "quality_backfill",
        "bootstrap_materialize",
        "prototype_ann_rebuild_optional",
        "summary",
    ]
    assert summary["clear_scope"]["person"] >= 1
    assert summary["clear_scope"]["export_template"] >= 1
    assert summary["dry_run_plan"]["clear_targets"] == summary["clear_scope"]
    assert summary["dry_run_plan"]["threshold_profile_candidate_validated"] is False

    backup_path = Path(str(summary["backup_db_path"]))
    assert backup_path.exists()
    assert backup_path.is_file()


def test_rebuild_without_existing_active_profile_bootstraps_default_profile(tmp_path: Path) -> None:
    workspace = tmp_path / "task5-script-bootstrap-profile"
    build_seed_workspace_with_mock_embeddings(workspace)

    conn = connect_db(workspace / ".hikbox" / "library.db")
    try:
        assert _count(conn, "identity_threshold_profile") == 0
        assert _active_profile_id(conn) is None
    finally:
        conn.close()

    rc = rebuild_main(["--workspace", str(workspace), "--backup-db"])
    assert rc == 0

    summary = _FIXTURE_MODULE.read_workspace_rebuild_summary(workspace)
    assert summary is not None
    assert summary["threshold_profile_id"] is not None
    assert summary["profile_mode"] == "seeded"
    assert summary["profile"]["profile_mode"] == "seeded"
    assert summary["imported_threshold_profile"] is False
    assert summary["update_profile_quantiles"] is True

    conn2 = connect_db(workspace / ".hikbox" / "library.db")
    try:
        assert _count(conn2, "identity_threshold_profile") == 1
        active = conn2.execute(
            "SELECT * FROM identity_threshold_profile WHERE active = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert active is not None
        assert int(active["id"]) == int(summary["threshold_profile_id"])
        assert str(active["embedding_model_key"]) == "pipeline-stub-v1"
    finally:
        conn2.close()


def test_rebuild_script_emits_periodic_progress_logs_for_long_phase(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    closed = {"value": False}

    class _FakeRebuildService:
        def __init__(self, workspace: Path) -> None:
            self.workspace = Path(workspace)

        def run_rebuild(
            self,
            *,
            dry_run: bool,
            backup_db: bool,
            skip_ann_rebuild: bool,
            threshold_profile_path: Path | None,
            progress_reporter=None,
        ) -> dict[str, object]:
            assert dry_run is False
            assert backup_db is True
            assert skip_ann_rebuild is False
            assert threshold_profile_path is None
            assert progress_reporter is not None
            progress_reporter(
                {
                    "phase": "quality_backfill",
                    "subphase": "write_scores",
                    "status": "running",
                    "total_count": 100,
                    "completed_count": 25,
                    "percent": 25.0,
                    "unit": "observation",
                }
            )
            time.sleep(0.035)
            return {
                "threshold_profile_id": 1,
                "profile_mode": "seeded",
                "materialized_cluster_count": 0,
                "review_pending_cluster_count": 0,
                "discarded_cluster_count": 0,
            }

        def close(self) -> None:
            closed["value"] = True

    monkeypatch.setattr(_SCRIPT_MODULE, "IdentityRebuildService", _FakeRebuildService)
    monkeypatch.setattr(_SCRIPT_MODULE, "_PROGRESS_HEARTBEAT_SECONDS", 0.01, raising=False)

    rc = rebuild_main(["--workspace", str(tmp_path), "--backup-db"])

    assert rc == 0
    assert closed["value"] is True
    captured = capsys.readouterr()
    assert "identity v3 进度:" in captured.err
    assert "quality_backfill" in captured.err
    assert "\"subphase\": \"write_scores\"" in captured.err
    assert "\"total_count\": 100" in captured.err
    assert "\"completed_count\": 25" in captured.err
    assert "\"percent\": 25.0" in captured.err
    assert "identity v3 重建完成:" in captured.out


def test_rebuild_script_roundtrip_profile_idempotent_and_clears_identity_export_layers(tmp_path: Path) -> None:
    workspace = tmp_path / "task5-script-run"
    build_seed_workspace_with_mock_embeddings(workspace)

    conn = sqlite3.connect(workspace / ".hikbox" / "library.db")
    conn.row_factory = sqlite3.Row
    ops_event_before = 0
    try:
        seed = seed_active_identity_threshold_profile(conn)
        ops_event_before = _count(conn, "ops_event")
        person_id_row = conn.execute("SELECT id FROM person ORDER BY id ASC LIMIT 1").fetchone()
        assert person_id_row is not None
        person_id = int(person_id_row["id"])
        batch_id = int(
            conn.execute(
                """
                INSERT INTO auto_cluster_batch(
                    model_key,
                    algorithm_version,
                    batch_type,
                    threshold_profile_id,
                    scan_session_id
                )
                VALUES (?, ?, 'bootstrap', ?, NULL)
                """,
                ("pipeline-stub-v1", "seed-task5", int(seed["active_profile_id"])),
            ).lastrowid
        )
        cluster_id = int(
            conn.execute(
                """
                INSERT INTO auto_cluster(
                    batch_id,
                    representative_observation_id,
                    cluster_status,
                    resolved_person_id,
                    diagnostic_json
                )
                VALUES (?, NULL, 'review_pending', ?, '{}')
                """,
                (batch_id, person_id),
            ).lastrowid
        )
        conn.execute(
            "UPDATE person SET origin_cluster_id = ? WHERE id = ?",
            (cluster_id, person_id),
        )
        conn.commit()
    finally:
        conn.close()

    summary_before = _FIXTURE_MODULE.read_workspace_rebuild_summary(workspace)
    assert summary_before is None

    candidate_path = workspace / ".tmp" / "task5" / "candidate-profile.json"
    profile = _FIXTURE_MODULE.build_identity_profile_candidate_from_active_db(workspace)
    profile.update(
        {
            "profile_name": "task5-导入档",
            "area_log_p10": -9.0,
            "area_log_p90": -8.0,
            "sharpness_log_p10": -2.0,
            "sharpness_log_p90": -1.0,
            "bootstrap_min_cluster_size": 2,
            "bootstrap_min_distinct_photo_count": 2,
            "bootstrap_min_high_quality_count": 2,
            "bootstrap_seed_min_count": 2,
            "bootstrap_seed_max_count": 4,
            "high_quality_threshold": 0.01,
            "trusted_seed_quality_threshold": 0.01,
            "bootstrap_edge_accept_threshold": 1.0,
            "bootstrap_edge_candidate_threshold": 1.2,
            "bootstrap_margin_threshold": 0.0,
        }
    )
    _FIXTURE_MODULE.write_json(candidate_path, profile)

    rc = rebuild_main(["--workspace", str(workspace), "--backup-db", "--threshold-profile", str(candidate_path)])
    assert rc == 0

    summary_1 = _FIXTURE_MODULE.read_workspace_rebuild_summary(workspace)
    assert summary_1 is not None
    assert summary_1["dry_run"] is False
    assert summary_1["imported_threshold_profile"] is True
    assert summary_1["update_profile_quantiles"] is False
    assert summary_1["executed_phase1_order"] == [
        "profile_resolve",
        "clear_identity_export_layers",
        "quality_backfill",
        "bootstrap_materialize",
        "prototype_ann_rebuild_optional",
        "summary",
    ]
    assert summary_1["optional_phase"]["prototype_ann_rebuild_optional"]["enabled"] is True
    assert summary_1["optional_phase"]["prototype_ann_rebuild_optional"]["status"] == "executed"
    assert summary_1["profile"]["profile_mode"] == "imported"
    assert summary_1["profile"]["active_threshold_profile_id"] == summary_1["threshold_profile_id"]
    assert summary_1["clear_execution"]["fk_break_updates"]["person.origin_cluster_id"] >= 1
    assert summary_1["clear_execution"]["fk_break_updates"]["auto_cluster.resolved_person_id"] >= 1
    assert "ops_event.export_run_id" in summary_1["clear_execution"]["fk_break_updates"]
    assert "ops_event.template_id" in summary_1["clear_execution"]["fk_break_updates"]
    assert summary_1["clear_execution"]["clear_targets"] == summary_1["cleared_counts"]
    assert summary_1["post_rebuild"]["active_threshold_profile"]["id"] == summary_1["threshold_profile_id"]
    assert summary_1["post_rebuild"]["materialized_cluster_count"] == summary_1["materialized_cluster_count"]
    assert summary_1["post_rebuild"]["review_pending_cluster_count"] == summary_1["review_pending_cluster_count"]
    assert summary_1["post_rebuild"]["discarded_cluster_count"] == summary_1["discarded_cluster_count"]

    conn2 = connect_db(workspace / ".hikbox" / "library.db")
    try:
        assert int(conn2.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        active = conn2.execute(
            "SELECT * FROM identity_threshold_profile WHERE active = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert active is not None
        assert float(active["area_log_p10"]) == -9.0
        assert float(active["area_log_p90"]) == -8.0
        assert float(active["sharpness_log_p10"]) == -2.0
        assert float(active["sharpness_log_p90"]) == -1.0

        person_count_1 = _count(conn2, "person")
        trusted_count_1 = _count(conn2, "person_trusted_sample")
        prototype_count_1 = _count(conn2, "person_prototype")

        assert person_count_1 >= 1
        assert trusted_count_1 >= 2
        assert prototype_count_1 >= 1

        assert _count(conn2, "person_face_exclusion") == 0
        assert _count(conn2, "export_template") == 0
        assert _count(conn2, "export_template_person") == 0
        assert _count(conn2, "export_run") == 0
        assert _count(conn2, "export_delivery") == 0
        assert _count(conn2, "ops_event") == ops_event_before
    finally:
        conn2.close()

    rc2 = rebuild_main(["--workspace", str(workspace), "--backup-db", "--threshold-profile", str(candidate_path)])
    assert rc2 == 0

    conn3 = sqlite3.connect(workspace / ".hikbox" / "library.db")
    conn3.row_factory = sqlite3.Row
    try:
        person_count_2 = _count(conn3, "person")
        trusted_count_2 = _count(conn3, "person_trusted_sample")
        prototype_count_2 = _count(conn3, "person_prototype")

        assert person_count_2 == person_count_1
        assert trusted_count_2 == trusted_count_1
        assert prototype_count_2 == prototype_count_1

        assert _count(conn3, "person_face_exclusion") == 0
        assert _count(conn3, "export_template") == 0
        assert _count(conn3, "export_template_person") == 0
        assert _count(conn3, "export_run") == 0
        assert _count(conn3, "export_delivery") == 0
    finally:
        conn3.close()


def test_rebuild_preserves_ops_event_and_nulls_export_fk_references(tmp_path: Path) -> None:
    workspace = tmp_path / "task5-ops-event-fk"
    build_seed_workspace_with_mock_embeddings(workspace)

    conn = connect_db(workspace / ".hikbox" / "library.db")
    export_run_id = 0
    template_id = 0
    event_id = 0
    try:
        seed_active_identity_threshold_profile(conn)
        template_row = conn.execute("SELECT id FROM export_template ORDER BY id ASC LIMIT 1").fetchone()
        assert template_row is not None
        template_id = int(template_row["id"])
        export_run_id = int(
            conn.execute(
                """
                INSERT INTO export_run(template_id, spec_hash, status, started_at)
                VALUES (?, ?, 'running', CURRENT_TIMESTAMP)
                """,
                (template_id, "task5-ops-event-spec"),
            ).lastrowid
        )
        event_id = int(
            conn.execute(
                """
                INSERT INTO ops_event(
                    level,
                    component,
                    event_type,
                    run_kind,
                    run_id,
                    export_run_id,
                    template_id,
                    message
                )
                VALUES ('info', 'test', 'task5_ops_event_fk', 'export', 'task5-run', ?, ?, 'task5')
                """,
                (export_run_id, template_id),
            ).lastrowid
        )
        conn.commit()
    finally:
        conn.close()

    rc = rebuild_main(["--workspace", str(workspace), "--backup-db"])
    assert rc == 0

    conn2 = connect_db(workspace / ".hikbox" / "library.db")
    try:
        event_row = conn2.execute(
            "SELECT id, export_run_id, template_id FROM ops_event WHERE id = ?",
            (event_id,),
        ).fetchone()
        assert event_row is not None
        assert event_row["export_run_id"] is None
        assert event_row["template_id"] is None
        assert conn2.execute("SELECT id FROM export_run WHERE id = ?", (export_run_id,)).fetchone() is None
        assert conn2.execute("SELECT id FROM export_template WHERE id = ?", (template_id,)).fetchone() is None
    finally:
        conn2.close()

    summary = _FIXTURE_MODULE.read_workspace_rebuild_summary(workspace)
    assert summary is not None
    assert summary["clear_execution"]["fk_break_updates"]["ops_event.export_run_id"] >= 1
    assert summary["clear_execution"]["fk_break_updates"]["ops_event.template_id"] >= 1


def test_rebuild_failure_rolls_back_without_half_state(tmp_path: Path) -> None:
    workspace = tmp_path / "task5-rollback"
    build_seed_workspace_with_mock_embeddings(workspace)

    conn = connect_db(workspace / ".hikbox" / "library.db")
    profile_count_before = 0
    active_profile_id_before: int | None = None
    person_count_before = 0
    assignment_count_before = 0
    try:
        seed_active_identity_threshold_profile(conn)
        profile_count_before = _count(conn, "identity_threshold_profile")
        active_profile_id_before = _active_profile_id(conn)
        person_count_before = _count(conn, "person")
        assignment_count_before = _count(conn, "person_face_assignment")

        row = conn.execute(
            """
            SELECT fo.id AS observation_id, fo.photo_asset_id
            FROM face_observation fo
            ORDER BY fo.id ASC
            LIMIT 1
            """
        ).fetchone()
        assert row is not None
        obs_id = int(row["observation_id"])
        photo_id = int(row["photo_asset_id"])
        conn.execute("UPDATE face_observation SET crop_path = NULL WHERE id = ?", (obs_id,))
        conn.execute("UPDATE photo_asset SET primary_path = ? WHERE id = ?", ("/tmp/task5-missing-original.jpg", photo_id))
        conn.commit()
    finally:
        conn.close()

    candidate_path = workspace / ".tmp" / "task5" / "rollback-candidate.json"
    candidate = _FIXTURE_MODULE.build_identity_profile_candidate_from_active_db(workspace)
    candidate["profile_name"] = "task5-rollback-candidate"
    _FIXTURE_MODULE.write_json(candidate_path, candidate)

    rc = rebuild_main(["--workspace", str(workspace), "--threshold-profile", str(candidate_path)])
    assert rc == 1

    conn2 = connect_db(workspace / ".hikbox" / "library.db")
    try:
        assert _count(conn2, "person") == person_count_before
        assert _count(conn2, "person_face_assignment") == assignment_count_before
        assert _count(conn2, "identity_threshold_profile") == profile_count_before
        assert _active_profile_id(conn2) == active_profile_id_before
    finally:
        conn2.close()
