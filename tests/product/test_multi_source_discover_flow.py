from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hikbox_pictures.product.scan.discover_stage import DiscoverStage


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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scan_session_source (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              scan_session_id INTEGER NOT NULL,
              library_source_id INTEGER NOT NULL,
              stage_status_json TEXT NOT NULL,
              processed_assets INTEGER NOT NULL DEFAULT 0,
              failed_assets INTEGER NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL,
              UNIQUE(scan_session_id, library_source_id)
            )
            """
        )
        conn.commit()


def test_discover_tracks_each_source_independently(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    _create_discover_tables(db_path)

    source1_root = tmp_path / "family"
    source2_root = tmp_path / "travel"
    source1_root.mkdir(parents=True)
    source2_root.mkdir(parents=True)

    (source1_root / "IMG_1001.HEIC").write_bytes(b"f1")
    (source2_root / "IMG_2001.HEIF").write_bytes(b"t1")

    stage = DiscoverStage(db_path)
    summary = stage.run_for_sources(
        scan_session_id=9,
        sources={1: source1_root, 2: source2_root},
    )

    assert summary.by_source[1].discovered_assets == 1
    assert summary.by_source[2].discovered_assets == 1

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT library_source_id, stage_status_json, processed_assets, failed_assets
            FROM scan_session_source
            WHERE scan_session_id=?
            ORDER BY library_source_id
            """,
            (9,),
        ).fetchall()

    assert len(rows) == 2
    assert rows[0][0] == 1
    assert rows[1][0] == 2
    assert rows[0][2] == 1
    assert rows[1][2] == 1
    assert rows[0][3] == 0
    assert rows[1][3] == 0

    stage_status_1 = json.loads(rows[0][1])
    stage_status_2 = json.loads(rows[1][1])
    assert stage_status_1["discover"] == "completed"
    assert stage_status_2["discover"] == "completed"

    (source1_root / "IMG_1001.HEIC").write_bytes(b"f1-changed")
    second_summary = stage.run_for_sources(
        scan_session_id=9,
        sources={1: source1_root, 2: source2_root},
    )

    assert second_summary.by_source[1].rerun_assets == 1
    assert second_summary.by_source[2].rerun_assets == 0
