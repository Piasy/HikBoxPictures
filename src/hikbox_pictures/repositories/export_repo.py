from __future__ import annotations

from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]


class ExportRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create_template(
        self,
        name: str,
        output_root: str,
        include_group: bool = True,
        export_live_mov: bool = False,
        enabled: bool = True,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO export_template(name, output_root, include_group, export_live_mov, enabled)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                name,
                output_root,
                1 if include_group else 0,
                1 if export_live_mov else 0,
                1 if enabled else 0,
            ),
        )
        return int(cursor.lastrowid)

    def add_template_person(self, template_id: int, person_id: int, position: int) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO export_template_person(template_id, person_id, position)
            VALUES (?, ?, ?)
            """,
            (int(template_id), int(person_id), int(position)),
        )
        return int(cursor.lastrowid)

    def list_templates(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT id, name, output_root, include_group, export_live_mov, enabled, created_at, updated_at
            FROM export_template
            ORDER BY id ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def count_templates(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM export_template").fetchone()
        return int(row["c"])
