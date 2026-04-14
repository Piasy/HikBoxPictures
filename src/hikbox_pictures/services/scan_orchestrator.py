from __future__ import annotations

from collections.abc import Callable
from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.repositories import ScanRepo, SourceRepo
from hikbox_pictures.services.observability_service import ObservabilityService
from hikbox_pictures.services.scan_execution_service import ScanExecutionService


class ScanOrchestrator:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.scan_repo = ScanRepo(conn)
        self.source_repo = SourceRepo(conn)
        self.observability = ObservabilityService(conn)

    def start_or_resume(self) -> int:
        session_id: int | None = None
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
                self.observability.emit_event(
                    level="info",
                    component="scanner",
                    event_type="scan.session.resumed",
                    message="扫描会话已恢复",
                    run_kind="scan",
                    run_id=str(session_id),
                    detail={"status": "running"},
                )
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
                self.observability.emit_event(
                    level="info",
                    component="scanner",
                    event_type="scan.session.resumed",
                    message="扫描会话已恢复",
                    run_kind="scan",
                    run_id=str(session_id),
                    detail={"status": "running"},
                )
                return session_id

            self.scan_repo.attach_sources(session_id, self.source_repo.list_active_source_ids())
            self.conn.execute("COMMIT")
            self.observability.emit_event(
                level="info",
                component="scanner",
                event_type="scan.session.started",
                message="扫描会话已启动",
                run_kind="scan",
                run_id=str(session_id),
                detail={"status": "running"},
            )
            return session_id
        except Exception as exc:
            try:
                self.conn.execute("ROLLBACK")
            except Exception:
                pass
            self.observability.emit_event(
                level="error",
                component="scanner",
                event_type="scan.session.failed",
                message=str(exc),
                run_kind="scan",
                run_id=str(session_id) if session_id is not None else None,
                detail={
                    "status": "failed",
                    "error_type": exc.__class__.__name__,
                    "error_message": str(exc),
                },
            )
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
        source_row = self.scan_repo.get_session_source(session_source_id)
        scan_session_id = int(source_row["scan_session_id"]) if source_row is not None else None
        self.observability.emit_event(
            level="info",
            component="scanner",
            event_type="scan.session.checkpointed",
            message="扫描检查点已写入",
            run_kind="scan",
            run_id=str(scan_session_id) if scan_session_id is not None else None,
            detail={
                "phase": phase,
                "status": "checkpointed",
                "pending_asset_count": pending_asset_count,
                "scan_session_source_id": int(session_source_id),
            },
        )
        return checkpoint_id

    def execute_session(
        self,
        session_id: int,
        *,
        progress_reporter: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, int]:
        execution = ScanExecutionService(
            self.conn,
            checkpoint_writer=lambda source_id, phase, cursor_json, pending: self.write_checkpoint(
                source_id,
                phase=phase,
                cursor_json=cursor_json,
                pending_asset_count=pending,
            ),
            progress_reporter=progress_reporter,
        )
        return execution.run_session(session_id)

    def start_or_resume_and_run(
        self,
        *,
        progress_reporter: Callable[[dict[str, Any]], None] | None = None,
    ) -> int:
        session_id = self.start_or_resume()
        self.execute_session(session_id, progress_reporter=progress_reporter)
        return session_id

    def abort(self) -> dict[str, Any]:
        session_id: int | None = None
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            session = self.scan_repo.latest_resumable_session()
            if session is None:
                self.conn.execute("COMMIT")
                return {"session_id": None, "status": "idle", "mode": None}

            session_id = int(session["id"])
            if str(session["status"]) in {"pending", "running", "paused"}:
                self.scan_repo.mark_session_interrupted(session_id)
                self.scan_repo.mark_session_sources_interrupted(session_id)
            self.conn.execute("COMMIT")

            interrupted = self.scan_repo.get_session(session_id)
            if interrupted is None:
                return {"session_id": session_id, "status": "unknown", "mode": None}

            self.observability.emit_event(
                level="info",
                component="scanner",
                event_type="scan.session.interrupted",
                message="扫描会话已中断",
                run_kind="scan",
                run_id=str(session_id),
                detail={"status": interrupted["status"]},
            )
            return {
                "session_id": int(interrupted["id"]),
                "status": interrupted["status"],
                "mode": interrupted["mode"],
            }
        except Exception as exc:
            try:
                self.conn.execute("ROLLBACK")
            except Exception:
                pass
            self.observability.emit_event(
                level="error",
                component="scanner",
                event_type="scan.session.abort.failed",
                message=str(exc),
                run_kind="scan",
                run_id=str(session_id) if session_id is not None else None,
                detail={
                    "status": "failed",
                    "error_type": exc.__class__.__name__,
                    "error_message": str(exc),
                },
            )
            raise

    def start_new(self, *, abandon_resumable: bool) -> int:
        session_id: int | None = None
        abandoned_count = 0
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            if abandon_resumable:
                abandoned_count = self.scan_repo.abandon_resumable_sessions()
            elif self.scan_repo.has_resumable_session():
                raise ValueError("存在未完成扫描会话，请使用 --abandon-resumable 放弃旧任务后再启动新扫描")

            session_id = self.scan_repo.create_session(mode="incremental", status="running", started=True)
            self.scan_repo.attach_sources(session_id, self.source_repo.list_active_source_ids())
            self.conn.execute("COMMIT")
            self.observability.emit_event(
                level="info",
                component="scanner",
                event_type="scan.session.started",
                message="扫描会话已启动",
                run_kind="scan",
                run_id=str(session_id),
                detail={
                    "status": "running",
                    "abandon_resumable": bool(abandon_resumable),
                    "abandoned_resumable_count": int(abandoned_count),
                },
            )
            return session_id
        except Exception as exc:
            try:
                self.conn.execute("ROLLBACK")
            except Exception:
                pass
            self.observability.emit_event(
                level="error",
                component="scanner",
                event_type="scan.session.failed",
                message=str(exc),
                run_kind="scan",
                run_id=str(session_id) if session_id is not None else None,
                detail={
                    "status": "failed",
                    "error_type": exc.__class__.__name__,
                    "error_message": str(exc),
                },
            )
            raise

    def start_new_and_run(
        self,
        *,
        abandon_resumable: bool,
        progress_reporter: Callable[[dict[str, Any]], None] | None = None,
    ) -> int:
        session_id = self.start_new(abandon_resumable=abandon_resumable)
        self.execute_session(session_id, progress_reporter=progress_reporter)
        return session_id

    def get_status(self) -> dict[str, Any]:
        session = self.scan_repo.latest_session()
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
