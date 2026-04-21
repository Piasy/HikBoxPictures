from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from hikbox_pictures.product.db.connection import connect_sqlite


@dataclass(frozen=True)
class LibrarySource:
    id: int
    root_path: str
    label: str
    enabled: bool
    status: str
    last_discovered_at: str | None
    created_at: str
    updated_at: str


class SQLiteSourceRepository:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def create_source(self, *, root_path: str, label: str, now: str) -> LibrarySource:
        try:
            with connect_sqlite(self._db_path) as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO library_source(
                        root_path,
                        label,
                        enabled,
                        status,
                        last_discovered_at,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, 1, 'active', NULL, ?, ?)
                    """,
                    (root_path, label, now, now),
                )
                source_id = int(cursor.lastrowid)
                row = conn.execute(
                    "SELECT * FROM library_source WHERE id=?",
                    (source_id,),
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"root_path 已存在: {root_path}") from exc

        assert row is not None
        return _row_to_source(row)

    def get_source(self, source_id: int, *, include_deleted: bool = True) -> LibrarySource | None:
        sql = "SELECT * FROM library_source WHERE id=?"
        params: tuple[object, ...] = (source_id,)
        if not include_deleted:
            sql += " AND status <> 'deleted'"
        with connect_sqlite(self._db_path) as conn:
            row = conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return _row_to_source(row)

    def list_sources(self, *, include_deleted: bool = False) -> list[LibrarySource]:
        sql = "SELECT * FROM library_source"
        if not include_deleted:
            sql += " WHERE status <> 'deleted'"
        sql += " ORDER BY id"
        with connect_sqlite(self._db_path) as conn:
            rows = conn.execute(sql).fetchall()
        return [_row_to_source(row) for row in rows]

    def update_source(
        self,
        source_id: int,
        *,
        label: str | None = None,
        enabled: bool | None = None,
        status: str | None = None,
        now: str,
    ) -> LibrarySource:
        current = self.get_source(source_id, include_deleted=True)
        if current is None:
            raise ValueError(f"source 不存在: id={source_id}")

        next_label = current.label if label is None else label
        next_enabled = current.enabled if enabled is None else enabled
        next_status = current.status if status is None else status

        with connect_sqlite(self._db_path) as conn:
            conn.execute(
                """
                UPDATE library_source
                SET label=?, enabled=?, status=?, updated_at=?
                WHERE id=?
                """,
                (next_label, int(next_enabled), next_status, now, source_id),
            )
            row = conn.execute(
                "SELECT * FROM library_source WHERE id=?",
                (source_id,),
            ).fetchone()
        assert row is not None
        return _row_to_source(row)


def _row_to_source(row: sqlite3.Row | tuple[object, ...]) -> LibrarySource:
    return LibrarySource(
        id=int(row[0]),
        root_path=str(row[1]),
        label=str(row[2]),
        enabled=bool(row[3]),
        status=str(row[4]),
        last_discovered_at=str(row[5]) if row[5] is not None else None,
        created_at=str(row[6]),
        updated_at=str(row[7]),
    )
