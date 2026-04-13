from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.cli import main
from hikbox_pictures.services.action_service import ActionService
from hikbox_pictures.services.observability_service import ObservabilityService
from hikbox_pictures.services.scan_orchestrator import ScanOrchestrator

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace_logs_tail", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_seed_workspace = _MODULE.build_seed_workspace


def test_observability_service_tail_run_log_with_limit(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        service = ObservabilityService(ws.conn, workspace=ws.root)
        for idx in range(5):
            service.emit_event(
                level="info",
                component="scanner",
                event_type=f"scan.session.step_{idx}",
                run_kind="scan",
                run_id="scan-77",
                message=f"scan step {idx}",
            )

        rows = service.tail_run_logs(run_kind="scan", run_id="scan-77", limit=3)

        assert [row["event_type"] for row in rows] == [
            "scan.session.step_2",
            "scan.session.step_3",
            "scan.session.step_4",
        ]
        assert all(row["run_kind"] == "scan" for row in rows)
        assert all(row["run_id"] == "scan-77" for row in rows)
    finally:
        ws.close()


def test_emit_event_persists_after_reopen_connection(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    db_path = ws.paths.db_path
    try:
        service = ObservabilityService(ws.conn, workspace=ws.root)
        event_id = service.emit_event(
            level="info",
            component="scanner",
            event_type="scan.session.started",
            run_kind="scan",
            run_id="scan-201",
            message="scan started",
        )
        assert event_id is not None
    finally:
        ws.close()

    reopened = connect_db(db_path)
    try:
        row = reopened.execute(
            """
            SELECT id, event_type, run_kind, run_id
            FROM ops_event
            WHERE id = ?
            """,
            (int(event_id),),
        ).fetchone()
        assert row is not None
        assert row["event_type"] == "scan.session.started"
        assert row["run_kind"] == "scan"
        assert row["run_id"] == "scan-201"
    finally:
        reopened.close()


def test_emit_event_writes_run_jsonl_and_app_log_with_core_fields(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        service = ObservabilityService(ws.conn, workspace=ws.root)
        service.emit_event(
            level="warning",
            component="exporter",
            event_type="export.delivery.skipped",
            run_kind="export",
            run_id="export-501",
            message="export skipped",
            detail={"status": "skipped", "phase": "delivery"},
        )

        run_log_path = ws.paths.logs_dir / "runs" / "export-export-501.jsonl"
        app_log_path = ws.paths.logs_dir / "app.log"
        assert run_log_path.exists()
        assert app_log_path.exists()

        run_payload = json.loads(run_log_path.read_text(encoding="utf-8").strip().splitlines()[-1])
        app_payload = json.loads(app_log_path.read_text(encoding="utf-8").strip().splitlines()[-1])

        expected_keys = {
            "ts",
            "level",
            "event_type",
            "component",
            "run_kind",
            "run_id",
            "message",
            "phase",
            "status",
            "duration_ms",
            "error_code",
            "error_type",
            "error_message",
            "error_stack",
        }
        assert expected_keys.issubset(run_payload.keys())
        assert expected_keys.issubset(app_payload.keys())
        assert run_payload["phase"] == "delivery"
        assert run_payload["status"] == "skipped"
        assert run_payload["duration_ms"] is None
        assert run_payload["error_code"] is None
    finally:
        ws.close()


def test_observability_service_prune_ops_event_by_days(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        ws.conn.execute(
            """
            INSERT INTO ops_event(occurred_at, level, component, event_type, run_kind, run_id, message)
            VALUES (datetime('now', '-40 days'), 'info', 'seed', 'seed.old_event', 'scan', 'scan-old', 'old')
            """
        )
        ws.conn.execute(
            """
            INSERT INTO ops_event(occurred_at, level, component, event_type, run_kind, run_id, message)
            VALUES (datetime('now'), 'info', 'seed', 'seed.new_event', 'scan', 'scan-new', 'new')
            """
        )
        ws.conn.commit()

        service = ObservabilityService(ws.conn, workspace=ws.root)
        deleted = service.prune_ops_events(days=7)
        rows = service.list_events(limit=100)

        assert deleted >= 1
        assert all(row["event_type"] != "seed.old_event" for row in rows)
        assert any(row["event_type"] == "seed.new_event" for row in rows)
    finally:
        ws.close()


def test_observability_service_prune_ops_event_in_batches(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        for idx in range(7):
            ws.conn.execute(
                """
                INSERT INTO ops_event(occurred_at, level, component, event_type, run_kind, run_id, message)
                VALUES (datetime('now', '-60 days'), 'info', 'seed', ?, 'scan', ?, 'old')
                """,
                (f"seed.old_batch_{idx}", f"scan-old-{idx}"),
            )
        ws.conn.commit()

        service = ObservabilityService(ws.conn, workspace=ws.root)
        deleted = service.prune_ops_events(days=30, batch_size=3)

        remaining_old = ws.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM ops_event
            WHERE event_type LIKE 'seed.old_batch_%'
            """
        ).fetchone()
        assert deleted == 7
        assert remaining_old is not None
        assert int(remaining_old["c"]) == 0
    finally:
        ws.close()


def test_observability_service_prune_run_logs_keeps_latest_200(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        runs_dir = ws.paths.logs_dir / "runs"
        now = datetime.now(timezone.utc)
        for idx in range(240):
            path = runs_dir / f"scan-run-{idx:03d}.jsonl"
            payload = {
                "ts": now.isoformat(timespec="seconds"),
                "level": "info",
                "event_type": "scan.session.checkpoint",
                "component": "scanner",
                "run_kind": "scan",
                "run_id": f"run-{idx:03d}",
                "phase": None,
                "status": None,
                "duration_ms": None,
                "error_code": None,
                "error_type": None,
                "error_message": None,
                "error_stack": None,
            }
            path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
            old_timestamp = (now - timedelta(days=45, minutes=idx)).timestamp()
            os.utime(path, (old_timestamp, old_timestamp))

        service = ObservabilityService(ws.conn, workspace=ws.root)
        deleted = service.prune_ops_events(days=30)
        assert deleted >= 0

        remaining_files = sorted(runs_dir.glob("*.jsonl"))
        assert len(remaining_files) == 200
        assert not (runs_dir / "scan-run-239.jsonl").exists()
        assert (runs_dir / "scan-run-000.jsonl").exists()
    finally:
        ws.close()


def test_cli_logs_tail_and_prune_commands(tmp_path, capsys) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        service = ObservabilityService(ws.conn, workspace=ws.root)
        for idx in range(3):
            service.emit_event(
                level="info",
                component="exporter",
                event_type=f"export.delivery.step_{idx}",
                run_kind="export",
                run_id="export-55",
                message=f"export step {idx}",
            )

        ws.conn.execute(
            """
            INSERT INTO ops_event(occurred_at, level, component, event_type, run_kind, run_id, message)
            VALUES (datetime('now', '-120 days'), 'info', 'seed', 'very_old', 'scan', 'scan-old', 'old')
            """
        )
        ws.conn.commit()

        rc_tail = main(
            [
                "logs",
                "tail",
                "--workspace",
                str(ws.root),
                "--run-kind",
                "export",
                "--run-id",
                "export-55",
                "--limit",
                "2",
            ]
        )
        assert rc_tail == 0
        out_tail = capsys.readouterr().out
        assert "export.delivery.step_1" in out_tail
        assert "export.delivery.step_2" in out_tail

        rc_prune = main(["logs", "prune", "--workspace", str(ws.root), "--days", "90"])
        assert rc_prune == 0
        out_prune = capsys.readouterr().out
        assert "pruned=" in out_prune
    finally:
        ws.close()


def test_scan_start_or_resume_emits_key_event_and_visible_in_ops_event(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        session_id = ScanOrchestrator(ws.conn).start_or_resume()
        rows = ObservabilityService(ws.conn, workspace=ws.root).list_events(
            limit=50,
            run_kind="scan",
            run_id=str(session_id),
        )

        event_types = {str(row["event_type"]) for row in rows}
        assert "scan.session.started" in event_types or "scan.session.resumed" in event_types
    finally:
        ws.close()


def test_export_run_emits_started_and_terminal_events_in_ops_and_run_jsonl(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_export_assets=True)
    try:
        summary = ActionService(ws.conn).run_export_template(template_id=ws.export_template_id)
        run_id = str(summary["run_id"])
        observability = ObservabilityService(ws.conn, workspace=ws.root)

        rows = observability.list_events(limit=200, run_kind="export", run_id=run_id)
        event_types = {str(row["event_type"]) for row in rows}
        assert "export.delivery.started" in event_types
        assert "export.delivery.completed" in event_types or "export.delivery.failed" in event_types

        run_rows = observability.tail_run_logs(run_kind="export", run_id=run_id, limit=500)
        run_event_types = {str(row["event_type"]) for row in run_rows}
        assert "export.delivery.started" in run_event_types
        assert "export.delivery.completed" in run_event_types or "export.delivery.failed" in run_event_types
    finally:
        ws.close()


def test_export_stale_marked_event_in_run_log_contains_run_id(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_export_assets=True)
    try:
        first = ActionService(ws.conn).run_export_template(template_id=ws.export_template_id)
        first_spec_hash = str(first["spec_hash"])

        delivery = ws.conn.execute(
            """
            SELECT photo_asset_id
            FROM export_delivery
            WHERE template_id = ?
              AND spec_hash = ?
              AND asset_variant = 'primary'
              AND status IN ('ok', 'skipped')
            ORDER BY id ASC
            LIMIT 1
            """,
            (int(ws.export_template_id), first_spec_hash),
        ).fetchone()
        assert delivery is not None
        target_asset_id = int(delivery["photo_asset_id"])

        ws.conn.execute(
            """
            UPDATE person_face_assignment
            SET active = 0
            WHERE face_observation_id IN (
                SELECT id FROM face_observation WHERE photo_asset_id = ?
            )
            """,
            (target_asset_id,),
        )
        ws.conn.commit()

        second = ActionService(ws.conn).run_export_template(template_id=ws.export_template_id)
        second_run_id = str(second["run_id"])
        observability = ObservabilityService(ws.conn, workspace=ws.root)
        stale_rows = [
            row
            for row in observability.tail_run_logs(run_kind="export", run_id=second_run_id, limit=500)
            if str(row.get("event_type")) == "export.delivery.stale_marked"
            and str(row.get("status")) == "stale_marked"
        ]

        assert stale_rows
        assert all(str(row.get("run_id")) == second_run_id for row in stale_rows)
    finally:
        ws.close()
