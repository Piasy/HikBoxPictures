"""运维事件记录与查询。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hikbox_pictures.product.db.connection import connect_sqlite

INTERNAL_EVENT_TYPES = {"audit.freeze"}


@dataclass(frozen=True)
class OpsEventRecord:
    id: int
    event_type: str
    severity: str
    scan_session_id: int | None
    export_run_id: int | None
    payload: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class OpsEventPage:
    items: list[OpsEventRecord]
    limit: int
    before_id: int | None
    next_before_id: int | None


class OpsEventService:
    """提供 ops_event 的记录与分页查询接口。"""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)

    def record_event(
        self,
        *,
        event_type: str,
        severity: str,
        payload: dict[str, Any],
        scan_session_id: int | None = None,
        export_run_id: int | None = None,
    ) -> OpsEventRecord:
        conn = connect_sqlite(self._db_path)
        try:
            cursor = conn.execute(
                """
                INSERT INTO ops_event(
                  event_type, severity, scan_session_id, export_run_id, payload_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(event_type),
                    str(severity),
                    None if scan_session_id is None else int(scan_session_id),
                    None if export_run_id is None else int(export_run_id),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            )
            conn.commit()
            row = conn.execute(
                """
                SELECT
                  id,
                  event_type,
                  severity,
                  scan_session_id,
                  export_run_id,
                  payload_json,
                  created_at
                FROM ops_event
                WHERE id=?
                """,
                (int(cursor.lastrowid),),
            ).fetchone()
            if row is None:
                raise RuntimeError("ops_event 插入后读取失败")
            return self._row_to_record(row)
        finally:
            conn.close()

    def query_events(
        self,
        *,
        scan_session_id: int | None = None,
        export_run_id: int | None = None,
        severity: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
        before_id: int | None = None,
    ) -> OpsEventPage:
        safe_limit = max(1, int(limit))
        filters: list[str] = []
        params: list[object] = []
        if scan_session_id is not None:
            filters.append("scan_session_id=?")
            params.append(int(scan_session_id))
        if export_run_id is not None:
            filters.append("export_run_id=?")
            params.append(int(export_run_id))
        if severity is not None:
            filters.append("severity=?")
            params.append(str(severity))
        if event_type is not None:
            filters.append("event_type=?")
            params.append(str(event_type))
        else:
            placeholders = ", ".join("?" for _ in sorted(INTERNAL_EVENT_TYPES))
            filters.append(f"event_type NOT IN ({placeholders})")
            params.extend(sorted(INTERNAL_EVENT_TYPES))
        if before_id is not None:
            filters.append("id < ?")
            params.append(int(before_id))
        where_sql = ""
        if filters:
            where_sql = "WHERE " + " AND ".join(filters)

        conn = connect_sqlite(self._db_path)
        try:
            rows = conn.execute(
                f"""
                SELECT
                  id,
                  event_type,
                  severity,
                  scan_session_id,
                  export_run_id,
                  payload_json,
                  created_at
                FROM ops_event
                {where_sql}
                ORDER BY id DESC
                LIMIT ?
                """,
                (*params, safe_limit + 1),
            ).fetchall()
        finally:
            conn.close()

        has_more = len(rows) > safe_limit
        page_rows = rows[:safe_limit]
        return OpsEventPage(
            items=[self._row_to_record(row) for row in page_rows],
            limit=safe_limit,
            before_id=None if before_id is None else int(before_id),
            next_before_id=(int(page_rows[-1][0]) if has_more and page_rows else None),
        )

    def _row_to_record(self, row: sqlite3.Row | tuple[Any, ...]) -> OpsEventRecord:
        return OpsEventRecord(
            id=int(row[0]),
            event_type=str(row[1]),
            severity=str(row[2]),
            scan_session_id=None if row[3] is None else int(row[3]),
            export_run_id=None if row[4] is None else int(row[4]),
            payload=json.loads(str(row[5])),
            created_at=str(row[6]),
        )
