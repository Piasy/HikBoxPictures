from pathlib import Path

import pytest
import sqlite3

from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.scan import models as scan_models
from hikbox_pictures.product.scan.errors import (
    InvalidRunKindError,
    ScanActiveConflictError,
    ServeBlockedByActiveScanError,
)
from hikbox_pictures.product.scan.session_service import (
    ScanSessionRepository,
    ScanSessionService,
    assert_no_active_scan_for_serve,
)


@pytest.fixture
def repo(tmp_path: Path) -> ScanSessionRepository:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    return ScanSessionRepository(layout.library_db)


def test_start_new_conflicts_when_active_session_exists(repo: ScanSessionRepository) -> None:
    service = ScanSessionService(repo)
    repo.create_session(run_kind="scan_full", status="running", triggered_by="manual_cli")

    with pytest.raises(ScanActiveConflictError):
        service.start_new(run_kind="scan_full", triggered_by="manual_cli")


def test_start_or_resume_resumes_latest_interrupted_when_no_active(repo: ScanSessionRepository) -> None:
    service = ScanSessionService(repo)
    older = repo.create_session(run_kind="scan_resume", status="interrupted", triggered_by="manual_cli")
    latest = repo.create_session(run_kind="scan_resume", status="interrupted", triggered_by="manual_cli")

    before_count = repo.count_sessions()
    resumed = service.start_or_resume(run_kind="scan_full", triggered_by="manual_cli")

    assert older.id < latest.id
    assert resumed.session_id == latest.id
    assert resumed.resumed is True
    assert repo.get_session(latest.id).status == "running"
    assert repo.count_sessions() == before_count


def test_start_or_resume_reuses_active_session_without_creating_new(repo: ScanSessionRepository) -> None:
    service = ScanSessionService(repo)
    active = repo.create_session(run_kind="scan_full", status="running", triggered_by="manual_cli")

    before_count = repo.count_sessions()
    reused = service.start_or_resume(run_kind="scan_incremental", triggered_by="manual_webui")

    assert reused.session_id == active.id
    assert reused.resumed is True
    assert repo.count_sessions() == before_count


def test_start_or_resume_creates_running_when_no_active_and_no_interrupted(repo: ScanSessionRepository) -> None:
    service = ScanSessionService(repo)

    created = service.start_or_resume(run_kind="scan_incremental", triggered_by="manual_webui")

    session = repo.get_session(created.session_id)
    assert created.resumed is False
    assert session.status == "running"
    assert session.run_kind == "scan_incremental"
    assert session.triggered_by == "manual_webui"


def test_start_new_abandons_interrupted_then_creates_new(repo: ScanSessionRepository) -> None:
    service = ScanSessionService(repo)
    old = repo.create_session(run_kind="scan_resume", status="interrupted", triggered_by="manual_cli")

    created = service.start_new(run_kind="scan_incremental", triggered_by="manual_webui")

    assert created.session_id != old.id
    assert repo.get_session(old.id).status == "abandoned"
    assert repo.get_session(created.session_id).status == "pending"


def test_start_new_rejects_invalid_run_kind(repo: ScanSessionRepository) -> None:
    service = ScanSessionService(repo)

    with pytest.raises(InvalidRunKindError, match="run_kind"):
        service.start_new(run_kind="invalid", triggered_by="manual_cli")


def test_assert_no_active_scan_for_serve_blocks_running_session(repo: ScanSessionRepository) -> None:
    repo.create_session(run_kind="scan_full", status="running", triggered_by="manual_cli")

    with pytest.raises(ServeBlockedByActiveScanError, match="serve"):
        assert_no_active_scan_for_serve(repo)


def test_assert_no_active_scan_for_serve_blocks_aborting_session(repo: ScanSessionRepository) -> None:
    repo.create_session(run_kind="scan_full", status="aborting", triggered_by="manual_cli")

    with pytest.raises(ServeBlockedByActiveScanError, match="serve"):
        assert_no_active_scan_for_serve(repo)


def test_scan_session_db_constraint_allows_only_one_active(repo: ScanSessionRepository) -> None:
    repo.create_session(run_kind="scan_full", status="running", triggered_by="manual_cli")

    with pytest.raises(sqlite3.IntegrityError):
        repo.create_session(run_kind="scan_incremental", status="aborting", triggered_by="manual_webui")


def test_start_or_resume_returns_active_when_integrity_error_happens() -> None:
    class RepoStub:
        def __init__(self) -> None:
            self.conn = sqlite3.connect(":memory:")
            self.call_count = 0
            self.interrupted = scan_models.ScanSessionRecord(
                id=11,
                run_kind="scan_resume",
                status="interrupted",
                triggered_by="manual_cli",
                resume_from_session_id=None,
                started_at=None,
                finished_at=None,
                last_error=None,
                created_at="2026-01-01T00:00:00",
                updated_at="2026-01-01T00:00:00",
            )
            self.active = scan_models.ScanSessionRecord(
                id=22,
                run_kind="scan_full",
                status="running",
                triggered_by="manual_webui",
                resume_from_session_id=None,
                started_at=None,
                finished_at=None,
                last_error=None,
                created_at="2026-01-01T00:00:01",
                updated_at="2026-01-01T00:00:01",
            )

        def connect(self) -> sqlite3.Connection:
            return self.conn

        def latest_by_status(self, statuses: set[str], *, conn: sqlite3.Connection | None = None):
            self.call_count += 1
            if self.call_count == 1 and statuses == {"running", "aborting"}:
                return None
            if self.call_count == 2 and statuses == {"interrupted"}:
                return self.interrupted
            if self.call_count == 3 and statuses == {"running", "aborting"}:
                return self.active
            return None

        def update_status(self, session_id: int, *, status: str, conn: sqlite3.Connection | None = None, **kwargs):
            raise sqlite3.IntegrityError("forced")

        def create_session(self, **kwargs):
            raise AssertionError("不应走到 create_session")

    repo = RepoStub()
    service = ScanSessionService(repo)  # type: ignore[arg-type]

    result = service.start_or_resume(run_kind="scan_full", triggered_by="manual_cli")

    assert result.resumed is True
    assert result.session_id == 22


def test_start_new_raises_conflict_when_integrity_error_happens() -> None:
    class RepoStub:
        def __init__(self) -> None:
            self.conn = sqlite3.connect(":memory:")
            self.call_count = 0
            self.active = scan_models.ScanSessionRecord(
                id=33,
                run_kind="scan_full",
                status="running",
                triggered_by="manual_cli",
                resume_from_session_id=None,
                started_at=None,
                finished_at=None,
                last_error=None,
                created_at="2026-01-01T00:00:02",
                updated_at="2026-01-01T00:00:02",
            )

        def connect(self) -> sqlite3.Connection:
            return self.conn

        def latest_by_status(self, statuses: set[str], *, conn: sqlite3.Connection | None = None):
            self.call_count += 1
            if self.call_count == 1 and statuses == {"running", "aborting"}:
                return None
            if self.call_count == 2 and statuses == {"interrupted"}:
                return None
            if self.call_count == 3 and statuses == {"running", "aborting"}:
                return self.active
            return None

        def update_status(self, *args, **kwargs):
            raise AssertionError("不应调用 update_status")

        def create_session(self, **kwargs):
            raise sqlite3.IntegrityError("forced")

    repo = RepoStub()
    service = ScanSessionService(repo)  # type: ignore[arg-type]

    with pytest.raises(ScanActiveConflictError) as exc:
        service.start_new(run_kind="scan_full", triggered_by="manual_webui")
    assert exc.value.active_session_id == 33
