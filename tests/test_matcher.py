from types import SimpleNamespace

import numpy as np
import pytest

from hikbox_pictures.matcher import (
    DEFAULT_DISTANCE_THRESHOLD,
    CandidateDecodeError,
    compute_min_distances,
    evaluate_candidate_photo,
)
from hikbox_pictures.models import CandidatePhoto, MatchBucket


def test_compute_min_distances_returns_min_distance_per_candidate() -> None:
    candidate_embeddings = [[0.0, 0.0], [2.0, 2.0]]
    reference_embeddings = [[1.0, 0.0], [3.0, 3.0]]

    distances = compute_min_distances(candidate_embeddings, reference_embeddings)

    assert distances == pytest.approx([1.0, 2**0.5])


def test_evaluate_candidate_photo_classifies_only_two(monkeypatch, tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "pair.jpg")

    class FakeEngine:
        def detect_faces(self, image_path):
            assert image_path == photo.path
            return [
                SimpleNamespace(embedding=[0.1, 0.1]),
                SimpleNamespace(embedding=[0.9, 0.9]),
            ]

    monkeypatch.setattr("hikbox_pictures.matcher._get_cached_matcher_engine", lambda: FakeEngine())

    evaluation = evaluate_candidate_photo(
        photo,
        [[0.0, 0.0], [0.2, 0.2]],
        [[1.0, 1.0]],
    )

    assert evaluation.detected_face_count == 2
    assert evaluation.bucket is MatchBucket.ONLY_TWO


def test_evaluate_candidate_photo_classifies_group(monkeypatch, tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "group.jpg")

    class FakeEngine:
        def detect_faces(self, image_path):
            assert image_path == photo.path
            return [
                SimpleNamespace(embedding=[0.1, 0.1]),
                SimpleNamespace(embedding=[0.9, 0.9]),
                SimpleNamespace(embedding=[3.0, 3.0]),
            ]

    monkeypatch.setattr("hikbox_pictures.matcher._get_cached_matcher_engine", lambda: FakeEngine())

    evaluation = evaluate_candidate_photo(
        photo,
        [[0.0, 0.0]],
        [[1.0, 1.0]],
    )

    assert evaluation.detected_face_count == 3
    assert evaluation.bucket is MatchBucket.GROUP


def test_evaluate_candidate_photo_requires_both_people(monkeypatch, tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "solo.jpg")

    class FakeEngine:
        def detect_faces(self, image_path):
            assert image_path == photo.path
            return [
                SimpleNamespace(embedding=[0.1, 0.1]),
                SimpleNamespace(embedding=[0.2, 0.2]),
            ]

    monkeypatch.setattr("hikbox_pictures.matcher._get_cached_matcher_engine", lambda: FakeEngine())

    evaluation = evaluate_candidate_photo(
        photo,
        [[0.0, 0.0]],
        [[2.0, 2.0]],
    )

    assert evaluation.bucket is None
    assert evaluation.detected_face_count == 2


def test_evaluate_candidate_photo_requires_distinct_matching_faces(monkeypatch, tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "ambiguous.jpg")

    class FakeEngine:
        def detect_faces(self, image_path):
            assert image_path == photo.path
            return [SimpleNamespace(embedding=[0.1, 0.1])]

    monkeypatch.setattr("hikbox_pictures.matcher._get_cached_matcher_engine", lambda: FakeEngine())

    evaluation = evaluate_candidate_photo(
        photo,
        [[0.0, 0.0]],
        [[0.2, 0.2]],
    )

    assert evaluation.bucket is None
    assert evaluation.detected_face_count == 1


def test_evaluate_candidate_photo_uses_custom_distance_threshold(monkeypatch, tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "threshold.jpg")

    class FakeEngine:
        def detect_faces(self, image_path):
            assert image_path == photo.path
            return [
                SimpleNamespace(embedding=[0.08, 0.0]),
                SimpleNamespace(embedding=[1.08, 0.0]),
            ]

    monkeypatch.setattr("hikbox_pictures.matcher._get_cached_matcher_engine", lambda: FakeEngine())

    evaluation = evaluate_candidate_photo(
        photo,
        [[0.0, 0.0]],
        [[1.0, 0.0]],
        distance_threshold=DEFAULT_DISTANCE_THRESHOLD / 10,
    )

    assert evaluation.bucket is None
    assert evaluation.detected_face_count == 2


def test_evaluate_candidate_photo_wraps_inference_errors(monkeypatch, tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "broken.jpg")

    class FakeEngine:
        def detect_faces(self, image_path):
            assert image_path == photo.path
            raise RuntimeError("inference boom")

    monkeypatch.setattr("hikbox_pictures.matcher._get_cached_matcher_engine", lambda: FakeEngine())

    with pytest.raises(CandidateDecodeError, match="inference boom"):
        evaluate_candidate_photo(photo, [[0.1]], [[0.2]])


def test_evaluate_candidate_photo_accepts_numpy_reference_embeddings(monkeypatch, tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "numpy-reference.jpg")

    class FakeEngine:
        def detect_faces(self, image_path):
            assert image_path == photo.path
            return [
                SimpleNamespace(embedding=np.array([0.1, 0.1])),
                SimpleNamespace(embedding=np.array([0.9, 0.9])),
            ]

    monkeypatch.setattr("hikbox_pictures.matcher._get_cached_matcher_engine", lambda: FakeEngine())

    evaluation = evaluate_candidate_photo(
        photo,
        np.array([[0.0, 0.0], [0.2, 0.2]]),
        np.array([[1.0, 1.0]]),
    )

    assert evaluation.detected_face_count == 2
    assert evaluation.bucket is MatchBucket.ONLY_TWO


def test_evaluate_candidate_photo_accepts_legacy_tolerance_alias(monkeypatch, tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "legacy-tolerance.jpg")

    class FakeEngine:
        def detect_faces(self, image_path):
            assert image_path == photo.path
            return [
                SimpleNamespace(embedding=[0.08, 0.0]),
                SimpleNamespace(embedding=[1.08, 0.0]),
            ]

    monkeypatch.setattr("hikbox_pictures.matcher._get_cached_matcher_engine", lambda: FakeEngine())

    evaluation = evaluate_candidate_photo(
        photo,
        [[0.0, 0.0]],
        [[1.0, 0.0]],
        tolerance=DEFAULT_DISTANCE_THRESHOLD / 10,
    )

    assert evaluation.bucket is None
    assert evaluation.detected_face_count == 2
