from pathlib import Path

import os
import re
import sqlite3

import pytest

from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.scan.discover_stage import DiscoverStageService
from hikbox_pictures.product.scan.errors import SessionNotFoundError, StageSchemaMissingError
from hikbox_pictures.product.scan.live_photo import match_live_mov
from hikbox_pictures.product.scan.metadata_stage import MetadataStageService, parse_capture_datetime
from hikbox_pictures.product.scan.session_service import ScanSessionRepository
from hikbox_pictures.product.source.repository import SourceRepository
from hikbox_pictures.product.source.service import SourceService


def test_match_live_photo_hidden_mov_patterns(tmp_path: Path) -> None:
    still = tmp_path / "IMG_7379.HEIF"
    mov = tmp_path / ".IMG_7379.HEIF_1771856408349261.MOV"
    still.write_bytes(b"x")
    mov.write_bytes(b"y")

    result = match_live_mov(still)

    assert result == mov


def test_match_live_photo_real_sample_from_tests_data() -> None:
    base = Path("tests/data/live-example")
    still = base / "IMG_6576.HEIC"

    matched = match_live_mov(still)

    assert matched == base / ".IMG_6576_1771856408444916.MOV"


def test_match_live_photo_returns_none_when_mov_missing(tmp_path: Path) -> None:
    still = tmp_path / "IMG_9000.HEIC"
    still.write_bytes(b"x")

    matched = match_live_mov(still)

    assert matched is None


def test_parse_capture_datetime_uses_expected_priority_and_capture_month() -> None:
    parsed = parse_capture_datetime(
        date_time_original="2024:07:10 11:12:13",
        date_time_digitized="2023:07:10 11:12:13",
        date_time="2022:07:10 11:12:13",
        fallback_mtime_ns=0,
        fallback_birthtime_ns=None,
    )
    assert parsed is not None
    assert parsed.strftime("%Y-%m") == "2024-07"
    assert parsed.utcoffset() is not None

    fallback = parse_capture_datetime(
        date_time_original=None,
        date_time_digitized=None,
        date_time=None,
        fallback_mtime_ns=1_714_928_800_000_000_000,
        fallback_birthtime_ns=None,
    )
    assert fallback is not None
    assert fallback.strftime("%Y-%m") == "2024-05"
    assert fallback.utcoffset() is not None


def test_parse_capture_datetime_fallbacks_to_birthtime_first() -> None:
    parsed = parse_capture_datetime(
        date_time_original=None,
        date_time_digitized=None,
        date_time=None,
        fallback_mtime_ns=1_700_000_000_000_000_000,
        fallback_birthtime_ns=1_600_000_000_000_000_000,
    )
    assert parsed is not None
    assert parsed.year == 2020


def test_match_live_photo_chooses_highest_token_then_latest_mtime(tmp_path: Path) -> None:
    still = tmp_path / "IMG_6666.HEIC"
    still.write_bytes(b"x")
    lower_token_newer = tmp_path / ".IMG_6666_20.MOV"
    higher_token_older = tmp_path / ".IMG_6666.HEIC_21.MOV"
    same_token_newest = tmp_path / ".IMG_6666_21.MOV"
    lower_token_newer.write_bytes(b"a")
    higher_token_older.write_bytes(b"b")
    same_token_newest.write_bytes(b"c")

    os.utime(higher_token_older, ns=(higher_token_older.stat().st_atime_ns, 1_700_000_000_000_000_000))
    os.utime(same_token_newest, ns=(same_token_newest.stat().st_atime_ns, 1_700_000_000_000_000_123))
    os.utime(lower_token_newer, ns=(lower_token_newer.stat().st_atime_ns, 1_800_000_000_000_000_000))

    matched = match_live_mov(still)
    assert matched == same_token_newest


def test_metadata_stage_writes_live_photo_fields_for_heic_asset(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    source_root = tmp_path / "photos"
    source_root.mkdir(parents=True)
    still = source_root / "IMG_6576.HEIC"
    mov = source_root / ".IMG_6576_1771856408444916.MOV"
    still.write_bytes(b"still")
    mov.write_bytes(b"mov-data")

    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    source_service = SourceService(SourceRepository(layout.library_db))
    source = source_service.add_source(str(source_root), label="family")
    session_repo = ScanSessionRepository(layout.library_db)
    session = session_repo.create_session(run_kind="scan_full", status="running", triggered_by="manual_cli")

    DiscoverStageService(layout.library_db).run(scan_session_id=session.id)
    MetadataStageService(layout.library_db).run(scan_session_id=session.id)

    conn = sqlite3.connect(layout.library_db)
    try:
        row = conn.execute(
            """
            SELECT is_live_photo, live_mov_path, live_mov_size, live_mov_mtime_ns, capture_month
            FROM photo_asset
            WHERE library_source_id = ? AND primary_path = ?
            """,
            (source.id, "IMG_6576.HEIC"),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row[0] == 1
    assert row[1] == ".IMG_6576_1771856408444916.MOV"
    assert row[2] == mov.stat().st_size
    assert isinstance(row[3], int)
    assert row[4] is not None


def test_metadata_stage_persists_iso8601_with_offset_and_failed_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    source_root = tmp_path / "photos"
    source_root.mkdir(parents=True)
    (source_root / "IMG_OK.HEIC").write_bytes(b"ok")
    (source_root / "IMG_BAD.HEIC").write_bytes(b"bad")

    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    source_service = SourceService(SourceRepository(layout.library_db))
    source = source_service.add_source(str(source_root), label="family")
    session = ScanSessionRepository(layout.library_db).create_session(
        run_kind="scan_full",
        status="running",
        triggered_by="manual_cli",
    )
    DiscoverStageService(layout.library_db).run(scan_session_id=session.id)

    from hikbox_pictures.product.scan import metadata_stage as metadata_module

    original = metadata_module._resolve_asset_capture_datetime

    def fake_resolve(path: Path, *, fallback_mtime_ns: int, fallback_birthtime_ns: int | None):
        if path.name == "IMG_BAD.HEIC":
            raise ValueError("simulated parse failure")
        return original(
            path,
            fallback_mtime_ns=fallback_mtime_ns,
            fallback_birthtime_ns=fallback_birthtime_ns,
        )

    monkeypatch.setattr(metadata_module, "_resolve_asset_capture_datetime", fake_resolve)
    MetadataStageService(layout.library_db).run(scan_session_id=session.id)

    conn = sqlite3.connect(layout.library_db)
    try:
        progress = conn.execute(
            """
            SELECT processed_assets, failed_assets
            FROM scan_session_source
            WHERE scan_session_id = ? AND library_source_id = ?
            """,
            (session.id, source.id),
        ).fetchone()
        dt_row = conn.execute(
            """
            SELECT capture_datetime
            FROM photo_asset
            WHERE library_source_id = ? AND primary_path = 'IMG_OK.HEIC'
            """,
            (source.id,),
        ).fetchone()
    finally:
        conn.close()

    assert progress == (1, 1)
    assert dt_row is not None
    assert dt_row[0] is not None
    assert re.search(r"[+-]\d{2}:\d{2}$", str(dt_row[0])) is not None


def test_metadata_raises_clear_error_when_required_tables_missing(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    source_root = tmp_path / "photos"
    source_root.mkdir(parents=True)
    (source_root / "IMG_A.HEIC").write_bytes(b"x")

    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    SourceService(SourceRepository(layout.library_db)).add_source(str(source_root), label="family")
    session = ScanSessionRepository(layout.library_db).create_session(
        run_kind="scan_full",
        status="running",
        triggered_by="manual_cli",
    )
    DiscoverStageService(layout.library_db).run(scan_session_id=session.id)

    conn = sqlite3.connect(layout.library_db)
    try:
        conn.execute("DROP TABLE scan_session_source")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(StageSchemaMissingError, match="scan_session_source"):
        MetadataStageService(layout.library_db).run(scan_session_id=session.id)


def test_metadata_raises_clear_error_when_scan_session_table_missing(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    source_root = tmp_path / "photos"
    source_root.mkdir(parents=True)
    (source_root / "IMG_B.HEIC").write_bytes(b"x")

    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    SourceService(SourceRepository(layout.library_db)).add_source(str(source_root), label="family")

    conn = sqlite3.connect(layout.library_db)
    try:
        conn.execute("DROP TABLE scan_session")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(StageSchemaMissingError, match="scan_session"):
        MetadataStageService(layout.library_db).run(scan_session_id=1)


def test_metadata_raises_domain_error_when_scan_session_not_found(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    source_root = tmp_path / "photos"
    source_root.mkdir(parents=True)
    (source_root / "IMG_C.HEIC").write_bytes(b"x")

    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    SourceService(SourceRepository(layout.library_db)).add_source(str(source_root), label="family")

    with pytest.raises(SessionNotFoundError, match="session_id=9999"):
        MetadataStageService(layout.library_db).run(scan_session_id=9999)


def test_metadata_marks_missing_asset_and_counts_failed_assets(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    source_root = tmp_path / "photos"
    source_root.mkdir(parents=True)
    still = source_root / "IMG_MISSING.HEIC"
    still.write_bytes(b"x")

    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    source = SourceService(SourceRepository(layout.library_db)).add_source(str(source_root), label="family")
    session = ScanSessionRepository(layout.library_db).create_session(
        run_kind="scan_full",
        status="running",
        triggered_by="manual_cli",
    )
    DiscoverStageService(layout.library_db).run(scan_session_id=session.id)
    still.unlink()

    summary = MetadataStageService(layout.library_db).run(scan_session_id=session.id)
    assert summary.by_source[source.id].failed_assets == 1
    assert summary.by_source[source.id].processed_assets == 0

    conn = sqlite3.connect(layout.library_db)
    try:
        asset_row = conn.execute(
            """
            SELECT asset_status
            FROM photo_asset
            WHERE library_source_id = ? AND primary_path = 'IMG_MISSING.HEIC'
            """,
            (source.id,),
        ).fetchone()
        progress_row = conn.execute(
            """
            SELECT failed_assets
            FROM scan_session_source
            WHERE scan_session_id = ? AND library_source_id = ?
            """,
            (session.id, source.id),
        ).fetchone()
    finally:
        conn.close()

    assert asset_row == ("missing",)
    assert progress_row == (1,)
