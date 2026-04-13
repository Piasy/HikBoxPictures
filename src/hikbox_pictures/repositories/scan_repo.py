from __future__ import annotations

from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]


_RESUMABLE_STATUSES: tuple[str, ...] = ("pending", "running", "paused", "interrupted")


class ScanRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create_session(
        self,
        mode: str,
        status: str,
        resume_from_session_id: int | None = None,
        started: bool = False,
    ) -> int:
        if started:
            cursor = self.conn.execute(
                """
                INSERT INTO scan_session(mode, status, resume_from_session_id, started_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (mode, status, resume_from_session_id),
            )
        else:
            cursor = self.conn.execute(
                """
                INSERT INTO scan_session(mode, status, resume_from_session_id)
                VALUES (?, ?, ?)
                """,
                (mode, status, resume_from_session_id),
            )
        return int(cursor.lastrowid)

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT id, mode, status, resume_from_session_id, created_at, started_at, stopped_at, finished_at
            FROM scan_session
            WHERE id = ?
            """,
            (int(session_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def latest_resumable_session(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT id, mode, status, resume_from_session_id, created_at, started_at, stopped_at, finished_at
            FROM scan_session
            WHERE status IN (?, ?, ?, ?)
            ORDER BY id DESC
            LIMIT 1
            """,
            _RESUMABLE_STATUSES,
        ).fetchone()
        return dict(row) if row is not None else None

    def create_session_source(
        self,
        scan_session_id: int,
        library_source_id: int,
        status: str = "pending",
        cursor_json: str | None = None,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO scan_session_source(scan_session_id, library_source_id, status, cursor_json)
            VALUES (?, ?, ?, ?)
            """,
            (int(scan_session_id), int(library_source_id), status, cursor_json),
        )
        return int(cursor.lastrowid)

    def list_session_sources(self, scan_session_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT id, scan_session_id, library_source_id, status, cursor_json,
                   discovered_count, metadata_done_count, faces_done_count,
                   embeddings_done_count, assignment_done_count,
                   last_checkpoint_at, created_at, updated_at
            FROM scan_session_source
            WHERE scan_session_id = ?
            ORDER BY id ASC
            """,
            (int(scan_session_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM scan_session").fetchone()
        return int(row["c"])
