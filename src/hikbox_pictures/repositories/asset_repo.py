from __future__ import annotations

from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]


class AssetRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add_photo_asset(
        self,
        library_source_id: int,
        primary_path: str,
        processing_status: str = "discovered",
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO photo_asset(library_source_id, primary_path, processing_status)
            VALUES (?, ?, ?)
            """,
            (int(library_source_id), primary_path, processing_status),
        )
        return int(cursor.lastrowid)

    def get_asset(self, asset_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT id, library_source_id, primary_path, processing_status,
                   capture_datetime, capture_month, created_at, updated_at
            FROM photo_asset
            WHERE id = ?
            """,
            (int(asset_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM photo_asset").fetchone()
        return int(row["c"])
