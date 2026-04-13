from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PIL import Image

EXIF_DATETIME_FORMAT = "%Y:%m:%d %H:%M:%S"
EXIF_DATETIME_TAG_IDS = (36867, 36868, 306)


def _normalize_datetime(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.astimezone()


def read_exif_datetime(path: Path) -> datetime | None:
    with Image.open(path) as image:
        exif = image.getexif()

    for tag_id in EXIF_DATETIME_TAG_IDS:
        raw_value = exif.get(tag_id)
        if not raw_value:
            continue

        parsed = datetime.strptime(str(raw_value), EXIF_DATETIME_FORMAT)
        return _normalize_datetime(parsed)

    return None


def read_birthtime_datetime(path: Path) -> datetime | None:
    birthtime = getattr(path.stat(), "st_birthtime", None)
    if birthtime is None:
        return None
    return datetime.fromtimestamp(birthtime).astimezone()


def read_modification_datetime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone()


def resolve_capture_datetime(path: Path) -> datetime:
    for reader in (
        read_exif_datetime,
        read_birthtime_datetime,
        read_modification_datetime,
    ):
        try:
            value = reader(path)
        except (OSError, ValueError):
            continue
        if value is not None:
            return value
    raise RuntimeError(f"Unable to resolve capture time for {path}")


def format_year_month(moment: datetime) -> str:
    return moment.astimezone().strftime("%Y-%m")


def resolve_capture_fields(path: Path) -> tuple[str | None, str | None]:
    try:
        capture_datetime = resolve_capture_datetime(path)
    except RuntimeError:
        return None, None
    return capture_datetime.isoformat(), format_year_month(capture_datetime)
