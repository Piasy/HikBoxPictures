from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import numpy as np
import numpy.typing as npt


Embedding = npt.NDArray[np.float32]
BBoxTLBR = tuple[int, int, int, int]
ImageSize = tuple[int, int]


class MatchBucket(str, Enum):
    ONLY_TWO = "only-two"
    GROUP = "group"


@dataclass(frozen=True)
class CandidatePhoto:
    path: Path
    live_photo_video: Path | None = None


@dataclass(frozen=True)
class ReferenceSample:
    path: Path
    embedding: Embedding
    bbox: BBoxTLBR
    image_size: ImageSize
    face_area_ratio: float
    sharpness_score: float
    quality_score: float
    center_distance: float | None
    kept: bool
    drop_reason: str | None


@dataclass(frozen=True)
class ReferenceTemplate:
    name: str
    samples: list[ReferenceSample]
    kept_samples: list[ReferenceSample]
    centroid_embedding: Embedding
    match_threshold: float
    top_k: int

    @property
    def dropped_samples(self) -> list[ReferenceSample]:
        return [sample for sample in self.samples if not sample.kept]


@dataclass(frozen=True)
class TemplateMatchResult:
    template_distance: float
    centroid_distance: float
    matched: bool
    top_k_distances: list[float]


@dataclass(frozen=True)
class PhotoEvaluation:
    candidate: CandidatePhoto
    detected_face_count: int
    bucket: MatchBucket | None
    joint_distance: float | None = None
    best_match_pair: tuple[int, int] | None = None


@dataclass
class RunSummary:
    scanned_files: int = 0
    only_two_matches: int = 0
    group_matches: int = 0
    skipped_decode_errors: int = 0
    skipped_no_faces: int = 0
    missing_live_photo_videos: int = 0
    warnings: list[str] = field(default_factory=list)
