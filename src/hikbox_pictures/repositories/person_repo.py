from __future__ import annotations

from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]


class PersonRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create_person(
        self,
        display_name: str,
        status: str = "active",
        confirmed: bool = False,
        ignored: bool = False,
        notes: str | None = None,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO person(display_name, status, confirmed, ignored, notes)
            VALUES (?, ?, ?, ?, ?)
            """,
            (display_name, status, 1 if confirmed else 0, 1 if ignored else 0, notes),
        )
        return int(cursor.lastrowid)

    def get_person(self, person_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT id, display_name, status, confirmed, ignored, notes, merged_into_person_id, created_at, updated_at
            FROM person
            WHERE id = ?
            """,
            (int(person_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_people(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT id, display_name, status, confirmed, ignored, notes, merged_into_person_id, created_at, updated_at
            FROM person
            ORDER BY id ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_merged(self, source_person_id: int, target_person_id: int) -> int:
        cursor = self.conn.execute(
            """
            UPDATE person
            SET status = 'merged',
                merged_into_person_id = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status = 'active'
            """,
            (int(target_person_id), int(source_person_id)),
        )
        return int(cursor.rowcount)

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM person").fetchone()
        return int(row["c"])
