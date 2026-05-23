from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "fix_live_photo_matches.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("fix_live_photo_matches", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_library_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE assets (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              source_id INTEGER NOT NULL,
              absolute_path TEXT NOT NULL UNIQUE,
              file_name TEXT NOT NULL,
              file_extension TEXT NOT NULL,
              capture_month TEXT NOT NULL,
              file_fingerprint TEXT NOT NULL,
              live_photo_mov_path TEXT,
              processing_status TEXT NOT NULL,
              failure_reason TEXT,
              scan_retry_count INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )
        connection.commit()
    finally:
        connection.close()


def _insert_asset(
    db_path: Path,
    *,
    absolute_path: Path,
    live_photo_mov_path: str | None,
) -> int:
    connection = sqlite3.connect(db_path)
    try:
        cursor = connection.execute(
            """
            INSERT INTO assets (
              source_id, absolute_path, file_name, file_extension, capture_month,
              file_fingerprint, live_photo_mov_path, processing_status,
              failure_reason, scan_retry_count, created_at, updated_at
            )
            VALUES (1, ?, ?, ?, '2025-01', 'fingerprint', ?, 'succeeded', NULL, 0, 'old', 'old')
            """,
            (
                str(absolute_path),
                absolute_path.name,
                absolute_path.suffix.lower().lstrip("."),
                live_photo_mov_path,
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)
    finally:
        connection.close()


def _fetch_live_photo_path(db_path: Path, asset_id: int) -> str | None:
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(
            "SELECT live_photo_mov_path FROM assets WHERE id = ?",
            (asset_id,),
        ).fetchone()[0]
    finally:
        connection.close()


def test_reconcile_live_photo_matches_updates_wrong_and_missing_matches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_script_module()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    heic_path = source_dir / "IMG_4721.heic"
    jpg_path = source_dir / "IMG_4721.JPG"
    missing_path = source_dir / "IMG_9999.heic"
    wrong_mov_path = source_dir / ".IMG_4721_1744532070331526.MOV"
    heic_path.write_bytes(b"heic")
    jpg_path.write_bytes(b"jpg")
    wrong_mov_path.write_bytes(b"mov")
    db_path = tmp_path / "library.db"
    _create_library_db(db_path)
    heic_id = _insert_asset(
        db_path,
        absolute_path=heic_path,
        live_photo_mov_path=str(wrong_mov_path),
    )
    jpg_id = _insert_asset(
        db_path,
        absolute_path=jpg_path,
        live_photo_mov_path=None,
    )
    missing_id = _insert_asset(
        db_path,
        absolute_path=missing_path,
        live_photo_mov_path=str(source_dir / ".IMG_9999.MOV"),
    )

    monkeypatch.setattr(module, "utc_now_text", lambda: "2026-05-11T00:00:00Z")

    result = module.reconcile_live_photo_matches(
        db_path,
        apply=True,
        backup=False,
        live_photo_finder=lambda path: str(wrong_mov_path.resolve()) if Path(path) == jpg_path else None,
    )

    assert result.changed == 2
    assert result.skipped_missing == 1
    assert _fetch_live_photo_path(db_path, heic_id) is None
    assert _fetch_live_photo_path(db_path, jpg_id) == str(wrong_mov_path.resolve())
    assert _fetch_live_photo_path(db_path, missing_id) == str(source_dir / ".IMG_9999.MOV")


def test_reconcile_live_photo_matches_dry_run_does_not_update_db(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_script_module()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    image_path = source_dir / "IMG_0001.heic"
    image_path.write_bytes(b"heic")
    mov_path = source_dir / ".IMG_0001.MOV"
    mov_path.write_bytes(b"mov")
    db_path = tmp_path / "library.db"
    _create_library_db(db_path)
    asset_id = _insert_asset(db_path, absolute_path=image_path, live_photo_mov_path=None)

    result = module.reconcile_live_photo_matches(
        db_path,
        apply=False,
        backup=False,
        live_photo_finder=lambda _path: str(mov_path.resolve()),
    )

    assert result.changed == 1
    assert _fetch_live_photo_path(db_path, asset_id) is None


def test_reconcile_live_photo_matches_prints_progress_every_10_seconds(
    tmp_path: Path,
    capsys,
) -> None:
    module = _load_script_module()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    db_path = tmp_path / "library.db"
    _create_library_db(db_path)
    for index in range(3):
        image_path = source_dir / f"IMG_{index:04d}.heic"
        image_path.write_bytes(b"heic")
        _insert_asset(db_path, absolute_path=image_path, live_photo_mov_path=None)

    elapsed = {"seconds": 0.0}

    def fake_clock() -> float:
        return elapsed["seconds"]

    def slow_live_photo_finder(_path: Path) -> str | None:
        elapsed["seconds"] += 5.0
        return None

    result = module.reconcile_live_photo_matches(
        db_path,
        apply=False,
        backup=False,
        live_photo_finder=slow_live_photo_finder,
        progress_interval_seconds=10.0,
        clock=fake_clock,
    )

    assert result.scanned == 3
    progress_line = (
        "进度: 已处理 2/3 行，已读取照片 2，待纠正记录 0，"
        "跳过缺失/不可读照片 0，跳过不支持类型 0，读取失败 0"
    )
    assert capsys.readouterr().out.splitlines() == [progress_line]


def test_main_prints_progress_every_10_seconds_by_default(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    module = _load_script_module()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    db_path = tmp_path / "library.db"
    _create_library_db(db_path)
    for index in range(3):
        image_path = source_dir / f"IMG_{index:04d}.heic"
        image_path.write_bytes(b"heic")
        _insert_asset(db_path, absolute_path=image_path, live_photo_mov_path=None)

    elapsed = {"seconds": 0.0}

    def fake_clock() -> float:
        return elapsed["seconds"]

    class SlowLivePhotoMovResolver:
        def find(self, _path: Path) -> str | None:
            elapsed["seconds"] += 5.0
            return None

    monkeypatch.setattr(module.time, "monotonic", fake_clock)
    monkeypatch.setattr(module, "_LivePhotoMovResolver", SlowLivePhotoMovResolver)

    exit_code = module.main(["--library-db", str(db_path)])

    assert exit_code == 0
    progress_line = (
        "进度: 已处理 2/3 行，已读取照片 2，待纠正记录 0，"
        "跳过缺失/不可读照片 0，跳过不支持类型 0，读取失败 0"
    )
    assert progress_line in capsys.readouterr().out.splitlines()


def test_main_prints_discovered_corrections_when_interrupted(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    module = _load_script_module()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    first_image_path = source_dir / "IMG_0001.heic"
    second_image_path = source_dir / "IMG_0002.heic"
    first_image_path.write_bytes(b"heic")
    second_image_path.write_bytes(b"heic")
    mov_path = source_dir / ".IMG_0001.MOV"
    mov_path.write_bytes(b"mov")
    db_path = tmp_path / "library.db"
    _create_library_db(db_path)
    first_asset_id = _insert_asset(db_path, absolute_path=first_image_path, live_photo_mov_path=None)
    _insert_asset(db_path, absolute_path=second_image_path, live_photo_mov_path=None)

    class InterruptingLivePhotoMovResolver:
        def find(self, path: Path) -> str | None:
            if Path(path) == first_image_path:
                return str(mov_path.resolve())
            raise KeyboardInterrupt

    monkeypatch.setattr(module, "_LivePhotoMovResolver", InterruptingLivePhotoMovResolver)

    exit_code = module.main(["--library-db", str(db_path)])

    output_lines = capsys.readouterr().out.splitlines()
    assert exit_code == 130
    assert "已中断，打印退出前已经发现的待纠正记录；数据库未修改。" in output_lines
    assert "待纠正记录: 1" in output_lines
    assert "待修改明细:" in output_lines
    assert (
        f"- id={first_asset_id} action=set path={first_image_path} old=None new={mov_path.resolve()}"
        in output_lines
    )
    assert _fetch_live_photo_path(db_path, first_asset_id) is None
