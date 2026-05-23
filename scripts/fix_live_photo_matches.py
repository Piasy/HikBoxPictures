#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
import shutil
import sqlite3
import sys
import time
from collections.abc import Callable
from typing import NamedTuple


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hikbox_pictures.product.scan_shared import LIVE_PHOTO_STILL_SUFFIXES
from hikbox_pictures.product.scan import _LivePhotoMovResolver
from hikbox_pictures.product.scan_shared import utc_now_text


class Correction(NamedTuple):
    asset_id: int
    absolute_path: str
    stored_mov_path: str | None
    expected_mov_path: str | None
    action: str


class ReconcileResult(NamedTuple):
    scanned: int
    changed: int
    skipped_missing: int
    skipped_unsupported: int
    errors: list[str]
    corrections: list[Correction]
    backup_path: str | None


class ReconcileInterrupted(Exception):
    def __init__(self, result: ReconcileResult) -> None:
        super().__init__("Live Photo 纠正过程已中断")
        self.result = result


def reconcile_live_photo_matches(
    library_db: Path,
    *,
    apply: bool,
    backup: bool = True,
    only_existing_live: bool = False,
    live_photo_finder: Callable[[Path], str | None] | None = None,
    progress_interval_seconds: float | None = None,
    clock: Callable[[], float] | None = None,
) -> ReconcileResult:
    library_db = library_db.expanduser().resolve()
    rows = _load_asset_rows(library_db, only_existing_live=only_existing_live)
    total_rows = len(rows)
    if live_photo_finder is None:
        live_photo_finder = _LivePhotoMovResolver().find
    if clock is None:
        clock = time.monotonic

    scanned = 0
    skipped_missing = 0
    skipped_unsupported = 0
    errors: list[str] = []
    corrections: list[Correction] = []
    last_progress_at = clock()

    try:
        for processed, row in enumerate(rows, start=1):
            asset_id = int(row["id"])
            absolute_path = Path(str(row["absolute_path"]))
            if absolute_path.suffix.lower() not in LIVE_PHOTO_STILL_SUFFIXES:
                skipped_unsupported += 1
            elif not _is_readable_file(absolute_path):
                skipped_missing += 1
            else:
                scanned += 1
                try:
                    expected_mov_path = _normalize_db_path(live_photo_finder(absolute_path))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"asset_id={asset_id} path={absolute_path}: {exc}")
                else:
                    stored_mov_path = _normalize_db_path(row["live_photo_mov_path"])
                    if stored_mov_path != expected_mov_path:
                        corrections.append(
                            Correction(
                                asset_id=asset_id,
                                absolute_path=str(absolute_path),
                                stored_mov_path=stored_mov_path,
                                expected_mov_path=expected_mov_path,
                                action=_correction_action(stored_mov_path, expected_mov_path),
                            )
                        )
            last_progress_at = _maybe_print_progress(
                progress_interval_seconds=progress_interval_seconds,
                clock=clock,
                last_progress_at=last_progress_at,
                processed=processed,
                total=total_rows,
                scanned=scanned,
                changed=len(corrections),
                skipped_missing=skipped_missing,
                skipped_unsupported=skipped_unsupported,
                error_count=len(errors),
            )
    except KeyboardInterrupt as exc:
        raise ReconcileInterrupted(
            _build_reconcile_result(
                scanned=scanned,
                skipped_missing=skipped_missing,
                skipped_unsupported=skipped_unsupported,
                errors=errors,
                corrections=corrections,
                backup_path=None,
            )
        ) from exc

    backup_path: str | None = None
    if apply and corrections:
        if backup:
            backup_path = str(_backup_library_db(library_db))
        _apply_corrections(library_db, corrections)

    return _build_reconcile_result(
        scanned=scanned,
        skipped_missing=skipped_missing,
        skipped_unsupported=skipped_unsupported,
        errors=errors,
        corrections=corrections,
        backup_path=backup_path,
    )


def _build_reconcile_result(
    *,
    scanned: int,
    skipped_missing: int,
    skipped_unsupported: int,
    errors: list[str],
    corrections: list[Correction],
    backup_path: str | None,
) -> ReconcileResult:
    return ReconcileResult(
        scanned=scanned,
        changed=len(corrections),
        skipped_missing=skipped_missing,
        skipped_unsupported=skipped_unsupported,
        errors=errors,
        corrections=corrections,
        backup_path=backup_path,
    )


def _maybe_print_progress(
    *,
    progress_interval_seconds: float | None,
    clock: Callable[[], float],
    last_progress_at: float,
    processed: int,
    total: int,
    scanned: int,
    changed: int,
    skipped_missing: int,
    skipped_unsupported: int,
    error_count: int,
) -> float:
    if progress_interval_seconds is None or progress_interval_seconds <= 0:
        return last_progress_at
    now = clock()
    if now - last_progress_at < progress_interval_seconds:
        return last_progress_at
    _print_progress(
        processed=processed,
        total=total,
        scanned=scanned,
        changed=changed,
        skipped_missing=skipped_missing,
        skipped_unsupported=skipped_unsupported,
        error_count=error_count,
    )
    return now


def _print_progress(
    *,
    processed: int,
    total: int,
    scanned: int,
    changed: int,
    skipped_missing: int,
    skipped_unsupported: int,
    error_count: int,
) -> None:
    print(
        f"进度: 已处理 {processed}/{total} 行，已读取照片 {scanned}，"
        f"待纠正记录 {changed}，跳过缺失/不可读照片 {skipped_missing}，"
        f"跳过不支持类型 {skipped_unsupported}，读取失败 {error_count}",
        flush=True,
    )


def _load_asset_rows(library_db: Path, *, only_existing_live: bool) -> list[sqlite3.Row]:
    if not library_db.is_file():
        raise FileNotFoundError(f"找不到 library.db：{library_db}")
    suffixes = sorted(suffix.lstrip(".") for suffix in LIVE_PHOTO_STILL_SUFFIXES)
    placeholders = ", ".join("?" for _ in suffixes)
    where_clause = f"LOWER(file_extension) IN ({placeholders})"
    if only_existing_live:
        where_clause += " AND live_photo_mov_path IS NOT NULL"
    connection = sqlite3.connect(library_db)
    connection.row_factory = sqlite3.Row
    try:
        return list(
            connection.execute(
                f"""
                SELECT id, absolute_path, file_extension, live_photo_mov_path
                FROM assets
                WHERE {where_clause}
                ORDER BY id ASC
                """,
                suffixes,
            ).fetchall()
        )
    finally:
        connection.close()


def _apply_corrections(library_db: Path, corrections: list[Correction]) -> None:
    now = utc_now_text()
    connection = sqlite3.connect(library_db)
    try:
        connection.executemany(
            """
            UPDATE assets
            SET live_photo_mov_path = ?, updated_at = ?
            WHERE id = ?
            """,
            [(item.expected_mov_path, now, item.asset_id) for item in corrections],
        )
        connection.commit()
    finally:
        connection.close()


def _backup_library_db(library_db: Path) -> Path:
    timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = library_db.with_name(f"{library_db.name}.fix-live-photo-{timestamp}.bak")
    counter = 1
    while backup_path.exists():
        backup_path = library_db.with_name(f"{library_db.name}.fix-live-photo-{timestamp}-{counter}.bak")
        counter += 1
    shutil.copy2(library_db, backup_path)
    return backup_path


def _is_readable_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _normalize_db_path(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return str(Path(text).expanduser().resolve())


def _correction_action(stored_mov_path: str | None, expected_mov_path: str | None) -> str:
    if stored_mov_path is None and expected_mov_path is not None:
        return "set"
    if stored_mov_path is not None and expected_mov_path is None:
        return "clear"
    return "update"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按当前 Live Photo 识别规则重新计算 assets.live_photo_mov_path。"
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--workspace", type=Path, help="HikBoxPictures 工作区路径")
    target.add_argument("--library-db", type=Path, help="直接指定 .hikbox/library.db 路径")
    parser.add_argument("--apply", action="store_true", help="实际更新数据库；默认只 dry-run")
    parser.add_argument("--no-backup", action="store_true", help="apply 时不备份 library.db")
    parser.add_argument(
        "--only-existing-live",
        action="store_true",
        help="只纠正当前 live_photo_mov_path 非空的记录",
    )
    parser.add_argument(
        "--max-print",
        type=int,
        default=200,
        help="最多打印多少条待修改记录，默认 200",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    library_db = _resolve_library_db(args)
    try:
        result = reconcile_live_photo_matches(
            library_db,
            apply=bool(args.apply),
            backup=not bool(args.no_backup),
            only_existing_live=bool(args.only_existing_live),
            progress_interval_seconds=10.0,
        )
    except ReconcileInterrupted as exc:
        print("已中断，打印退出前已经发现的待纠正记录；数据库未修改。")
        _print_result(exc.result, apply=False, max_print=max(0, int(args.max_print)), interrupted=True)
        return 130
    _print_result(result, apply=bool(args.apply), max_print=max(0, int(args.max_print)))
    return 1 if result.errors else 0


def _resolve_library_db(args: argparse.Namespace) -> Path:
    if args.library_db is not None:
        return Path(args.library_db).expanduser().resolve()
    workspace = Path(args.workspace).expanduser().resolve()
    return workspace / ".hikbox" / "library.db"


def _print_result(result: ReconcileResult, *, apply: bool, max_print: int, interrupted: bool = False) -> None:
    mode = "INTERRUPTED" if interrupted else ("APPLY" if apply else "DRY-RUN")
    print(f"模式: {mode}")
    print(f"已读取照片: {result.scanned}")
    print(f"待纠正记录: {result.changed}")
    print(f"跳过缺失/不可读照片: {result.skipped_missing}")
    print(f"跳过不支持类型: {result.skipped_unsupported}")
    if result.backup_path is not None:
        print(f"数据库备份: {result.backup_path}")
    if result.corrections:
        print("待修改明细:")
        for item in result.corrections[:max_print]:
            print(
                f"- id={item.asset_id} action={item.action} "
                f"path={item.absolute_path} old={item.stored_mov_path} new={item.expected_mov_path}"
            )
        remaining = len(result.corrections) - max_print
        if remaining > 0:
            print(f"... 还有 {remaining} 条未打印")
    if result.errors:
        print("读取失败:")
        for error in result.errors:
            print(f"- {error}")
    if result.changed and not apply and not interrupted:
        print("当前未修改数据库；确认无误后加 --apply 执行纠正。")


if __name__ == "__main__":
    raise SystemExit(main())
