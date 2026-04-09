from __future__ import annotations

import math
from collections.abc import Sequence
from numbers import Real

from hikbox_pictures.insightface_engine import InsightFaceEngine
from hikbox_pictures.models import CandidatePhoto, MatchBucket, PhotoEvaluation

DEFAULT_DISTANCE_THRESHOLD = 0.5


class CandidateDecodeError(RuntimeError):
    pass


_CACHED_MATCHER_ENGINE: InsightFaceEngine | None = None


def _get_cached_matcher_engine() -> InsightFaceEngine:
    global _CACHED_MATCHER_ENGINE
    if _CACHED_MATCHER_ENGINE is None:
        _CACHED_MATCHER_ENGINE = InsightFaceEngine.create()
    return _CACHED_MATCHER_ENGINE


def _normalize_reference_embeddings(
    reference_embeddings: Sequence[Sequence[float]] | Sequence[float],
) -> list[Sequence[float]]:
    if not reference_embeddings:
        return []
    first_item = reference_embeddings[0]
    if isinstance(first_item, Real):
        return [reference_embeddings]
    return list(reference_embeddings)


def _euclidean_distance(lhs: Sequence[float], rhs: Sequence[float]) -> float:
    return math.dist(tuple(lhs), tuple(rhs))


def compute_min_distances(
    candidate_embeddings: Sequence[Sequence[float]],
    reference_embeddings: Sequence[Sequence[float]],
) -> list[float]:
    if not reference_embeddings:
        return [float("inf")] * len(candidate_embeddings)

    return [
        min(_euclidean_distance(candidate_embedding, reference_embedding) for reference_embedding in reference_embeddings)
        for candidate_embedding in candidate_embeddings
    ]


def _has_distinct_matches(matches_a: set[int], matches_b: set[int]) -> bool:
    return any(index_a != index_b for index_a in matches_a for index_b in matches_b)


def evaluate_candidate_photo(
    photo: CandidatePhoto,
    person_a_embeddings: Sequence[Sequence[float]] | Sequence[float],
    person_b_embeddings: Sequence[Sequence[float]] | Sequence[float],
    *,
    distance_threshold: float = DEFAULT_DISTANCE_THRESHOLD,
) -> PhotoEvaluation:
    try:
        faces = _get_cached_matcher_engine().detect_faces(photo.path)
    except Exception as exc:  # pragma: no cover
        raise CandidateDecodeError(f"Failed to decode {photo.path}: {exc}") from exc

    if not faces:
        return PhotoEvaluation(candidate=photo, detected_face_count=0, bucket=None)

    candidate_embeddings = [face.embedding for face in faces]
    normalized_person_a_embeddings = _normalize_reference_embeddings(person_a_embeddings)
    normalized_person_b_embeddings = _normalize_reference_embeddings(person_b_embeddings)

    min_distances_to_a = compute_min_distances(candidate_embeddings, normalized_person_a_embeddings)
    min_distances_to_b = compute_min_distances(candidate_embeddings, normalized_person_b_embeddings)

    matches_a = {index for index, distance in enumerate(min_distances_to_a) if distance <= distance_threshold}
    matches_b = {index for index, distance in enumerate(min_distances_to_b) if distance <= distance_threshold}

    if not matches_a or not matches_b or not _has_distinct_matches(matches_a, matches_b):
        return PhotoEvaluation(candidate=photo, detected_face_count=len(candidate_embeddings), bucket=None)

    bucket = MatchBucket.ONLY_TWO if len(candidate_embeddings) == 2 else MatchBucket.GROUP
    return PhotoEvaluation(candidate=photo, detected_face_count=len(candidate_embeddings), bucket=bucket)
