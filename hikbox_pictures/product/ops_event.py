from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hikbox_pictures.product.db.connection import connect_sqlite


ALLOWED_SEVERITIES = {"info", "warning", "error"}


@dataclass(frozen=True)
class OpsEvent:
    id: int
    event_type: str
    severity: str
    scan_session_id: int | None
    export_run_id: int | None
    payload_json: dict[str, Any]
    created_at: str


class OpsEventService:
    def __init__(self, library_db_path: Path) -> None:
        self._library_db_path = library_db_path

    def record_event(
        self,
        *,
        event_type: str,
        severity: str,
        scan_session_id: int | None = None,
        export_run_id: int | None = None,
        payload: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> OpsEvent:
        clean_event_type = event_type.strip()
        if not clean_event_type:
            raise ValueError("event_type 不能为空")
        if severity not in ALLOWED_SEVERITIES:
            raise ValueError(f"不支持的 severity: {severity}")
        now = created_at or _utc_now()
        payload_json = payload or {}
        scan_session_id_value = int(scan_session_id) if scan_session_id is not None else None
        export_run_id_value = int(export_run_id) if export_run_id is not None else None
        payload_text = json.dumps(payload_json, ensure_ascii=False)
        with connect_sqlite(self._library_db_path) as conn:
            _ensure_ops_event_schema(conn)
            if export_run_id_value is None:
                cursor = conn.execute(
                    """
                    INSERT INTO ops_event(
                        event_type,
                        severity,
                        scan_session_id,
                        export_run_id,
                        payload_json,
                        created_at
                    )
                    VALUES (?, ?, ?, NULL, ?, ?)
                    """,
                    (
                        clean_event_type,
                        severity,
                        scan_session_id_value,
                        payload_text,
                        now,
                    ),
                )
            else:
                _ensure_export_run_table_exists(conn=conn, export_run_id=export_run_id_value)
                cursor = conn.execute(
                    """
                    INSERT INTO ops_event(
                        event_type,
                        severity,
                        scan_session_id,
                        export_run_id,
                        payload_json,
                        created_at
                    )
                    SELECT ?, ?, ?, ?, ?, ?
                    WHERE EXISTS (
                        SELECT 1 FROM export_run WHERE id=?
                    )
                    """,
                    (
                        clean_event_type,
                        severity,
                        scan_session_id_value,
                        export_run_id_value,
                        payload_text,
                        now,
                        export_run_id_value,
                    ),
                )
                changes_row = conn.execute("SELECT changes()").fetchone()
                changed_count = int(changes_row[0]) if changes_row is not None else 0
                if changed_count == 0:
                    raise ValueError(f"export_run 不存在: id={export_run_id_value}")
            event_id = int(cursor.lastrowid)
            row = conn.execute(
                """
                SELECT id, event_type, severity, scan_session_id, export_run_id, payload_json, created_at
                FROM ops_event
                WHERE id=?
                """,
                (event_id,),
            ).fetchone()
            conn.commit()
        assert row is not None
        return _row_to_ops_event(row)

    def query_events(
        self,
        *,
        scan_session_id: int | None = None,
        export_run_id: int | None = None,
        severity: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[OpsEvent]:
        if limit <= 0:
            raise ValueError(f"limit 必须 > 0: {limit}")
        if offset < 0:
            raise ValueError(f"offset 必须 >= 0: {offset}")
        if severity is not None and severity not in ALLOWED_SEVERITIES:
            raise ValueError(f"不支持的 severity: {severity}")
        if event_type is not None and not event_type.strip():
            raise ValueError("event_type 不能为空字符串")

        conditions: list[str] = []
        params: list[object] = []
        if scan_session_id is not None:
            conditions.append("scan_session_id=?")
            params.append(int(scan_session_id))
        if export_run_id is not None:
            conditions.append("export_run_id=?")
            params.append(int(export_run_id))
        if severity is not None:
            conditions.append("severity=?")
            params.append(severity)
        if event_type is not None:
            conditions.append("event_type=?")
            params.append(event_type.strip())

        sql = """
            SELECT id, event_type, severity, scan_session_id, export_run_id, payload_json, created_at
            FROM ops_event
        """
        if conditions:
            sql += f" WHERE {' AND '.join(conditions)}"
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])

        with connect_sqlite(self._library_db_path) as conn:
            _ensure_ops_event_schema(conn)
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [_row_to_ops_event(row) for row in rows]


def _ensure_ops_event_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ops_event (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          event_type TEXT NOT NULL,
          severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
          scan_session_id INTEGER REFERENCES scan_session(id),
          export_run_id INTEGER,
          payload_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_ops_event_type_created
        ON ops_event(event_type, created_at);

        CREATE INDEX IF NOT EXISTS idx_ops_event_scan
        ON ops_event(scan_session_id);

        CREATE INDEX IF NOT EXISTS idx_ops_event_export
        ON ops_event(export_run_id);
        """
    )


def _ensure_export_run_table_exists(*, conn: sqlite3.Connection, export_run_id: int) -> None:
    table_row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type='table'
          AND name='export_run'
        LIMIT 1
        """
    ).fetchone()
    if table_row is None:
        raise ValueError(f"export_run 表不存在，无法校验 id: {int(export_run_id)}")


def _row_to_ops_event(row: sqlite3.Row | tuple[object, ...]) -> OpsEvent:
    return OpsEvent(
        id=int(row[0]),
        event_type=str(row[1]),
        severity=str(row[2]),
        scan_session_id=int(row[3]) if row[3] is not None else None,
        export_run_id=int(row[4]) if row[4] is not None else None,
        payload_json=_parse_json_dict(row[5]),
        created_at=str(row[6]),
    )


def _parse_json_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
