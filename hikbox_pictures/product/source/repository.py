"""图库 source 仓储层。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from hikbox_pictures.product.db.connection import connect_sqlite


@dataclass(frozen=True)
class SourceRecord:
    id: int
    root_path: str
    label: str
    enabled: bool
    removed_at: str | None
    created_at: str
    updated_at: str


class SourceRepository:
    """`library_source` 表数据访问。"""

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)

    def insert_source(self, *, root_path: str, label: str) -> SourceRecord:
        conn = connect_sqlite(self._db_path)
        try:
            cursor = conn.execute(
                """
                INSERT INTO library_source(root_path, label, enabled, created_at, updated_at)
                VALUES (?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (root_path, label),
            )
            conn.commit()
            source_id = int(cursor.lastrowid)
        finally:
            conn.close()
        return self.get_source(source_id, include_removed=True)

    def get_source(self, source_id: int, *, include_removed: bool = False) -> SourceRecord | None:
        where_removed = "" if include_removed else "AND removed_at IS NULL"
        conn = connect_sqlite(self._db_path)
        try:
            row = conn.execute(
                f"""
                SELECT id, root_path, label, enabled, removed_at, created_at, updated_at
                FROM library_source
                WHERE id = ? {where_removed}
                """,
                (source_id,),
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            return None
        return _to_source_record(row)

    def list_sources(self, *, include_removed: bool = False) -> list[SourceRecord]:
        where_removed = "" if include_removed else "WHERE removed_at IS NULL"
        conn = connect_sqlite(self._db_path)
        try:
            rows = conn.execute(
                f"""
                SELECT id, root_path, label, enabled, removed_at, created_at, updated_at
                FROM library_source
                {where_removed}
                ORDER BY id ASC
                """
            ).fetchall()
        finally:
            conn.close()
        return [_to_source_record(row) for row in rows]

    def set_enabled(self, source_id: int, enabled: bool) -> SourceRecord | None:
        conn = connect_sqlite(self._db_path)
        try:
            conn.execute(
                """
                UPDATE library_source
                SET enabled = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND removed_at IS NULL
                """,
                (1 if enabled else 0, source_id),
            )
            changed = conn.total_changes
            conn.commit()
        finally:
            conn.close()

        if changed == 0:
            return None
        return self.get_source(source_id, include_removed=False)

    def set_label(self, source_id: int, label: str) -> SourceRecord | None:
        conn = connect_sqlite(self._db_path)
        try:
            conn.execute(
                """
                UPDATE library_source
                SET label = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND removed_at IS NULL
                """,
                (label, source_id),
            )
            changed = conn.total_changes
            conn.commit()
        finally:
            conn.close()

        if changed == 0:
            return None
        return self.get_source(source_id, include_removed=False)

    def soft_remove(self, source_id: int) -> SourceRecord | None:
        conn = connect_sqlite(self._db_path)
        try:
            conn.execute(
                """
                UPDATE library_source
                SET enabled = 0,
                    removed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND removed_at IS NULL
                """,
                (source_id,),
            )
            changed = conn.total_changes
            conn.commit()
        finally:
            conn.close()

        if changed == 0:
            return None
        return self.get_source(source_id, include_removed=True)


def _to_source_record(row: sqlite3.Row | tuple[object, ...]) -> SourceRecord:
    return SourceRecord(
        id=int(row[0]),
        root_path=str(row[1]),
        label=str(row[2]),
        enabled=bool(int(row[3])),
        removed_at=None if row[4] is None else str(row[4]),
        created_at=str(row[5]),
        updated_at=str(row[6]),
    )
