from __future__ import annotations

from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.repositories import PersonRepo


class ActionService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.person_repo = PersonRepo(conn)

    def rename_person(self, person_id: int, display_name: str) -> dict[str, Any]:
        clean_name = display_name.strip()
        if not clean_name:
            raise ValueError("display_name 不能为空")

        cursor = self.conn.execute(
            "UPDATE person SET display_name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (clean_name, int(person_id)),
        )
        if cursor.rowcount == 0:
            self.conn.rollback()
            raise LookupError(f"person {person_id} 不存在")

        self.conn.commit()
        row = self.person_repo.get_person(int(person_id))
        if row is None:
            raise LookupError(f"person {person_id} 不存在")
        return {
            "id": row["id"],
            "display_name": row["display_name"],
            "status": row["status"],
            "confirmed": bool(row["confirmed"]),
            "ignored": bool(row["ignored"]),
            "notes": row["notes"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
