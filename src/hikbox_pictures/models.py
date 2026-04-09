from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class MatchBucket(str, Enum):
    ONLY_TWO = "only-two"
    GROUP = "group"


@dataclass(frozen=True)
class CandidatePhoto:
    path: Path
    live_photo_video: Path | None = None


@dataclass(frozen=True)
class PhotoEvaluation:
    candidate: CandidatePhoto
    detected_face_count: int
    bucket: MatchBucket | None


@dataclass
class RunSummary:
    scanned_files: int = 0
    only_two_matches: int = 0
    group_matches: int = 0
    skipped_decode_errors: int = 0
    skipped_no_faces: int = 0
    missing_live_photo_videos: int = 0
    warnings: list[str] = field(default_factory=list)
