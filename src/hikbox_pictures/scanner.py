from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from hikbox_pictures.models import CandidatePhoto

SUPPORTED_EXTENSIONS = {".heic", ".jpg", ".jpeg", ".png"}


def find_live_photo_video(image_path: Path) -> Path | None:
    if image_path.suffix.lower() != ".heic":
        return None

    matches = sorted(
        candidate
        for candidate in image_path.parent.iterdir()
        if candidate.is_file()
        and candidate.suffix.lower() == ".mov"
        and candidate.name.startswith(f".{image_path.stem}_")
    )
    return matches[0] if matches else None


def iter_candidate_photos(input_root: Path) -> Iterator[CandidatePhoto]:
    for path in sorted(input_root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        yield CandidatePhoto(path=path, live_photo_video=find_live_photo_video(path))
