"""扫描会话状态机服务。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hikbox_pictures.product.db.connection import connect_sqlite
from hikbox_pictures.product.scan.errors import (
    InvalidRunKindError,
    InvalidTriggeredByError,
    ScanActiveConflictError,
    ServeBlockedByActiveScanError,
    SessionNotFoundError,
)
from hikbox_pictures.product.scan.models import (
    ACTIVE_STATUS,
    ALLOWED_RUN_KIND,
    ALLOWED_TRIGGERED_BY,
    ScanSessionRecord,
    ScanStartResult,
)


class ScanSessionRepository:
    """扫描会话数据访问层。"""

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)

    def create_session(
        self,
        *,
        run_kind: str,
        status: str,
        triggered_by: str,
        resume_from_session_id: int | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        last_error: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> ScanSessionRecord:
        managed_conn = conn is None
        db_conn = conn or connect_sqlite(self._db_path)
        try:
            cursor = db_conn.execute(
                """
                INSERT INTO scan_session(
                    run_kind,
                    status,
                    triggered_by,
                    resume_from_session_id,
                    started_at,
                    finished_at,
                    last_error,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    run_kind,
                    status,
                    triggered_by,
                    resume_from_session_id,
                    started_at,
                    finished_at,
                    last_error,
                ),
            )
            if managed_conn:
                db_conn.commit()
            session_id = int(cursor.lastrowid)
        finally:
            if managed_conn:
                db_conn.close()
        return self.get_session(session_id, conn=conn)

    def get_session(self, session_id: int, *, conn: sqlite3.Connection | None = None) -> ScanSessionRecord:
        managed_conn = conn is None
        db_conn = conn or connect_sqlite(self._db_path)
        try:
            row = db_conn.execute(
                """
                SELECT
                    id,
                    run_kind,
                    status,
                    triggered_by,
                    resume_from_session_id,
                    started_at,
                    finished_at,
                    last_error,
                    created_at,
                    updated_at
                FROM scan_session
                WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
        finally:
            if managed_conn:
                db_conn.close()

        if row is None:
            raise SessionNotFoundError(session_id)
        return _to_scan_session_record(row)

    def count_sessions(self, *, conn: sqlite3.Connection | None = None) -> int:
        managed_conn = conn is None
        db_conn = conn or connect_sqlite(self._db_path)
        try:
            row = db_conn.execute("SELECT COUNT(*) FROM scan_session").fetchone()
        finally:
            if managed_conn:
                db_conn.close()
        return int(row[0]) if row is not None else 0

    def latest_by_status(
        self,
        statuses: set[str],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> ScanSessionRecord | None:
        if not statuses:
            return None
        placeholders = ", ".join("?" for _ in statuses)
        managed_conn = conn is None
        db_conn = conn or connect_sqlite(self._db_path)
        try:
            row = db_conn.execute(
                f"""
                SELECT
                    id,
                    run_kind,
                    status,
                    triggered_by,
                    resume_from_session_id,
                    started_at,
                    finished_at,
                    last_error,
                    created_at,
                    updated_at
                FROM scan_session
                WHERE status IN ({placeholders})
                ORDER BY id DESC
                LIMIT 1
                """,
                tuple(statuses),
            ).fetchone()
        finally:
            if managed_conn:
                db_conn.close()

        if row is None:
            return None
        return _to_scan_session_record(row)

    def update_status(
        self,
        session_id: int,
        *,
        status: str,
        started_at: str | None = None,
        finished_at: str | None = None,
        last_error: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> ScanSessionRecord:
        managed_conn = conn is None
        db_conn = conn or connect_sqlite(self._db_path)
        try:
            cursor = db_conn.execute(
                """
                UPDATE scan_session
                SET status = ?,
                    started_at = COALESCE(?, started_at),
                    finished_at = COALESCE(?, finished_at),
                    last_error = COALESCE(?, last_error),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, started_at, finished_at, last_error, session_id),
            )
            if cursor.rowcount == 0:
                raise SessionNotFoundError(session_id)
            if managed_conn:
                db_conn.commit()
        finally:
            if managed_conn:
                db_conn.close()
        return self.get_session(session_id, conn=conn)

    def connect(self) -> sqlite3.Connection:
        return connect_sqlite(self._db_path)


class ScanSessionService:
    """扫描会话状态机。"""

    def __init__(self, repo: ScanSessionRepository):
        self._repo = repo

    def start_or_resume(self, *, run_kind: str, triggered_by: str) -> ScanStartResult:
        _validate_run_kind(run_kind)
        _validate_triggered_by(triggered_by)
        conn = self._repo.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            active = self._repo.latest_by_status(ACTIVE_STATUS, conn=conn)
            if active is not None:
                conn.commit()
                return ScanStartResult(session_id=active.id, resumed=True, should_execute=False)

            interrupted = self._repo.latest_by_status({"interrupted"}, conn=conn)
            if interrupted is not None:
                try:
                    resumed = self._repo.update_status(interrupted.id, status="running", conn=conn)
                except sqlite3.IntegrityError:
                    active = self._repo.latest_by_status(ACTIVE_STATUS, conn=conn)
                    if active is None:
                        raise
                    conn.commit()
                    return ScanStartResult(session_id=active.id, resumed=True, should_execute=False)
                conn.commit()
                return ScanStartResult(session_id=resumed.id, resumed=True, should_execute=True)

            try:
                created = self._repo.create_session(
                    run_kind=run_kind,
                    status="running",
                    triggered_by=triggered_by,
                    conn=conn,
                )
            except sqlite3.IntegrityError:
                active = self._repo.latest_by_status(ACTIVE_STATUS, conn=conn)
                if active is None:
                    raise
                conn.commit()
                return ScanStartResult(session_id=active.id, resumed=True, should_execute=False)
            conn.commit()
            return ScanStartResult(session_id=created.id, resumed=False, should_execute=True)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def start_new(self, *, run_kind: str, triggered_by: str) -> ScanStartResult:
        _validate_run_kind(run_kind)
        _validate_triggered_by(triggered_by)
        conn = self._repo.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            active = self._repo.latest_by_status(ACTIVE_STATUS, conn=conn)
            if active is not None:
                raise ScanActiveConflictError(active.id)

            interrupted = self._repo.latest_by_status({"interrupted"}, conn=conn)
            if interrupted is not None:
                self._repo.update_status(interrupted.id, status="abandoned", conn=conn)

            try:
                created = self._repo.create_session(
                    run_kind=run_kind,
                    status="pending",
                    triggered_by=triggered_by,
                    conn=conn,
                )
            except sqlite3.IntegrityError:
                active = self._repo.latest_by_status(ACTIVE_STATUS, conn=conn)
                if active is None:
                    raise
                raise ScanActiveConflictError(active.id) from None

            conn.commit()
            return ScanStartResult(session_id=created.id, resumed=False, should_execute=True)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def abort(self, session_id: int) -> ScanSessionRecord:
        session = self._repo.get_session(session_id)
        if session.status in {"pending", "running"}:
            return self._repo.update_status(session_id, status="aborting")
        return session


def assert_no_active_scan_for_serve(repo: ScanSessionRepository) -> None:
    """serve 启动前检查：有活跃会话则阻断。"""

    active = repo.latest_by_status(ACTIVE_STATUS)
    if active is not None:
        raise ServeBlockedByActiveScanError(active.id)


def _validate_run_kind(run_kind: str) -> None:
    if run_kind not in ALLOWED_RUN_KIND:
        raise InvalidRunKindError(f"非法 run_kind: {run_kind}")


def _validate_triggered_by(triggered_by: str) -> None:
    if triggered_by not in ALLOWED_TRIGGERED_BY:
        raise InvalidTriggeredByError(f"非法 triggered_by: {triggered_by}")


def _to_scan_session_record(row: sqlite3.Row | tuple[object, ...]) -> ScanSessionRecord:
    return ScanSessionRecord(
        id=int(row[0]),
        run_kind=str(row[1]),
        status=str(row[2]),
        triggered_by=str(row[3]),
        resume_from_session_id=None if row[4] is None else int(row[4]),
        started_at=None if row[5] is None else str(row[5]),
        finished_at=None if row[6] is None else str(row[6]),
        last_error=None if row[7] is None else str(row[7]),
        created_at=str(row[8]),
        updated_at=str(row[9]),
    )
