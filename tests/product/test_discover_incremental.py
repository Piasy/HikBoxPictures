from __future__ import annotations

import sqlite3
from pathlib import Path

from hikbox_pictures.product.db.schema_bootstrap import bootstrap_library_schema
from hikbox_pictures.product.scan.discover_stage import DiscoverStage
from hikbox_pictures.product.scan.metadata_stage import MetadataStage
from hikbox_pictures.product.scan.models import AssetFileState


def _create_discover_tables(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_asset (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              library_source_id INTEGER NOT NULL,
              primary_path TEXT NOT NULL,
              primary_fingerprint TEXT NOT NULL,
              fingerprint_algo TEXT NOT NULL,
              file_size INTEGER NOT NULL,
              mtime_ns INTEGER NOT NULL,
              capture_datetime TEXT,
              capture_month TEXT,
              is_live_photo INTEGER NOT NULL DEFAULT 0,
              live_mov_path TEXT,
              live_mov_size INTEGER,
              live_mov_mtime_ns INTEGER,
              asset_status TEXT NOT NULL DEFAULT 'active',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(library_source_id, primary_path)
            )
            """
        )
        conn.commit()


def test_size_or_mtime_change_requires_full_stage_rerun(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "library.db"
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True)
    _create_discover_tables(db_path)

    stage = DiscoverStage(db_path)

    unchanged_old = AssetFileState(file_size=128, mtime_ns=100)
    unchanged_new = AssetFileState(file_size=128, mtime_ns=100)
    size_changed = AssetFileState(file_size=129, mtime_ns=100)
    mtime_changed = AssetFileState(file_size=128, mtime_ns=101)

    assert stage.should_rerun(unchanged_old, unchanged_new) is False
    assert stage.should_rerun(unchanged_old, size_changed) is True
    assert stage.should_rerun(unchanged_old, mtime_changed) is True

    photo = source_root / "IMG_0001.HEIC"
    photo.write_bytes(b"a" * 8)

    call_counter = {"count": 0}

    def _fake_sha256_for_file(_path: Path) -> str:
        call_counter["count"] += 1
        return f"fp-{call_counter['count']}"

    monkeypatch.setattr(
        "hikbox_pictures.product.scan.discover_stage.sha256_for_file",
        _fake_sha256_for_file,
    )

    first = stage.run(source_id=1, source_root=source_root)
    assert first.discovered_assets == 1
    assert first.rerun_assets == 1
    assert call_counter["count"] == 1
    with sqlite3.connect(db_path) as conn:
        first_fingerprint = conn.execute(
            "SELECT primary_fingerprint FROM photo_asset WHERE library_source_id=1 AND primary_path='IMG_0001.HEIC'",
        ).fetchone()
    assert first_fingerprint is not None
    assert first_fingerprint[0] != ""

    second = stage.run(source_id=1, source_root=source_root)
    assert second.discovered_assets == 1
    assert second.rerun_assets == 0
    assert call_counter["count"] == 1
    with sqlite3.connect(db_path) as conn:
        second_fingerprint = conn.execute(
            "SELECT primary_fingerprint FROM photo_asset WHERE library_source_id=1 AND primary_path='IMG_0001.HEIC'",
        ).fetchone()
    assert second_fingerprint is not None
    assert second_fingerprint[0] == first_fingerprint[0]

    photo.write_bytes(b"a" * 16)
    third = stage.run(source_id=1, source_root=source_root)
    assert third.discovered_assets == 1
    assert third.rerun_assets == 1
    assert call_counter["count"] == 2
    with sqlite3.connect(db_path) as conn:
        third_fingerprint = conn.execute(
            "SELECT primary_fingerprint FROM photo_asset WHERE library_source_id=1 AND primary_path='IMG_0001.HEIC'",
        ).fetchone()
    assert third_fingerprint is not None
    assert third_fingerprint[0] != ""
    assert third_fingerprint[0] != first_fingerprint[0]


def test_bootstrap_schema_supports_discover_and_metadata_without_missing_table(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True)
    photo = source_root / "IMG_0002.HEIF"
    photo.write_bytes(b"demo")

    bootstrap_library_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO library_source(root_path, label, enabled, status, last_discovered_at, created_at, updated_at)
            VALUES (?, 'src', 1, 'active', NULL, datetime('now'), datetime('now'))
            """,
            (str(source_root.resolve()),),
        )
        conn.commit()

    discover = DiscoverStage(db_path)
    metadata = MetadataStage(db_path)

    discover_summary = discover.run(source_id=1, source_root=source_root)
    metadata_summary = metadata.run(source_id=1, source_root=source_root)

    assert discover_summary.discovered_assets == 1
    assert metadata_summary.processed_assets == 1
