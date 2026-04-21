from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from hikbox_pictures.product.db.connection import connect_sqlite

from .live_photo import match_live_photo_mov
from .models import MetadataSourceSummary


@dataclass(frozen=True)
class MetadataFileRecord:
    primary_path: str
    mtime_ns: int
    capture_datetime: str | None


class MetadataStage:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def run(self, *, source_id: int, source_root: Path) -> MetadataSourceSummary:
        processed_assets = 0

        with connect_sqlite(self._db_path) as conn:
            rows = conn.execute(
                """
                SELECT primary_path, mtime_ns, capture_datetime
                FROM photo_asset
                WHERE library_source_id=? AND asset_status='active'
                ORDER BY id
                """,
                (source_id,),
            ).fetchall()

            for row in rows:
                record = MetadataFileRecord(
                    primary_path=str(row[0]),
                    mtime_ns=int(row[1]),
                    capture_datetime=str(row[2]) if row[2] is not None else None,
                )
                still_path = source_root / record.primary_path
                if not still_path.exists():
                    continue

                parsed_dt = _resolve_capture_datetime(record.capture_datetime, still_path)
                capture_datetime = parsed_dt.isoformat()
                capture_month = parsed_dt.strftime("%Y-%m")

                mov = match_live_photo_mov(still_path)
                if mov is None:
                    is_live_photo = 0
                    live_mov_path = None
                    live_mov_size = None
                    live_mov_mtime_ns = None
                else:
                    is_live_photo = 1
                    live_mov_path = mov.relative_to(source_root).as_posix()
                    live_mov_size = mov.stat().st_size
                    live_mov_mtime_ns = mov.stat().st_mtime_ns

                conn.execute(
                    """
                    UPDATE photo_asset
                    SET capture_datetime=?,
                        capture_month=?,
                        is_live_photo=?,
                        live_mov_path=?,
                        live_mov_size=?,
                        live_mov_mtime_ns=?,
                        updated_at=?
                    WHERE library_source_id=? AND primary_path=?
                    """,
                    (
                        capture_datetime,
                        capture_month,
                        is_live_photo,
                        live_mov_path,
                        live_mov_size,
                        live_mov_mtime_ns,
                        _utc_now(),
                        source_id,
                        record.primary_path,
                    ),
                )
                processed_assets += 1

            conn.commit()

        return MetadataSourceSummary(processed_assets=processed_assets, failed_assets=0)


def _resolve_capture_datetime(existing_capture_datetime: str | None, still_path: Path) -> datetime:
    if existing_capture_datetime:
        try:
            parsed = datetime.fromisoformat(existing_capture_datetime)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
        except ValueError:
            pass

    stat_result = still_path.stat()
    return datetime.fromtimestamp(stat_result.st_mtime_ns / 1_000_000_000, tz=UTC)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
