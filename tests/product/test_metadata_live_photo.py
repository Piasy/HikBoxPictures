from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from hikbox_pictures.product.scan.live_photo import match_live_photo_mov
from hikbox_pictures.product.scan.metadata_stage import MetadataStage


def _create_photo_asset_table(db_path: Path) -> None:
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


def _insert_asset(db_path: Path, *, source_id: int, rel_path: str, size: int, mtime_ns: int) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO photo_asset(
              library_source_id,
              primary_path,
              primary_fingerprint,
              fingerprint_algo,
              file_size,
              mtime_ns,
              capture_datetime,
              capture_month,
              is_live_photo,
              live_mov_path,
              live_mov_size,
              live_mov_mtime_ns,
              asset_status,
              created_at,
              updated_at
            )
            VALUES (?, ?, '', 'sha256', ?, ?, NULL, NULL, 0, NULL, NULL, NULL, 'active', datetime('now'), datetime('now'))
            """,
            (source_id, rel_path, size, mtime_ns),
        )
        conn.commit()


def test_match_live_photo_hidden_mov_patterns(tmp_path: Path) -> None:
    still_heif = tmp_path / "IMG_7379.HEIF"
    mov_with_ext = tmp_path / ".IMG_7379.HEIF_1771856408349261.MOV"
    mov_without_ext = tmp_path / ".IMG_7380_1771856408349261.MOV"
    still_heic = tmp_path / "IMG_7380.HEIC"
    jpeg = tmp_path / "IMG_7381.JPG"

    still_heif.write_bytes(b"heif")
    mov_with_ext.write_bytes(b"mov-ext")
    mov_without_ext.write_bytes(b"mov-noext")
    still_heic.write_bytes(b"heic")
    jpeg.write_bytes(b"jpeg")

    matched_heif = match_live_photo_mov(still_heif)
    matched_heic = match_live_photo_mov(still_heic)
    matched_jpeg = match_live_photo_mov(jpeg)

    assert matched_heif is not None
    assert matched_heif.name == mov_with_ext.name
    assert matched_heic is not None
    assert matched_heic.name == mov_without_ext.name
    assert matched_jpeg is None


def test_match_live_photo_hidden_mov_patterns_chooses_latest_timestamp_then_mtime(tmp_path: Path) -> None:
    still = tmp_path / "IMG_8001.HEIF"
    still.write_bytes(b"still")

    candidate_ts_old = tmp_path / ".IMG_8001.HEIF_100.MOV"
    candidate_ts_new = tmp_path / ".IMG_8001.HEIF_200.MOV"
    candidate_same_ts_high_mtime = tmp_path / ".IMG_8001_200.MOV"
    candidate_same_rank_name_b = tmp_path / ".IMG_8001_300.MOV"
    candidate_same_rank_name_a = tmp_path / ".IMG_8001.HEIF_300.MOV"
    for item in (
        candidate_ts_old,
        candidate_ts_new,
        candidate_same_ts_high_mtime,
        candidate_same_rank_name_b,
        candidate_same_rank_name_a,
    ):
        item.write_bytes(item.name.encode("utf-8"))

    os.utime(candidate_ts_old, ns=(100, 100))
    os.utime(candidate_ts_new, ns=(200, 200))
    os.utime(candidate_same_ts_high_mtime, ns=(999, 999))
    os.utime(candidate_same_rank_name_b, ns=(777, 777))
    os.utime(candidate_same_rank_name_a, ns=(777, 777))

    matched = match_live_photo_mov(still)

    assert matched is not None
    assert matched.name == candidate_same_rank_name_a.name


def test_metadata_stage_sets_capture_month_and_live_mov_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True)

    still = source_root / "IMG_7379.HEIF"
    still.write_bytes(b"still")
    fixed_mtime_ns = 1_704_614_400_000_000_000
    os.utime(still, ns=(fixed_mtime_ns, fixed_mtime_ns))
    mov = source_root / ".IMG_7379.HEIF_1771856408349261.MOV"
    mov.write_bytes(b"mov")

    _create_photo_asset_table(db_path)
    _insert_asset(
        db_path,
        source_id=11,
        rel_path="IMG_7379.HEIF",
        size=still.stat().st_size,
        mtime_ns=still.stat().st_mtime_ns,
    )

    stage = MetadataStage(db_path)
    summary = stage.run(source_id=11, source_root=source_root)

    assert summary.processed_assets == 1
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT capture_datetime, capture_month, is_live_photo, live_mov_path, live_mov_size, live_mov_mtime_ns
            FROM photo_asset
            WHERE library_source_id=? AND primary_path=?
            """,
            (11, "IMG_7379.HEIF"),
        ).fetchone()

    assert row is not None
    capture_datetime, capture_month, is_live_photo, live_mov_path, live_mov_size, live_mov_mtime_ns = row
    assert isinstance(capture_datetime, str)
    expected_month = datetime.fromtimestamp(fixed_mtime_ns / 1_000_000_000, tz=UTC).strftime("%Y-%m")
    assert capture_month == expected_month
    assert is_live_photo == 1
    assert live_mov_path == ".IMG_7379.HEIF_1771856408349261.MOV"
    assert live_mov_size == mov.stat().st_size
    assert live_mov_mtime_ns == mov.stat().st_mtime_ns
