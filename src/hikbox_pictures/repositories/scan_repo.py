from __future__ import annotations

from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]


_RESUMABLE_STATUSES: tuple[str, ...] = ("pending", "running", "paused", "interrupted")
_ACTIVE_SOURCE_STATUSES: tuple[str, ...] = ("pending", "running", "paused", "interrupted")
_TERMINAL_SOURCE_STATUSES: tuple[str, ...] = ("completed", "failed", "abandoned")


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

    def latest_session(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT id, mode, status, resume_from_session_id, created_at, started_at, stopped_at, finished_at
            FROM scan_session
            ORDER BY id DESC
            LIMIT 1
            """
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

    def has_resumable_session(self) -> bool:
        row = self.conn.execute(
            """
            SELECT 1
            FROM scan_session
            WHERE status IN (?, ?, ?, ?)
            LIMIT 1
            """,
            _RESUMABLE_STATUSES,
        ).fetchone()
        return row is not None

    def latest_running_session(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT id, mode, status, resume_from_session_id, created_at, started_at, stopped_at, finished_at
            FROM scan_session
            WHERE status = 'running'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row is not None else None

    def mark_session_running(self, session_id: int) -> None:
        self.conn.execute(
            """
            UPDATE scan_session
            SET status = 'running',
                started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                stopped_at = NULL,
                finished_at = NULL
            WHERE id = ?
            """,
            (int(session_id),),
        )

    def mark_session_interrupted(self, session_id: int) -> int:
        cursor = self.conn.execute(
            """
            UPDATE scan_session
            SET status = 'interrupted',
                stopped_at = COALESCE(stopped_at, CURRENT_TIMESTAMP),
                finished_at = NULL
            WHERE id = ?
              AND status IN ('pending', 'running', 'paused')
            """,
            (int(session_id),),
        )
        return int(cursor.rowcount)

    def mark_session_completed(self, session_id: int) -> None:
        self.conn.execute(
            """
            UPDATE scan_session
            SET status = 'completed',
                finished_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status IN ('pending', 'running', 'paused', 'interrupted')
            """,
            (int(session_id),),
        )

    def mark_session_failed(self, session_id: int) -> None:
        self.conn.execute(
            """
            UPDATE scan_session
            SET status = 'failed',
                stopped_at = COALESCE(stopped_at, CURRENT_TIMESTAMP),
                finished_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status IN ('pending', 'running', 'paused', 'interrupted')
            """,
            (int(session_id),),
        )

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

    def attach_sources(self, scan_session_id: int, source_ids: list[int]) -> None:
        for source_id in source_ids:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO scan_session_source(scan_session_id, library_source_id, status)
                VALUES (?, ?, 'pending')
                """,
                (int(scan_session_id), int(source_id)),
            )

    def mark_session_sources_running(self, scan_session_id: int) -> None:
        self.conn.execute(
            """
            UPDATE scan_session_source
            SET status = 'running',
                updated_at = CURRENT_TIMESTAMP
            WHERE scan_session_id = ?
              AND status IN (?, ?, ?, ?)
            """,
            (int(scan_session_id),) + _ACTIVE_SOURCE_STATUSES,
        )

    def mark_session_sources_interrupted(self, scan_session_id: int) -> int:
        cursor = self.conn.execute(
            """
            UPDATE scan_session_source
            SET status = 'interrupted',
                updated_at = CURRENT_TIMESTAMP
            WHERE scan_session_id = ?
              AND status IN ('pending', 'running', 'paused')
            """,
            (int(scan_session_id),),
        )
        return int(cursor.rowcount)

    def abandon_resumable_sessions(self) -> int:
        rows = self.conn.execute(
            """
            SELECT id
            FROM scan_session
            WHERE status IN (?, ?, ?, ?)
            ORDER BY id ASC
            """,
            _RESUMABLE_STATUSES,
        ).fetchall()
        session_ids = [int(row["id"]) for row in rows]
        if not session_ids:
            return 0

        placeholders = ", ".join("?" for _ in session_ids)
        self.conn.execute(
            f"""
            UPDATE scan_session
            SET status = 'abandoned',
                stopped_at = COALESCE(stopped_at, CURRENT_TIMESTAMP),
                finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP)
            WHERE id IN ({placeholders})
              AND status IN (?, ?, ?, ?)
            """,
            tuple(session_ids) + _RESUMABLE_STATUSES,
        )
        self.conn.execute(
            f"""
            UPDATE scan_session_source
            SET status = 'abandoned',
                updated_at = CURRENT_TIMESTAMP
            WHERE scan_session_id IN ({placeholders})
              AND status IN (?, ?, ?, ?)
            """,
            tuple(session_ids) + _ACTIVE_SOURCE_STATUSES,
        )
        return len(session_ids)

    def mark_session_source_running(self, session_source_id: int) -> int:
        cursor = self.conn.execute(
            """
            UPDATE scan_session_source
            SET status = 'running',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status IN (?, ?, ?, ?)
            """,
            (int(session_source_id),) + _ACTIVE_SOURCE_STATUSES,
        )
        return int(cursor.rowcount)

    def mark_session_source_completed(self, session_source_id: int) -> int:
        cursor = self.conn.execute(
            """
            UPDATE scan_session_source
            SET status = 'completed',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status NOT IN (?, ?, ?)
            """,
            (int(session_source_id),) + _TERMINAL_SOURCE_STATUSES,
        )
        return int(cursor.rowcount)

    def mark_session_source_failed(self, session_source_id: int, *, cursor_json: str | None = None) -> int:
        cursor = self.conn.execute(
            """
            UPDATE scan_session_source
            SET status = 'failed',
                cursor_json = COALESCE(?, cursor_json),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status NOT IN (?, ?, ?)
            """,
            (
                cursor_json,
                int(session_source_id),
            )
            + _TERMINAL_SOURCE_STATUSES,
        )
        return int(cursor.rowcount)

    def get_session_source(self, session_source_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT id, scan_session_id, library_source_id, status, cursor_json,
                   discovered_count, metadata_done_count, faces_done_count,
                   embeddings_done_count, assignment_done_count,
                   last_checkpoint_at, created_at, updated_at
            FROM scan_session_source
            WHERE id = ?
            """,
            (int(session_source_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_session_sources(self, scan_session_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT ss.id, ss.scan_session_id, ss.library_source_id, ss.status, ss.cursor_json,
                   discovered_count, metadata_done_count, faces_done_count,
                   embeddings_done_count, assignment_done_count,
                   last_checkpoint_at, ss.created_at, ss.updated_at,
                   ls.name AS source_name, ls.root_path AS source_root_path
            FROM scan_session_source ss
            JOIN library_source ls ON ls.id = ss.library_source_id
            WHERE ss.scan_session_id = ?
            ORDER BY ss.id ASC
            """,
            (int(scan_session_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    def touch_source_heartbeat(self, session_source_id: int, cursor_json: str | None = None) -> None:
        if cursor_json is None:
            self.conn.execute(
                """
                UPDATE scan_session_source
                SET updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (int(session_source_id),),
            )
            return
        self.conn.execute(
            """
            UPDATE scan_session_source
            SET cursor_json = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (cursor_json, int(session_source_id)),
        )

    def update_source_progress_counts(
        self,
        session_source_id: int,
        *,
        discovered_count: int,
        metadata_done_count: int,
        faces_done_count: int,
        embeddings_done_count: int,
        assignment_done_count: int,
    ) -> None:
        self.conn.execute(
            """
            UPDATE scan_session_source
            SET discovered_count = ?,
                metadata_done_count = ?,
                faces_done_count = ?,
                embeddings_done_count = ?,
                assignment_done_count = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                int(discovered_count),
                int(metadata_done_count),
                int(faces_done_count),
                int(embeddings_done_count),
                int(assignment_done_count),
                int(session_source_id),
            ),
        )

    def insert_checkpoint(
        self,
        session_source_id: int,
        phase: str,
        cursor_json: str | None,
        pending_asset_count: int = 0,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO scan_checkpoint(scan_session_source_id, phase, cursor_json, pending_asset_count)
            VALUES (?, ?, ?, ?)
            """,
            (int(session_source_id), phase, cursor_json, int(pending_asset_count)),
        )
        self.conn.execute(
            """
            UPDATE scan_session_source
            SET last_checkpoint_at = CURRENT_TIMESTAMP,
                cursor_json = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (cursor_json, int(session_source_id)),
        )
        return int(cursor.lastrowid)

    def latest_checkpoint_for_source(self, session_source_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT id, scan_session_source_id, phase, cursor_json, pending_asset_count, created_at
            FROM scan_checkpoint
            WHERE scan_session_source_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(session_source_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def mark_stale_running_as_interrupted(self, stale_after_seconds: int) -> int:
        stale_ids_rows = self.conn.execute(
            """
            SELECT s.id
            FROM scan_session s
            LEFT JOIN scan_session_source ss ON ss.scan_session_id = s.id
            WHERE s.status = 'running'
            GROUP BY s.id
            HAVING (julianday('now') - julianday(
                COALESCE(MAX(ss.last_checkpoint_at), MAX(ss.updated_at), s.started_at, s.created_at)
            )) * 86400.0 > ?
            """,
            (max(int(stale_after_seconds), 0),),
        ).fetchall()
        session_ids = [int(row["id"]) for row in stale_ids_rows]
        if not session_ids:
            return 0

        placeholders = ", ".join("?" for _ in session_ids)
        self.conn.execute(
            f"""
            UPDATE scan_session
            SET status = 'interrupted',
                stopped_at = CURRENT_TIMESTAMP
            WHERE id IN ({placeholders})
              AND status = 'running'
            """,
            tuple(session_ids),
        )
        self.conn.execute(
            f"""
            UPDATE scan_session_source
            SET status = 'interrupted',
                updated_at = CURRENT_TIMESTAMP
            WHERE scan_session_id IN ({placeholders})
              AND status IN ('pending', 'running', 'paused')
            """,
            tuple(session_ids),
        )
        return len(session_ids)

    def finalize_session_if_all_sources_terminal(self, session_id: int) -> str | None:
        row = self.conn.execute(
            """
            SELECT CASE
                WHEN EXISTS (
                    SELECT 1
                    FROM scan_session_source
                    WHERE scan_session_id = ?
                      AND status NOT IN (?, ?, ?)
                ) THEN NULL
                WHEN EXISTS (
                    SELECT 1
                    FROM scan_session_source
                    WHERE scan_session_id = ?
                      AND status = 'failed'
                ) THEN 'failed'
                ELSE 'completed'
            END AS final_status
            """,
            (int(session_id),) + _TERMINAL_SOURCE_STATUSES + (int(session_id),),
        ).fetchone()
        if row is None:
            return None

        final_status = row["final_status"]
        if final_status is None:
            return None
        if str(final_status) == "failed":
            self.mark_session_failed(session_id)
            return "failed"

        self.mark_session_completed(session_id)
        return "completed"

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM scan_session").fetchone()
        return int(row["c"])
