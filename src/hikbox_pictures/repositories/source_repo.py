from __future__ import annotations

from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]


class SourceRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add_source(
        self,
        name: str,
        root_path: str,
        root_fingerprint: str | None = None,
        active: bool = True,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO library_source(name, root_path, root_fingerprint, active)
            VALUES (?, ?, ?, ?)
            """,
            (name, root_path, root_fingerprint, 1 if active else 0),
        )
        return int(cursor.lastrowid)

    def get_source(self, source_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT id, name, root_path, root_fingerprint, active, created_at, updated_at
            FROM library_source
            WHERE id = ?
            """,
            (int(source_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_sources(self, active: bool | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT id, name, root_path, root_fingerprint, active, created_at, updated_at "
            "FROM library_source"
        )
        params: tuple[int, ...] | tuple[()] = ()
        if active is not None:
            sql += " WHERE active = ?"
            params = (1 if active else 0,)

        sql += " ORDER BY id ASC"
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def list_active_source_ids(self) -> list[int]:
        rows = self.conn.execute(
            """
            SELECT id
            FROM library_source
            WHERE active = 1
            ORDER BY id ASC
            """
        ).fetchall()
        return [int(row["id"]) for row in rows]

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM library_source").fetchone()
        return int(row["c"])
