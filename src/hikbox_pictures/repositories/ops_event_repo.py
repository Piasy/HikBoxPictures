from __future__ import annotations

from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]


class OpsEventRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def append_event(
        self,
        level: str,
        component: str,
        event_type: str,
        message: str | None = None,
        detail_json: str | None = None,
        run_kind: str | None = None,
        run_id: str | None = None,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO ops_event(level, component, event_type, message, detail_json, run_kind, run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (level, component, event_type, message, detail_json, run_kind, run_id),
        )
        return int(cursor.lastrowid)

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 1000))
        rows = self.conn.execute(
            """
            SELECT id, occurred_at, level, component, event_type, run_kind, run_id, message, detail_json
            FROM ops_event
            ORDER BY id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM ops_event").fetchone()
        return int(row["c"])
