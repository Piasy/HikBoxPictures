from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from hikbox_pictures.deepface_engine import DeepFaceEngine
from hikbox_pictures.models import CandidatePhoto, MatchBucket, PhotoEvaluation, ReferenceTemplate
from hikbox_pictures.reference_template import compute_template_match

DEFAULT_DISTANCE_THRESHOLD = 10.0


class CandidateDecodeError(RuntimeError):
    pass


@runtime_checkable
class MatcherEngineProtocol(Protocol):
    def detect_faces(self, image_path: object) -> list[object]:
        ...

    def distance(self, lhs: object, rhs: object) -> float:
        ...


_CACHED_MATCHER_ENGINE: DeepFaceEngine | None = None


def _get_cached_matcher_engine() -> DeepFaceEngine:
    global _CACHED_MATCHER_ENGINE
    if _CACHED_MATCHER_ENGINE is None:
        _CACHED_MATCHER_ENGINE = DeepFaceEngine.create()
    return _CACHED_MATCHER_ENGINE


def _validate_matcher_engine(engine: object) -> MatcherEngineProtocol:
    required_members = ("detect_faces", "distance")
    missing_members = [member for member in required_members if not hasattr(engine, member)]
    if missing_members:
        missing_detail = ", ".join(missing_members)
        raise TypeError(f"engine 接口不兼容，缺少: {missing_detail}")

    non_callable_members = [member for member in required_members if not callable(getattr(engine, member))]
    if non_callable_members:
        non_callable_detail = ", ".join(non_callable_members)
        raise TypeError(f"engine 接口不兼容，不可调用: {non_callable_detail}")

    if not isinstance(engine, MatcherEngineProtocol):
        raise TypeError("engine 接口不兼容，缺少: matcher engine protocol")
    return engine


def _has_distinct_matches(matches_a: set[int], matches_b: set[int]) -> bool:
    return any(index_a != index_b for index_a in matches_a for index_b in matches_b)


def _select_best_match_pair(
    matches_a: set[int],
    matches_b: set[int],
    distances_to_a: Sequence[float],
    distances_to_b: Sequence[float],
) -> tuple[int, int] | None:
    candidate_pairs = [
        (index_a, index_b)
        for index_a in matches_a
        for index_b in matches_b
        if index_a != index_b
    ]
    if not candidate_pairs:
        return None

    return min(
        candidate_pairs,
        key=lambda pair: (
            max(distances_to_a[pair[0]], distances_to_b[pair[1]]),
            pair[0],
            pair[1],
        ),
    )


def _joint_distance_for_pair(
    pair: tuple[int, int] | None,
    distances_to_a: Sequence[float],
    distances_to_b: Sequence[float],
) -> float | None:
    if pair is None:
        return None
    index_a, index_b = pair
    return max(distances_to_a[index_a], distances_to_b[index_b])


def _face_area(face: object) -> int | None:
    bbox = getattr(face, "bbox", None)
    if bbox is None:
        return None

    try:
        top, right, bottom, left = bbox
    except (TypeError, ValueError):
        return None

    try:
        return max(0, bottom - top) * max(0, right - left)
    except TypeError:
        return None


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

    return max(
        candidate_pairs,
        key=lambda pair: (
            face_areas[pair[0]] + face_areas[pair[1]],
            min(face_areas[pair[0]], face_areas[pair[1]]),
            -pair[0],
            -pair[1],
        ),
    )


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
    person_a_template: ReferenceTemplate,
    person_b_template: ReferenceTemplate,
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
        detected_faces = face_engine.detect_faces(photo.path)
    except Exception as exc:  # pragma: no cover
        raise CandidateDecodeError(f"Failed to decode {photo.path}: {exc}") from exc

    if detected_faces is None:
        faces = []
    elif isinstance(detected_faces, (str, bytes)):
        raise CandidateDecodeError(f"Failed to decode {photo.path}: detect_faces 返回了非法字符串类型")
    else:
        try:
            faces = list(detected_faces)
        except TypeError as exc:
            raise CandidateDecodeError(f"Failed to decode {photo.path}: detect_faces 返回值不可迭代") from exc

    if not faces:
        return PhotoEvaluation(candidate=photo, detected_face_count=0, bucket=None)

    try:
        candidate_embeddings = [face.embedding for face in faces]
    except Exception as exc:
        raise CandidateDecodeError(f"Failed to decode {photo.path}: {exc}") from exc

    try:
        match_results_to_a = [
            compute_template_match(candidate_embedding, person_a_template, engine=face_engine)
            for candidate_embedding in candidate_embeddings
        ]
        match_results_to_b = [
            compute_template_match(candidate_embedding, person_b_template, engine=face_engine)
            for candidate_embedding in candidate_embeddings
        ]
    except Exception as exc:  # pragma: no cover
        raise CandidateDecodeError(f"Failed to decode {photo.path}: {exc}") from exc

    template_distances_to_a = [result.template_distance for result in match_results_to_a]
    template_distances_to_b = [result.template_distance for result in match_results_to_b]
    matches_a = {index for index, result in enumerate(match_results_to_a) if result.matched}
    matches_b = {index for index, result in enumerate(match_results_to_b) if result.matched}

    best_match_pair = _select_best_match_pair(
        matches_a,
        matches_b,
        template_distances_to_a,
        template_distances_to_b,
    )
    joint_distance = _joint_distance_for_pair(best_match_pair, template_distances_to_a, template_distances_to_b)

    if not matches_a or not matches_b or not _has_distinct_matches(matches_a, matches_b):
        return PhotoEvaluation(candidate=photo, detected_face_count=len(candidate_embeddings), bucket=None)

    if len(candidate_embeddings) == 2:
        bucket = MatchBucket.ONLY_TWO
    else:
        face_areas = [_face_area(face) for face in faces]
        primary_pair = _select_largest_matching_pair(matches_a, matches_b, face_areas)
        bucket = MatchBucket.GROUP if _has_large_extra_face(face_areas, primary_pair=primary_pair) else MatchBucket.ONLY_TWO

    return PhotoEvaluation(
        candidate=photo,
        detected_face_count=len(candidate_embeddings),
        bucket=bucket,
        joint_distance=joint_distance,
        best_match_pair=best_match_pair,
    )
