from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from hikbox_pictures.product.db.connection import connect_sqlite

from .errors import (
    ScanActiveConflictError,
    ScanSessionIllegalStatusError,
    ScanSessionNotFoundError,
    ServeBlockedByActiveScanError,
)
from .models import ScanSession

ALLOWED_RUN_KIND = {"scan_full", "scan_incremental", "scan_resume"}
ALLOWED_TRIGGERED_BY = {"manual_webui", "manual_cli"}
ACTIVE_STATUS = {"running", "aborting"}


class SQLiteScanSessionRepository:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def create_session(
        self,
        *,
        run_kind: str,
        triggered_by: str,
        status: str,
        resume_from_session_id: int | None = None,
    ) -> ScanSession:
        now = _utc_now()
        with connect_sqlite(self._db_path) as conn:
            cursor = conn.execute(
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
                )
                VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (run_kind, status, triggered_by, resume_from_session_id, now, now, now),
            )
            session_id = int(cursor.lastrowid)
            row = conn.execute(
                "SELECT * FROM scan_session WHERE id=?",
                (session_id,),
            ).fetchone()
        assert row is not None
        return _row_to_scan_session(row)

    def get_session(self, session_id: int) -> ScanSession | None:
        with connect_sqlite(self._db_path) as conn:
            row = conn.execute(
                "SELECT * FROM scan_session WHERE id=?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_scan_session(row)

    def latest_by_status(self, statuses: set[str]) -> ScanSession | None:
        placeholders = ",".join("?" for _ in statuses)
        sql = f"""
            SELECT *
            FROM scan_session
            WHERE status IN ({placeholders})
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        """
        with connect_sqlite(self._db_path) as conn:
            row = conn.execute(sql, tuple(statuses)).fetchone()
        if row is None:
            return None
        return _row_to_scan_session(row)

    def update_status(
        self,
        session_id: int,
        *,
        status: str,
        finished_at: str | None,
        last_error: str | None = None,
    ) -> ScanSession:
        with connect_sqlite(self._db_path) as conn:
            conn.execute(
                """
                UPDATE scan_session
                SET status=?,
                    finished_at=?,
                    last_error=?,
                    updated_at=?
                WHERE id=?
                """,
                (status, finished_at, last_error, _utc_now(), session_id),
            )
            row = conn.execute(
                "SELECT * FROM scan_session WHERE id=?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise ScanSessionNotFoundError(session_id)
        return _row_to_scan_session(row)

    def has_active(self) -> bool:
        return self.latest_by_status(ACTIVE_STATUS) is not None


class ScanSessionService:
    def __init__(self, repo: SQLiteScanSessionRepository) -> None:
        self._repo = repo

    def start_or_resume(self, *, run_kind: str, triggered_by: str) -> ScanSession:
        _validate_run_kind(run_kind)
        _validate_triggered_by(triggered_by)

        active = self._repo.latest_by_status(ACTIVE_STATUS)
        if active is not None:
            return replace(active, resumed=True)

        interrupted = self._repo.latest_by_status({"interrupted"})
        if interrupted is not None:
            try:
                resumed = self._repo.update_status(
                    interrupted.id,
                    status="running",
                    finished_at=None,
                )
            except sqlite3.IntegrityError as exc:
                raise _as_active_conflict(self._repo, exc) from exc
            return replace(resumed, resumed=True)

        try:
            created = self._repo.create_session(
                run_kind=run_kind,
                triggered_by=triggered_by,
                status="running",
            )
        except sqlite3.IntegrityError as exc:
            raise _as_active_conflict(self._repo, exc) from exc
        return replace(created, resumed=False)

    def start_new(self, *, run_kind: str, triggered_by: str) -> ScanSession:
        _validate_run_kind(run_kind)
        _validate_triggered_by(triggered_by)

        active = self._repo.latest_by_status(ACTIVE_STATUS)
        if active is not None:
            raise ScanActiveConflictError(active.id)

        interrupted = self._repo.latest_by_status({"interrupted"})
        if interrupted is not None:
            self._repo.update_status(
                interrupted.id,
                status="abandoned",
                finished_at=_utc_now(),
            )

        try:
            return self._repo.create_session(
                run_kind=run_kind,
                triggered_by=triggered_by,
                status="running",
            )
        except sqlite3.IntegrityError as exc:
            raise _as_active_conflict(self._repo, exc) from exc

    def abort(self, session_id: int) -> ScanSession:
        session = self._repo.get_session(session_id)
        if session is None:
            raise ScanSessionNotFoundError(session_id)
        if session.status == "aborting":
            return session
        if session.status != "running":
            raise ScanSessionIllegalStatusError(session_id, session.status)
        try:
            return self._repo.update_status(
                session_id,
                status="aborting",
                finished_at=None,
            )
        except sqlite3.IntegrityError as exc:
            raise _as_active_conflict(self._repo, exc) from exc

    def mark_interrupted(self, session_id: int, *, last_error: str | None = None) -> ScanSession:
        session = self._repo.get_session(session_id)
        if session is None:
            raise ScanSessionNotFoundError(session_id)
        if session.status == "interrupted":
            return session
        if session.status not in {"running", "aborting"}:
            raise ScanSessionIllegalStatusError(session_id, session.status)
        return self._repo.update_status(
            session_id,
            status="interrupted",
            finished_at=_utc_now(),
            last_error=last_error,
        )


def assert_no_active_scan_for_serve(repo: SQLiteScanSessionRepository) -> None:
    active = repo.latest_by_status(ACTIVE_STATUS)
    if active is not None:
        raise ServeBlockedByActiveScanError(active.id)


def _validate_run_kind(run_kind: str) -> None:
    if run_kind not in ALLOWED_RUN_KIND:
        raise ValueError(f"不支持的 run_kind: {run_kind}")


def _validate_triggered_by(triggered_by: str) -> None:
    if triggered_by not in ALLOWED_TRIGGERED_BY:
        raise ValueError(f"不支持的 triggered_by: {triggered_by}")


def _row_to_scan_session(row: sqlite3.Row | tuple[object, ...]) -> ScanSession:
    return ScanSession(
        id=int(row[0]),
        run_kind=str(row[1]),
        status=str(row[2]),
        triggered_by=str(row[3]),
        resume_from_session_id=int(row[4]) if row[4] is not None else None,
        started_at=str(row[5]) if row[5] is not None else None,
        finished_at=str(row[6]) if row[6] is not None else None,
        last_error=str(row[7]) if row[7] is not None else None,
        created_at=str(row[8]),
        updated_at=str(row[9]),
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _as_active_conflict(repo: SQLiteScanSessionRepository, exc: sqlite3.IntegrityError) -> ScanActiveConflictError:
    message = str(exc).lower()
    if "uq_scan_session_single_active" in message or "unique constraint failed" in message:
        active = repo.latest_by_status(ACTIVE_STATUS)
        active_session_id = active.id if active is not None else None
        return ScanActiveConflictError(active_session_id)
    raise exc
