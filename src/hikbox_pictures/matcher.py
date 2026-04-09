from __future__ import annotations

from typing import Sequence

import face_recognition

from hikbox_pictures.image_io import load_rgb_image
from hikbox_pictures.models import CandidatePhoto, MatchBucket, PhotoEvaluation


class CandidateDecodeError(RuntimeError):
    pass


def _matching_face_indexes(
    encodings: list[Sequence[float]],
    target_encoding: Sequence[float],
    tolerance: float,
) -> set[int]:
    return {
        index
        for index, is_match in enumerate(
            face_recognition.compare_faces(encodings, target_encoding, tolerance=tolerance)
        )
        if is_match
    }


def _has_distinct_matches(matches_a: set[int], matches_b: set[int]) -> bool:
    return any(index_a != index_b for index_a in matches_a for index_b in matches_b)


def evaluate_candidate_photo(
    photo: CandidatePhoto,
    person_a_encoding: Sequence[float],
    person_b_encoding: Sequence[float],
    *,
    tolerance: float = 0.5,
) -> PhotoEvaluation:
    try:
        image = load_rgb_image(photo.path)
    except Exception as exc:  # pragma: no cover
        raise CandidateDecodeError(f"Failed to decode {photo.path}: {exc}") from exc

    locations = face_recognition.face_locations(image)
    if not locations:
        return PhotoEvaluation(candidate=photo, detected_face_count=0, bucket=None)

    encodings = face_recognition.face_encodings(image, known_face_locations=locations)
    matches_a = _matching_face_indexes(encodings, person_a_encoding, tolerance)
    matches_b = _matching_face_indexes(encodings, person_b_encoding, tolerance)

    if not matches_a or not matches_b or not _has_distinct_matches(matches_a, matches_b):
        return PhotoEvaluation(candidate=photo, detected_face_count=len(encodings), bucket=None)

    bucket = MatchBucket.ONLY_TWO if len(encodings) == 2 else MatchBucket.GROUP
    return PhotoEvaluation(candidate=photo, detected_face_count=len(encodings), bucket=bucket)
