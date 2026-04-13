from __future__ import annotations

from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.repositories import ScanRepo, SourceRepo


class ScanOrchestrator:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.scan_repo = ScanRepo(conn)
        self.source_repo = SourceRepo(conn)

    def start_or_resume(self) -> int:
        try:
            self.conn.execute("BEGIN IMMEDIATE")

            session = self.scan_repo.latest_resumable_session()
            if session is not None:
                session_id = int(session["id"])
                if session["status"] != "running":
                    try:
                        self.scan_repo.mark_session_running(session_id)
                    except sqlite3.IntegrityError:
                        running = self.scan_repo.latest_running_session()
                        if running is None:
                            raise
                        session_id = int(running["id"])
                self.scan_repo.mark_session_sources_running(session_id)
                self.conn.execute("COMMIT")
                return session_id

            try:
                session_id = self.scan_repo.create_session(mode="incremental", status="running", started=True)
            except sqlite3.IntegrityError:
                running = self.scan_repo.latest_running_session()
                if running is None:
                    raise
                session_id = int(running["id"])
                self.scan_repo.mark_session_sources_running(session_id)
                self.conn.execute("COMMIT")
                return session_id

            self.scan_repo.attach_sources(session_id, self.source_repo.list_active_source_ids())
            self.conn.execute("COMMIT")
            return session_id
        except Exception:
            try:
                self.conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def write_checkpoint(
        self,
        session_source_id: int,
        *,
        phase: str,
        cursor_json: str | None,
        pending_asset_count: int = 0,
    ) -> int:
        checkpoint_id = self.scan_repo.insert_checkpoint(
            session_source_id,
            phase=phase,
            cursor_json=cursor_json,
            pending_asset_count=pending_asset_count,
        )
        self.scan_repo.touch_source_heartbeat(session_source_id, cursor_json=cursor_json)
        self.conn.commit()
        return checkpoint_id

    def get_status(self) -> dict[str, Any]:
        session = self.scan_repo.latest_resumable_session()
        if session is None:
            return {
                "session_id": None,
                "mode": None,
                "status": "idle",
                "created_at": None,
                "started_at": None,
                "stopped_at": None,
                "finished_at": None,
                "sources": [],
            }
        session_id = int(session["id"])
        return {
            "session_id": session_id,
            "mode": session["mode"],
            "status": session["status"],
            "created_at": session["created_at"],
            "started_at": session["started_at"],
            "stopped_at": session["stopped_at"],
            "finished_at": session["finished_at"],
            "sources": self.scan_repo.list_session_sources(session_id),
        }
