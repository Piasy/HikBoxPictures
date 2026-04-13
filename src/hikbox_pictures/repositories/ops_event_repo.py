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

    def list_recent(
        self,
        limit: int = 50,
        *,
        run_kind: str | None = None,
        event_type: str | None = None,
        run_id: str | None = None,
        level: str | None = None,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 1000))
        where_clauses: list[str] = []
        params: list[Any] = []
        if run_kind is not None:
            where_clauses.append("run_kind = ?")
            params.append(run_kind)
        if event_type is not None:
            where_clauses.append("event_type = ?")
            params.append(event_type)
        if run_id is not None:
            where_clauses.append("run_id = ?")
            params.append(run_id)
        if level is not None:
            where_clauses.append("level = ?")
            params.append(level)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        params.append(safe_limit)
        rows = self.conn.execute(
            f"""
            SELECT id, occurred_at, level, component, event_type, run_kind, run_id, message, detail_json
            FROM ops_event
            {where_sql}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def prune_older_than_days(self, *, days: int, batch_size: int = 5000) -> int:
        safe_days = max(1, int(days))
        safe_batch_size = max(1, min(int(batch_size), 5000))
        total_deleted = 0

        while True:
            cursor = self.conn.execute(
                """
                DELETE FROM ops_event
                WHERE id IN (
                    SELECT id
                    FROM ops_event
                    WHERE occurred_at < datetime('now', ?)
                    ORDER BY id ASC
                    LIMIT ?
                )
                """,
                (f"-{safe_days} days", safe_batch_size),
            )
            batch_deleted = max(0, int(cursor.rowcount))
            total_deleted += batch_deleted
            if batch_deleted < safe_batch_size:
                break
        return total_deleted

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM ops_event").fetchone()
        return int(row["c"])
