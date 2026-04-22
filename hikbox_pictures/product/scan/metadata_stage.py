"""metadata 阶段：提取时间并补齐 Live Photo 字段。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from hikbox_pictures.product.db.connection import connect_sqlite
from hikbox_pictures.product.scan.errors import StageSchemaMissingError
from hikbox_pictures.product.scan.live_photo import pick_best_live_mov
from hikbox_pictures.product.scan.models import MetadataSourceSummary, MetadataStageSummary

try:
    import pillow_heif
except ImportError:  # pragma: no cover - 运行时可选依赖
    pillow_heif = None

if pillow_heif is not None:
    pillow_heif.register_heif_opener()

REQUIRED_TABLES = {"library_source", "photo_asset", "scan_session_source", "scan_session"}


def parse_capture_datetime(
    *,
    date_time_original: str | None,
    date_time_digitized: str | None,
    date_time: str | None,
    fallback_mtime_ns: int | None,
    fallback_birthtime_ns: int | None,
) -> datetime | None:
    """按优先级解析拍摄时间并补齐时区。"""
    for raw in (date_time_original, date_time_digitized, date_time):
        parsed = _parse_datetime_text(raw)
        if parsed is not None:
            return _ensure_local_timezone(parsed)

    if fallback_birthtime_ns is not None:
        return datetime.fromtimestamp(fallback_birthtime_ns / 1_000_000_000, tz=_local_tz())
    if fallback_mtime_ns is not None:
        return datetime.fromtimestamp(fallback_mtime_ns / 1_000_000_000, tz=_local_tz())
    return None


class MetadataStageService:
    """metadata 阶段服务。"""

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)

    def run(self, *, scan_session_id: int) -> MetadataStageSummary:
        conn = connect_sqlite(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            self._assert_required_tables(conn)
            sources = conn.execute(
                """
                SELECT id, root_path
                FROM library_source
                WHERE enabled = 1 AND removed_at IS NULL
                ORDER BY id ASC
                """
            ).fetchall()

            by_source: dict[int, MetadataSourceSummary] = {}
            for source_row in sources:
                source_id = int(source_row[0])
                source_root = Path(str(source_row[1]))
                summary = self._process_source(conn, source_id=source_id, source_root=source_root)
                self._upsert_source_progress(conn, scan_session_id=scan_session_id, summary=summary)
                by_source[source_id] = summary

            conn.commit()
            return MetadataStageSummary(by_source=by_source)
        finally:
            conn.close()

    def _process_source(self, conn: sqlite3.Connection, *, source_id: int, source_root: Path) -> MetadataSourceSummary:
        rows = conn.execute(
            """
            SELECT id, primary_path, mtime_ns
            FROM photo_asset
            WHERE library_source_id = ? AND asset_status = 'active'
            ORDER BY id ASC
            """,
            (source_id,),
        ).fetchall()

        processed_assets = 0
        failed_assets = 0
        live_photo_assets = 0
        directory_entries_cache: dict[Path, list[Path]] = {}

        for row in rows:
            asset_id = int(row[0])
            rel_path = str(row[1])
            mtime_ns = int(row[2])
            full_path = source_root / rel_path
            if not full_path.exists():
                continue

            try:
                stat = full_path.stat()
                birthtime_ns = _extract_birthtime_ns(stat)
                dt = _resolve_asset_capture_datetime(
                    full_path,
                    fallback_mtime_ns=mtime_ns,
                    fallback_birthtime_ns=birthtime_ns,
                )
                capture_datetime = dt.isoformat(timespec="seconds") if dt is not None else None
                capture_month = dt.strftime("%Y-%m") if dt is not None else None

                parent = full_path.parent
                directory_entries = directory_entries_cache.get(parent)
                if directory_entries is None:
                    directory_entries = [entry for entry in parent.iterdir() if entry.is_file()]
                    directory_entries_cache[parent] = directory_entries

                mov_path = pick_best_live_mov(full_path, directory_entries)
                if mov_path is not None:
                    mov_stat = mov_path.stat()
                    is_live_photo = 1
                    live_mov_path = mov_path.relative_to(source_root).as_posix()
                    live_mov_size = int(mov_stat.st_size)
                    live_mov_mtime_ns = int(mov_stat.st_mtime_ns)
                    live_photo_assets += 1
                else:
                    is_live_photo = 0
                    live_mov_path = None
                    live_mov_size = None
                    live_mov_mtime_ns = None

                conn.execute(
                    """
                    UPDATE photo_asset
                    SET capture_datetime = ?,
                        capture_month = ?,
                        is_live_photo = ?,
                        live_mov_path = ?,
                        live_mov_size = ?,
                        live_mov_mtime_ns = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        capture_datetime,
                        capture_month,
                        is_live_photo,
                        live_mov_path,
                        live_mov_size,
                        live_mov_mtime_ns,
                        asset_id,
                    ),
                )
                processed_assets += 1
            except (OSError, ValueError):
                failed_assets += 1

        return MetadataSourceSummary(
            source_id=source_id,
            processed_assets=processed_assets,
            failed_assets=failed_assets,
            live_photo_assets=live_photo_assets,
        )

    def _upsert_source_progress(
        self,
        conn: sqlite3.Connection,
        *,
        scan_session_id: int,
        summary: MetadataSourceSummary,
    ) -> None:
        existing = conn.execute(
            """
            SELECT stage_status_json
            FROM scan_session_source
            WHERE scan_session_id = ? AND library_source_id = ?
            """,
            (scan_session_id, summary.source_id),
        ).fetchone()

        if existing is None:
            stage_status: dict[str, str] = {"discover": "pending", "metadata": "done"}
        else:
            stage_status = json.loads(str(existing[0]))
            stage_status["metadata"] = "done"
            stage_status.setdefault("discover", "done")

        conn.execute(
            """
            INSERT INTO scan_session_source(
              scan_session_id, library_source_id, stage_status_json, processed_assets, failed_assets, updated_at
            ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(scan_session_id, library_source_id)
            DO UPDATE SET
              stage_status_json = excluded.stage_status_json,
              processed_assets = excluded.processed_assets,
              failed_assets = excluded.failed_assets,
              updated_at = CURRENT_TIMESTAMP
            """,
            (
                scan_session_id,
                summary.source_id,
                json.dumps(stage_status, ensure_ascii=False, sort_keys=True),
                summary.processed_assets,
                summary.failed_assets,
            ),
        )

    def _assert_required_tables(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        existing = {str(row[0]) for row in rows}
        missing = sorted(REQUIRED_TABLES - existing)
        if missing:
            raise StageSchemaMissingError(stage="metadata", missing_tables=missing)


def _resolve_asset_capture_datetime(
    image_path: Path,
    *,
    fallback_mtime_ns: int,
    fallback_birthtime_ns: int | None,
) -> datetime | None:
    original, digitized, date_time = _read_exif_capture_datetime_fields(image_path)
    return parse_capture_datetime(
        date_time_original=original,
        date_time_digitized=digitized,
        date_time=date_time,
        fallback_mtime_ns=fallback_mtime_ns,
        fallback_birthtime_ns=fallback_birthtime_ns,
    )


def _read_exif_capture_datetime_fields(image_path: Path) -> tuple[str | None, str | None, str | None]:
    try:
        with Image.open(image_path) as image:
            exif = image.getexif()
    except OSError:
        return (None, None, None)

    if not exif:
        return (None, None, None)

    date_time_original = exif.get(36867)
    date_time_digitized = exif.get(36868)
    date_time = exif.get(306)
    return (_normalize_exif_value(date_time_original), _normalize_exif_value(date_time_digitized), _normalize_exif_value(date_time))


def _normalize_exif_value(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_datetime_text(raw: str | None) -> datetime | None:
    if raw is None:
        return None

    value = raw.strip()
    if not value:
        return None

    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _ensure_local_timezone(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=_local_tz())
    return value


def _local_tz():
    return datetime.now(UTC).astimezone().tzinfo


def _extract_birthtime_ns(stat: object) -> int | None:
    birth_ns = getattr(stat, "st_birthtime_ns", None)
    if birth_ns is not None:
        return int(birth_ns)
    birth = getattr(stat, "st_birthtime", None)
    if birth is not None:
        return int(float(birth) * 1_000_000_000)
    return None
