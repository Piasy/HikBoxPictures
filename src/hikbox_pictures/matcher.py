from __future__ import annotations

from collections.abc import Sequence
from numbers import Real
from typing import Protocol, runtime_checkable

import numpy as np

from hikbox_pictures.deepface_engine import DeepFaceEngine, EmbeddingLike
from hikbox_pictures.models import CandidatePhoto, MatchBucket, PhotoEvaluation

DEFAULT_DISTANCE_THRESHOLD = 10.0


class CandidateDecodeError(RuntimeError):
    pass


@runtime_checkable
class MatcherEngineProtocol(Protocol):
    def detect_faces(self, image_path: object) -> list[object]:
        ...

    def min_distance(self, embedding: EmbeddingLike, references: Sequence[EmbeddingLike] | np.ndarray) -> float:
        ...

    def is_match(self, distance: float) -> bool:
        ...


_CACHED_MATCHER_ENGINE: DeepFaceEngine | None = None


def _get_cached_matcher_engine() -> DeepFaceEngine:
    global _CACHED_MATCHER_ENGINE
    if _CACHED_MATCHER_ENGINE is None:
        _CACHED_MATCHER_ENGINE = DeepFaceEngine.create()
    return _CACHED_MATCHER_ENGINE


def _normalize_reference_embeddings(reference_embeddings: object) -> Sequence[EmbeddingLike] | np.ndarray:
    if isinstance(reference_embeddings, np.ndarray):
        return reference_embeddings

    if not isinstance(reference_embeddings, Sequence):
        raise TypeError("reference_embeddings 类型非法")

    if len(reference_embeddings) == 0:
        return []

    first_item = reference_embeddings[0]
    if isinstance(first_item, Real):
        return [reference_embeddings]

    return list(reference_embeddings)


def compute_min_distances(
    candidate_embeddings: Sequence[EmbeddingLike],
    reference_embeddings: Sequence[EmbeddingLike] | np.ndarray,
    *,
    engine: MatcherEngineProtocol,
) -> list[float]:
    if len(reference_embeddings) == 0:
        return [float("inf")] * len(candidate_embeddings)

    return [engine.min_distance(candidate_embedding, reference_embeddings) for candidate_embedding in candidate_embeddings]


def _validate_matcher_engine(engine: object) -> MatcherEngineProtocol:
    if not isinstance(engine, MatcherEngineProtocol):
        missing_members = [
            member
            for member in ("detect_faces", "min_distance", "is_match")
            if not hasattr(engine, member)
        ]
        missing_detail = ", ".join(missing_members) if missing_members else "matcher engine protocol"
        raise TypeError(f"engine 接口不兼容，缺少: {missing_detail}")
    return engine


def _has_distinct_matches(matches_a: set[int], matches_b: set[int]) -> bool:
    return any(index_a != index_b for index_a in matches_a for index_b in matches_b)


def _face_area(face: object) -> int | None:
    bbox = getattr(face, "bbox", None)
    if bbox is None or len(bbox) != 4:
        return None

    top, right, bottom, left = bbox
    return max(0, bottom - top) * max(0, right - left)


def _select_largest_matching_pair(
    matches_a: set[int],
    matches_b: set[int],
    face_areas: Sequence[int | None],
) -> tuple[int, int] | None:
    candidate_pairs = [
        (index_a, index_b)
        for index_a in matches_a
        for index_b in matches_b
        if index_a != index_b and face_areas[index_a] is not None and face_areas[index_b] is not None
    ]
    if not candidate_pairs:
        return None

    return max(candidate_pairs, key=lambda pair: face_areas[pair[0]] + face_areas[pair[1]])


def _has_large_extra_face(
    face_areas: Sequence[int | None],
    *,
    primary_pair: tuple[int, int] | None,
) -> bool:
    if primary_pair is None:
        return True

    primary_indexes = set(primary_pair)
    primary_a_area = face_areas[primary_pair[0]]
    primary_b_area = face_areas[primary_pair[1]]
    if primary_a_area is None or primary_b_area is None:
        return True

    min_primary_area = min(primary_a_area, primary_b_area)
    extra_face_threshold = min_primary_area / 4

    for index, area in enumerate(face_areas):
        if index in primary_indexes:
            continue
        if area is None:
            return True
        if area >= extra_face_threshold:
            return True

    return False


def evaluate_candidate_photo(
    photo: CandidatePhoto,
    person_a_embeddings: Sequence[Sequence[float]] | Sequence[float] | np.ndarray,
    person_b_embeddings: Sequence[Sequence[float]] | Sequence[float] | np.ndarray,
    *,
    engine: MatcherEngineProtocol | None = None,
    distance_threshold: float = DEFAULT_DISTANCE_THRESHOLD,
    tolerance: float | None = None,
) -> PhotoEvaluation:
    if tolerance is not None:
        raise ValueError("tolerance 参数已弃用，请使用引擎默认阈值语义")
    if distance_threshold != DEFAULT_DISTANCE_THRESHOLD:
        raise ValueError("distance_threshold 不再生效，请使用引擎默认阈值语义")

    face_engine: MatcherEngineProtocol
    if engine is not None:
        face_engine = _validate_matcher_engine(engine)
    else:
        try:
            face_engine = _validate_matcher_engine(_get_cached_matcher_engine())
        except Exception as exc:  # pragma: no cover
            raise CandidateDecodeError(f"Failed to decode {photo.path}: {exc}") from exc

    try:
        faces = face_engine.detect_faces(photo.path)
    except Exception as exc:  # pragma: no cover
        raise CandidateDecodeError(f"Failed to decode {photo.path}: {exc}") from exc

    if not faces:
        return PhotoEvaluation(candidate=photo, detected_face_count=0, bucket=None)

    candidate_embeddings = [face.embedding for face in faces]
    normalized_person_a_embeddings = _normalize_reference_embeddings(person_a_embeddings)
    normalized_person_b_embeddings = _normalize_reference_embeddings(person_b_embeddings)

    try:
        min_distances_to_a = compute_min_distances(
            candidate_embeddings,
            normalized_person_a_embeddings,
            engine=face_engine,
        )
        min_distances_to_b = compute_min_distances(
            candidate_embeddings,
            normalized_person_b_embeddings,
            engine=face_engine,
        )

        matches_a = {index for index, distance in enumerate(min_distances_to_a) if face_engine.is_match(distance)}
        matches_b = {index for index, distance in enumerate(min_distances_to_b) if face_engine.is_match(distance)}
    except Exception as exc:  # pragma: no cover
        raise CandidateDecodeError(f"Failed to decode {photo.path}: {exc}") from exc

    if not matches_a or not matches_b or not _has_distinct_matches(matches_a, matches_b):
        return PhotoEvaluation(candidate=photo, detected_face_count=len(candidate_embeddings), bucket=None)

    if len(candidate_embeddings) == 2:
        bucket = MatchBucket.ONLY_TWO
    else:
        face_areas = [_face_area(face) for face in faces]
        primary_pair = _select_largest_matching_pair(matches_a, matches_b, face_areas)
        bucket = MatchBucket.GROUP if _has_large_extra_face(face_areas, primary_pair=primary_pair) else MatchBucket.ONLY_TWO

    return PhotoEvaluation(candidate=photo, detected_face_count=len(candidate_embeddings), bucket=bucket)
