from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from hikbox_pictures.models import CandidatePhoto

SUPPORTED_EXTENSIONS = {".heic", ".jpg", ".jpeg", ".png"}


def find_live_photo_video(image_path: Path) -> Path | None:
    if image_path.suffix.lower() != ".heic":
        return None

    entries = sorted(image_path.parent.iterdir(), key=lambda candidate: candidate.name)
    return _build_live_photo_video_index(entries).get(image_path.stem)


def is_supported_photo_path(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_EXTENSIONS


def iter_candidate_photos(input_root: Path) -> Iterator[CandidatePhoto]:
    if not input_root.exists() or not input_root.is_dir():
        return
    yield from _iter_candidate_photos_in_directory(input_root)


def _iter_candidate_photos_in_directory(directory: Path) -> Iterator[CandidatePhoto]:
    entries = sorted(directory.iterdir(), key=lambda candidate: candidate.name)
    live_photo_videos = _build_live_photo_video_index(entries)

    for entry in entries:
        if entry.is_dir():
            yield from _iter_candidate_photos_in_directory(entry)
            continue
        if not entry.is_file():
            continue
        if not is_supported_photo_path(entry):
            continue
        live_photo_video = live_photo_videos.get(entry.stem) if entry.suffix.lower() == ".heic" else None
        yield CandidatePhoto(path=entry, live_photo_video=live_photo_video)


def _build_live_photo_video_index(entries: list[Path]) -> dict[str, Path]:
    index: dict[str, Path] = {}
    for candidate in entries:
        if not candidate.is_file():
            continue
        live_photo_stem = _extract_live_photo_stem(candidate.name)
        if live_photo_stem is None:
            continue
        index.setdefault(live_photo_stem, candidate)
    return index


def _extract_live_photo_stem(file_name: str) -> str | None:
    path = Path(file_name)
    if path.suffix.lower() != ".mov" or not file_name.startswith("."):
        return None
    body = file_name[: -len(path.suffix)][1:]
    stem, separator, _suffix = body.rpartition("_")
    if not separator or not stem:
        return None
    return stem
