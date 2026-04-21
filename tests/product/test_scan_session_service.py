from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hikbox_pictures.product.db.schema_bootstrap import bootstrap_library_schema
from hikbox_pictures.product.scan.errors import (
    ScanActiveConflictError,
    ScanSessionIllegalStatusError,
    ScanSessionNotFoundError,
    ServeBlockedByActiveScanError,
)
from hikbox_pictures.product.scan.session_service import (
    SQLiteScanSessionRepository,
    ScanSessionService,
    assert_no_active_scan_for_serve,
)


def _insert_scan_session(
    db_path: Path,
    *,
    run_kind: str,
    status: str,
    created_at: str,
    updated_at: str,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> int:
    with sqlite3.connect(db_path) as conn:
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
            VALUES (?, ?, 'manual_cli', NULL, ?, ?, NULL, ?, ?)
            """,
            (run_kind, status, started_at, finished_at, created_at, updated_at),
        )
        conn.commit()
        return int(cursor.lastrowid)


def _count_sessions(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(1) FROM scan_session").fetchone()
    return int(row[0])


def _status_of(db_path: Path, session_id: int) -> str:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM scan_session WHERE id=?",
            (session_id,),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _fetch_finished_at(db_path: Path, session_id: int) -> str | None:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT finished_at FROM scan_session WHERE id=?",
            (session_id,),
        ).fetchone()
    assert row is not None
    return row[0]


def test_start_new_raises_when_active_session_exists(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)
    _insert_scan_session(
        db_path,
        run_kind="scan_full",
        status="running",
        created_at="2026-04-22T00:00:00+00:00",
        updated_at="2026-04-22T00:00:00+00:00",
        started_at="2026-04-22T00:00:00+00:00",
    )

    repo = SQLiteScanSessionRepository(db_path)
    service = ScanSessionService(repo)

    with pytest.raises(ScanActiveConflictError):
        service.start_new(run_kind="scan_full", triggered_by="manual_cli")


def test_start_or_resume_resumes_latest_interrupted_without_new_session(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)

    _insert_scan_session(
        db_path,
        run_kind="scan_full",
        status="interrupted",
        created_at="2026-04-21T00:00:00+00:00",
        updated_at="2026-04-21T00:00:00+00:00",
        finished_at="2026-04-21T00:01:00+00:00",
    )
    latest_interrupted_id = _insert_scan_session(
        db_path,
        run_kind="scan_incremental",
        status="interrupted",
        created_at="2026-04-22T00:00:00+00:00",
        updated_at="2026-04-22T00:00:00+00:00",
        finished_at="2026-04-22T00:01:00+00:00",
    )
    before_count = _count_sessions(db_path)

    repo = SQLiteScanSessionRepository(db_path)
    service = ScanSessionService(repo)

    resumed = service.start_or_resume(run_kind="scan_resume", triggered_by="manual_cli")

    assert resumed.id == latest_interrupted_id
    assert resumed.status == "running"
    assert resumed.resumed is True
    assert _count_sessions(db_path) == before_count


def test_start_or_resume_reuses_active_session(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)
    active_id = _insert_scan_session(
        db_path,
        run_kind="scan_full",
        status="running",
        created_at="2026-04-22T00:00:00+00:00",
        updated_at="2026-04-22T00:00:00+00:00",
        started_at="2026-04-22T00:00:00+00:00",
    )
    before_count = _count_sessions(db_path)

    repo = SQLiteScanSessionRepository(db_path)
    service = ScanSessionService(repo)

    reused = service.start_or_resume(run_kind="scan_incremental", triggered_by="manual_cli")

    assert reused.id == active_id
    assert reused.status == "running"
    assert reused.resumed is True
    assert _count_sessions(db_path) == before_count


def test_start_or_resume_creates_new_when_no_active_and_no_interrupted(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)
    before_count = _count_sessions(db_path)

    repo = SQLiteScanSessionRepository(db_path)
    service = ScanSessionService(repo)

    created = service.start_or_resume(run_kind="scan_incremental", triggered_by="manual_cli")

    assert created.status == "running"
    assert created.resumed is False
    assert _count_sessions(db_path) == before_count + 1


def test_start_new_abandons_latest_interrupted_then_creates_new_running(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)

    older_interrupted = _insert_scan_session(
        db_path,
        run_kind="scan_full",
        status="interrupted",
        created_at="2026-04-20T00:00:00+00:00",
        updated_at="2026-04-20T00:00:00+00:00",
        finished_at="2026-04-20T00:01:00+00:00",
    )
    latest_interrupted = _insert_scan_session(
        db_path,
        run_kind="scan_incremental",
        status="interrupted",
        created_at="2026-04-21T00:00:00+00:00",
        updated_at="2026-04-21T00:00:00+00:00",
        finished_at="2026-04-21T00:01:00+00:00",
    )

    repo = SQLiteScanSessionRepository(db_path)
    service = ScanSessionService(repo)

    created = service.start_new(run_kind="scan_full", triggered_by="manual_cli")

    assert created.status == "running"
    assert created.id != latest_interrupted
    assert _status_of(db_path, older_interrupted) == "interrupted"
    assert _status_of(db_path, latest_interrupted) == "abandoned"
    assert _fetch_finished_at(db_path, latest_interrupted) is not None


def test_start_new_rejects_unknown_run_kind(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)

    repo = SQLiteScanSessionRepository(db_path)
    service = ScanSessionService(repo)

    with pytest.raises(ValueError, match="run_kind"):
        service.start_new(run_kind="scan_invalid", triggered_by="manual_cli")


def test_abort_switches_running_to_aborting(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)

    session_id = _insert_scan_session(
        db_path,
        run_kind="scan_full",
        status="running",
        created_at="2026-04-22T00:00:00+00:00",
        updated_at="2026-04-22T00:00:00+00:00",
        started_at="2026-04-22T00:00:00+00:00",
    )

    repo = SQLiteScanSessionRepository(db_path)
    service = ScanSessionService(repo)

    aborted = service.abort(session_id)

    assert aborted.id == session_id
    assert aborted.status == "aborting"


def test_abort_raises_when_session_not_found(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)

    repo = SQLiteScanSessionRepository(db_path)
    service = ScanSessionService(repo)

    with pytest.raises(ScanSessionNotFoundError):
        service.abort(9999)


def test_abort_raises_when_session_not_running(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)
    session_id = _insert_scan_session(
        db_path,
        run_kind="scan_full",
        status="completed",
        created_at="2026-04-22T00:00:00+00:00",
        updated_at="2026-04-22T00:00:00+00:00",
        finished_at="2026-04-22T00:05:00+00:00",
    )

    repo = SQLiteScanSessionRepository(db_path)
    service = ScanSessionService(repo)

    with pytest.raises(ScanSessionIllegalStatusError):
        service.abort(session_id)


def test_mark_interrupted_allows_running_and_aborting(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)

    running_id = _insert_scan_session(
        db_path,
        run_kind="scan_full",
        status="running",
        created_at="2026-04-22T00:00:00+00:00",
        updated_at="2026-04-22T00:00:00+00:00",
        started_at="2026-04-22T00:00:00+00:00",
    )
    repo = SQLiteScanSessionRepository(db_path)
    service = ScanSessionService(repo)

    running_marked = service.mark_interrupted(running_id, last_error="running-stop")
    assert running_marked.status == "interrupted"
    assert running_marked.last_error == "running-stop"
    assert running_marked.finished_at is not None

    aborting_id = _insert_scan_session(
        db_path,
        run_kind="scan_incremental",
        status="aborting",
        created_at="2026-04-22T00:01:00+00:00",
        updated_at="2026-04-22T00:01:00+00:00",
        started_at="2026-04-22T00:01:00+00:00",
    )
    aborting_marked = service.mark_interrupted(aborting_id, last_error="aborting-stop")
    assert aborting_marked.status == "interrupted"
    assert aborting_marked.last_error == "aborting-stop"
    assert aborting_marked.finished_at is not None


def test_mark_interrupted_rejects_completed_and_failed(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)
    completed_id = _insert_scan_session(
        db_path,
        run_kind="scan_full",
        status="completed",
        created_at="2026-04-22T00:00:00+00:00",
        updated_at="2026-04-22T00:00:00+00:00",
        finished_at="2026-04-22T00:02:00+00:00",
    )
    failed_id = _insert_scan_session(
        db_path,
        run_kind="scan_incremental",
        status="failed",
        created_at="2026-04-22T00:01:00+00:00",
        updated_at="2026-04-22T00:01:00+00:00",
        finished_at="2026-04-22T00:03:00+00:00",
    )
    repo = SQLiteScanSessionRepository(db_path)
    service = ScanSessionService(repo)

    with pytest.raises(ScanSessionIllegalStatusError):
        service.mark_interrupted(completed_id, last_error="nope")
    with pytest.raises(ScanSessionIllegalStatusError):
        service.mark_interrupted(failed_id, last_error="nope")


def test_mark_interrupted_is_idempotent_when_already_interrupted(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)
    interrupted_id = _insert_scan_session(
        db_path,
        run_kind="scan_full",
        status="interrupted",
        created_at="2026-04-22T00:00:00+00:00",
        updated_at="2026-04-22T00:00:00+00:00",
        finished_at="2026-04-22T00:01:00+00:00",
    )
    repo = SQLiteScanSessionRepository(db_path)
    service = ScanSessionService(repo)

    before = repo.get_session(interrupted_id)
    assert before is not None
    again = service.mark_interrupted(interrupted_id, last_error="ignored")
    after = repo.get_session(interrupted_id)
    assert after is not None

    assert again.status == "interrupted"
    assert again.finished_at == before.finished_at
    assert again.last_error == before.last_error
    assert after.last_error == before.last_error


def test_start_new_converts_db_unique_conflict_to_domain_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)

    repo = SQLiteScanSessionRepository(db_path)
    service = ScanSessionService(repo)

    def _fake_latest_by_status(_: set[str]) -> None:
        return None

    def _fake_create_session(**_: object) -> None:
        raise sqlite3.IntegrityError("uq_scan_session_single_active")

    monkeypatch.setattr(repo, "latest_by_status", _fake_latest_by_status)
    monkeypatch.setattr(repo, "create_session", _fake_create_session)

    with pytest.raises(ScanActiveConflictError):
        service.start_new(run_kind="scan_full", triggered_by="manual_cli")


def test_scan_session_enforces_single_active_in_db(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)
    _insert_scan_session(
        db_path,
        run_kind="scan_full",
        status="running",
        created_at="2026-04-22T00:00:00+00:00",
        updated_at="2026-04-22T00:00:00+00:00",
        started_at="2026-04-22T00:00:00+00:00",
    )

    with pytest.raises(sqlite3.IntegrityError):
        _insert_scan_session(
            db_path,
            run_kind="scan_incremental",
            status="aborting",
            created_at="2026-04-22T00:01:00+00:00",
            updated_at="2026-04-22T00:01:00+00:00",
            started_at="2026-04-22T00:01:00+00:00",
        )


def test_assert_no_active_scan_for_serve_blocks_when_active_exists(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)
    _insert_scan_session(
        db_path,
        run_kind="scan_full",
        status="aborting",
        created_at="2026-04-22T00:00:00+00:00",
        updated_at="2026-04-22T00:00:00+00:00",
        started_at="2026-04-22T00:00:00+00:00",
    )

    repo = SQLiteScanSessionRepository(db_path)

    with pytest.raises(ServeBlockedByActiveScanError):
        assert_no_active_scan_for_serve(repo)


def test_assert_no_active_scan_for_serve_passes_without_active(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)

    repo = SQLiteScanSessionRepository(db_path)
    assert_no_active_scan_for_serve(repo)
