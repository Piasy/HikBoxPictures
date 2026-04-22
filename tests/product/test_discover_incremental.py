import os
from pathlib import Path

import pytest
import sqlite3

from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.scan.discover_stage import DiscoverStageService
from hikbox_pictures.product.scan.errors import StageSchemaMissingError
from hikbox_pictures.product.scan.session_service import ScanSessionRepository
from hikbox_pictures.product.source.repository import SourceRepository
from hikbox_pictures.product.source.service import SourceService


def test_size_or_mtime_change_requires_full_stage_rerun(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    source_root = tmp_path / "photos"
    source_root.mkdir(parents=True)

    still = source_root / "IMG_0001.HEIC"
    still.write_bytes(b"1234")

    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    source_service = SourceService(SourceRepository(layout.library_db))
    source = source_service.add_source(str(source_root), label="family")

    session_repo = ScanSessionRepository(layout.library_db)
    session = session_repo.create_session(run_kind="scan_incremental", status="running", triggered_by="manual_cli")

    discover = DiscoverStageService(layout.library_db)
    discover.run(scan_session_id=session.id)

    still.write_bytes(b"123456")
    size_changed = discover.run(scan_session_id=session.id)
    assert size_changed.by_source[source.id].should_rerun is True

    current = still.stat()
    os.utime(still, ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000))

    mtime_changed = discover.run(scan_session_id=session.id)
    assert mtime_changed.by_source[source.id].should_rerun is True


def test_discover_failed_assets_is_counted_and_persisted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    source_root = tmp_path / "photos"
    source_root.mkdir(parents=True)
    good = source_root / "IMG_0002.HEIC"
    bad = source_root / "IMG_0003.HEIC"
    good.write_bytes(b"good")
    bad.write_bytes(b"bad")

    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    source_service = SourceService(SourceRepository(layout.library_db))
    source = source_service.add_source(str(source_root), label="family")
    session = ScanSessionRepository(layout.library_db).create_session(
        run_kind="scan_incremental",
        status="running",
        triggered_by="manual_cli",
    )

    from hikbox_pictures.product.scan import discover_stage as discover_module

    original = discover_module.sha256_file

    def fake_sha(path: Path) -> str:
        if path.name == "IMG_0003.HEIC":
            raise OSError("simulated hash failure")
        return original(path)

    monkeypatch.setattr(discover_module, "sha256_file", fake_sha)
    summary = DiscoverStageService(layout.library_db).run(scan_session_id=session.id)
    assert summary.by_source[source.id].failed_assets == 1

    conn = sqlite3.connect(layout.library_db)
    try:
        row = conn.execute(
            """
            SELECT processed_assets, failed_assets
            FROM scan_session_source
            WHERE scan_session_id = ? AND library_source_id = ?
            """,
            (session.id, source.id),
        ).fetchone()
    finally:
        conn.close()

    assert row == (1, 1)


def test_discover_raises_clear_error_when_required_tables_missing(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    source_root = tmp_path / "photos"
    source_root.mkdir(parents=True)
    (source_root / "IMG_0010.HEIC").write_bytes(b"x")

    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    SourceService(SourceRepository(layout.library_db)).add_source(str(source_root), label="family")
    session = ScanSessionRepository(layout.library_db).create_session(
        run_kind="scan_incremental",
        status="running",
        triggered_by="manual_cli",
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        conn.execute("DROP TABLE photo_asset")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(StageSchemaMissingError, match="photo_asset"):
        DiscoverStageService(layout.library_db).run(scan_session_id=session.id)


def test_discover_raises_clear_error_when_scan_session_table_missing(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    source_root = tmp_path / "photos"
    source_root.mkdir(parents=True)
    (source_root / "IMG_0011.HEIC").write_bytes(b"x")

    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    SourceService(SourceRepository(layout.library_db)).add_source(str(source_root), label="family")

    conn = sqlite3.connect(layout.library_db)
    try:
        conn.execute("DROP TABLE scan_session")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(StageSchemaMissingError, match="scan_session"):
        DiscoverStageService(layout.library_db).run(scan_session_id=1)
