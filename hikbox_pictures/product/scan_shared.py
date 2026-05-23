from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
import subprocess
from typing import Final

import numpy as np
from PIL import Image
from PIL import ImageOps


SUPPORTED_SCAN_SUFFIXES: Final[set[str]] = {".jpg", ".jpeg", ".png", ".heic", ".heif"}
LIVE_PHOTO_STILL_SUFFIXES: Final[set[str]] = {".jpg", ".jpeg", ".heic", ".heif"}
_IMAGE_CAPTURE_DATE_TAGS: Final[tuple[int, ...]] = (36867, 36868, 306)
_EXIFTOOL_IMAGE_DATE_ARGS: Final[tuple[str, ...]] = (
    "-DateTimeOriginal",
    "-CreateDate",
    "-CreationDate",
)
_EXIFTOOL_MOV_DATE_ARGS: Final[tuple[str, ...]] = (
    "-CreationDate",
    "-CreateDate",
    "-MediaCreateDate",
)


def utc_now_text() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def register_heif_opener() -> None:
    import pillow_heif

    pillow_heif.register_heif_opener()


def load_rgb_image_with_exif(image_path: Path) -> Image.Image:
    register_heif_opener()
    with Image.open(image_path) as image:
        normalized = ImageOps.exif_transpose(image)
        return normalized.convert("RGB")


def normalize_vector(values: np.ndarray) -> np.ndarray:
    safe = np.asarray(values, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(safe))
    if norm <= 1e-9:
        return safe
    return safe / norm


def compute_capture_month(image_path: Path) -> str:
    register_heif_opener()
    try:
        with Image.open(image_path) as image:
            exif = image.getexif()
    except Exception:  # noqa: BLE001
        exif = None
    for key in _IMAGE_CAPTURE_DATE_TAGS:
        value = exif.get(key) if exif is not None else None
        month = _parse_exif_month(value)
        if month is not None:
            return month
    modified_at = datetime.fromtimestamp(_safe_stat(image_path).st_mtime, tz=UTC)
    return modified_at.strftime("%Y-%m")


def capture_day_for_live_photo(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in LIVE_PHOTO_STILL_SUFFIXES:
        return _capture_day_from_image(path) or _capture_day_from_exiftool(path, _EXIFTOOL_IMAGE_DATE_ARGS)
    if suffix == ".mov":
        return _capture_day_from_exiftool(path, _EXIFTOOL_MOV_DATE_ARGS)
    return None


def live_photo_mov_xattr_name(image_path: Path) -> str | None:
    raw_value = _read_xattr(image_path, "livephoto")
    if raw_value is None:
        return None
    try:
        return raw_value.rstrip(b"\0").decode("utf-8")
    except UnicodeDecodeError:
        return None


def live_photo_dates_allow_match(
    image_path: Path,
    mov_path: Path,
    *,
    image_day: str | None,
    mov_day: str | None,
) -> bool:
    if image_day is not None and mov_day is not None:
        return image_day == mov_day
    return live_photo_mov_xattr_name(image_path) == mov_path.name


def find_live_photo_mov(image_path: Path) -> str | None:
    if image_path.suffix.lower() not in LIVE_PHOTO_STILL_SUFFIXES:
        return None
    prefix = f".{image_path.stem}"
    candidates = sorted(
        child
        for child in image_path.parent.iterdir()
        if child.is_file()
        and child.name.startswith(prefix)
        and child.suffix.lower() == ".mov"
    )
    if not candidates:
        return None
    image_day = capture_day_for_live_photo(image_path)
    for candidate in candidates:
        mov_day = capture_day_for_live_photo(candidate)
        if live_photo_dates_allow_match(
            image_path,
            candidate,
            image_day=image_day,
            mov_day=mov_day,
        ):
            return str(candidate.resolve())
    return None


def _capture_day_from_image(image_path: Path) -> str | None:
    register_heif_opener()
    try:
        with Image.open(image_path) as image:
            exif = image.getexif()
    except Exception:  # noqa: BLE001
        return None
    for key in _IMAGE_CAPTURE_DATE_TAGS:
        day = _parse_capture_day(exif.get(key))
        if day is not None:
            return day
    return None


def _capture_day_from_exiftool(path: Path, date_args: tuple[str, ...]) -> str | None:
    try:
        result = subprocess.run(
            ["exiftool", "-s3", *date_args, str(path)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        day = _parse_capture_day(line)
        if day is not None:
            return day
    return None


def _read_xattr(path: Path, name: str) -> bytes | None:
    getxattr = getattr(os, "getxattr", None)
    if getxattr is not None:
        try:
            return getxattr(path, name)
        except OSError:
            return None
    try:
        result = subprocess.run(
            ["xattr", "-px", name, str(path)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return bytes.fromhex(result.stdout)
    except ValueError:
        return None


def resize_to_max_edge(image: Image.Image, *, max_edge: int) -> tuple[Image.Image, float]:
    width, height = image.size
    longest_edge = max(width, height)
    if longest_edge <= 0:
        return image.copy(), 1.0
    scale = min(1.0, float(max_edge) / float(longest_edge))
    if abs(scale - 1.0) <= 1e-9:
        return image.copy(), 1.0
    resized = image.resize(
        (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
        Image.Resampling.LANCZOS,
    )
    return resized, scale


def clamp_bbox(*, x1: float, y1: float, x2: float, y2: float, width: int, height: int) -> tuple[int, int, int, int]:
    left = max(0, min(int(round(x1)), max(width - 1, 0)))
    top = max(0, min(int(round(y1)), max(height - 1, 0)))
    right = max(left + 1, min(int(round(x2)), width))
    bottom = max(top + 1, min(int(round(y2)), height))
    return left, top, right, bottom


def _parse_exif_month(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if len(value) < 7:
        return None
    year = value[0:4]
    month = value[5:7]
    if not year.isdigit() or not month.isdigit():
        return None
    return f"{year}-{month}"


def _parse_capture_day(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) < 10:
        return None
    year = value[0:4]
    month = value[5:7]
    day = value[8:10]
    if not year.isdigit() or not month.isdigit() or not day.isdigit():
        return None
    try:
        parsed = datetime(int(year), int(month), int(day))
    except ValueError:
        return None
    return parsed.strftime("%Y-%m-%d")


def _safe_stat(path: Path):
    try:
        return path.stat()
    except OSError:
        return path.lstat()
