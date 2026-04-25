from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from pathlib import Path
from typing import Final

import numpy as np
from PIL import Image
from PIL import ImageOps


SUPPORTED_SCAN_SUFFIXES: Final[set[str]] = {".jpg", ".jpeg", ".png", ".heic", ".heif"}
HEIF_SUFFIXES: Final[set[str]] = {".heic", ".heif"}


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
    for key in (36867, 36868, 306):
        value = exif.get(key) if exif is not None else None
        month = _parse_exif_month(value)
        if month is not None:
            return month
    modified_at = datetime.fromtimestamp(_safe_stat(image_path).st_mtime, tz=UTC)
    return modified_at.strftime("%Y-%m")


def compute_file_sha256(path: Path) -> str:
    with path.open("rb") as handle:
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        return digest.hexdigest()
    

def compute_file_fingerprint(path: Path) -> str:
    try:
        return compute_file_sha256(path)
    except OSError:
        stat_result = _safe_stat(path)
        payload = "|".join(
            [
                str(path.resolve()),
                str(stat_result.st_size),
                str(stat_result.st_mtime_ns),
                str(getattr(stat_result, "st_ino", 0)),
            ]
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def find_live_photo_mov(image_path: Path) -> str | None:
    if image_path.suffix.lower() not in HEIF_SUFFIXES:
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
    return str(candidates[0].resolve())


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


def _safe_stat(path: Path):
    try:
        return path.stat()
    except OSError:
        return path.lstat()
