from __future__ import annotations

import sqlite3
from pathlib import Path

from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.ops_event import OpsEventService
import pytest


def _insert_scan_session(db_path: Path, started_at: str, *, status: str = "running") -> int:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
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
            VALUES ('scan_full', ?, 'manual_cli', NULL, ?, NULL, NULL, ?, ?)
            """,
            (status, started_at, started_at, started_at),
        )
        conn.commit()
        return int(cursor.lastrowid)


def _ensure_export_run_row(db_path: Path, export_run_id: int) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS export_run (
              id INTEGER PRIMARY KEY
            )
            """,
        )
        conn.execute(
            "INSERT INTO export_run(id) VALUES (?) ON CONFLICT(id) DO NOTHING",
            (export_run_id,),
        )
        conn.commit()


def test_filter_by_scan_session_and_export_run(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_a = _insert_scan_session(layout.library_db_path, "2026-04-22T00:00:00+00:00")
    scan_b = _insert_scan_session(layout.library_db_path, "2026-04-22T00:01:00+00:00", status="completed")
    _ensure_export_run_row(layout.library_db_path, 1001)
    service = OpsEventService(layout.library_db_path)

    service.record_event(
        event_type="scan_progress",
        severity="warning",
        scan_session_id=scan_a,
        payload={"seq": 1},
        created_at="2026-04-22T00:00:11+00:00",
    )
    service.record_event(
        event_type="export_progress",
        severity="warning",
        scan_session_id=scan_a,
        export_run_id=1001,
        payload={"seq": 2},
        created_at="2026-04-22T00:00:12+00:00",
    )
    service.record_event(
        event_type="export_progress",
        severity="warning",
        scan_session_id=scan_b,
        export_run_id=1001,
        payload={"seq": 3},
        created_at="2026-04-22T00:00:13+00:00",
    )

    events = service.query_events(scan_session_id=scan_a, export_run_id=1001)

    assert [event.payload_json["seq"] for event in events] == [2]


def test_query_supports_severity_event_type_and_pagination(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_id = _insert_scan_session(layout.library_db_path, "2026-04-22T00:00:00+00:00")
    service = OpsEventService(layout.library_db_path)

    service.record_event(
        event_type="scan_progress",
        severity="warning",
        scan_session_id=scan_id,
        payload={"seq": 1},
        created_at="2026-04-22T00:00:11+00:00",
    )
    service.record_event(
        event_type="scan_progress",
        severity="info",
        scan_session_id=scan_id,
        payload={"seq": 2},
        created_at="2026-04-22T00:00:12+00:00",
    )
    service.record_event(
        event_type="scan_progress",
        severity="warning",
        scan_session_id=scan_id,
        payload={"seq": 3},
        created_at="2026-04-22T00:00:13+00:00",
    )
    service.record_event(
        event_type="worker_ready",
        severity="warning",
        scan_session_id=scan_id,
        payload={"seq": 4},
        created_at="2026-04-22T00:00:14+00:00",
    )

    page_1 = service.query_events(
        severity="warning",
        event_type="scan_progress",
        limit=1,
        offset=0,
    )
    page_2 = service.query_events(
        severity="warning",
        event_type="scan_progress",
        limit=1,
        offset=1,
    )

    assert [event.payload_json["seq"] for event in page_1] == [3]
    assert [event.payload_json["seq"] for event in page_2] == [1]


def test_record_event_raises_when_export_run_id_not_exists(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_id = _insert_scan_session(layout.library_db_path, "2026-04-22T00:00:00+00:00")
    _ensure_export_run_row(layout.library_db_path, 2001)
    service = OpsEventService(layout.library_db_path)

    with pytest.raises(ValueError, match="export_run 不存在"):
        service.record_event(
            event_type="export_progress",
            severity="warning",
            scan_session_id=scan_id,
            export_run_id=9999,
            payload={"seq": 7},
            created_at="2026-04-22T00:00:17+00:00",
        )

    with sqlite3.connect(layout.library_db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM ops_event").fetchone()
    assert row is not None
    assert int(row[0]) == 0


def test_record_event_raises_when_export_run_table_missing(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_id = _insert_scan_session(layout.library_db_path, "2026-04-22T00:00:00+00:00")
    service = OpsEventService(layout.library_db_path)

    with pytest.raises(ValueError, match="export_run 表不存在"):
        service.record_event(
            event_type="export_progress",
            severity="warning",
            scan_session_id=scan_id,
            export_run_id=1234,
            payload={"seq": 8},
            created_at="2026-04-22T00:00:18+00:00",
        )

    with sqlite3.connect(layout.library_db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM ops_event").fetchone()
    assert row is not None
    assert int(row[0]) == 0
