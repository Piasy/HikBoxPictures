from types import SimpleNamespace

import numpy as np
import pytest

from hikbox_pictures.matcher import (
    CandidateDecodeError,
    DEFAULT_DISTANCE_THRESHOLD,
    compute_min_distances,
    evaluate_candidate_photo,
)
from hikbox_pictures.models import CandidatePhoto, MatchBucket


class _DistanceMappingEngine:
    def __init__(self, faces, distance_by_face_and_group, *, threshold=0.5):
        self._faces = faces
        self._distance_by_face_and_group = distance_by_face_and_group
        self._threshold = threshold
        self.min_distance_calls = []
        self.is_match_calls = []

    def detect_faces(self, image_path):
        return self._faces

    def min_distance(self, embedding, references):
        face_id = int(np.asarray(embedding, dtype=float).reshape(-1)[0])
        group = "A" if _first_reference_scalar(references) < 20 else "B"
        self.min_distance_calls.append((face_id, group, references))
        return self._distance_by_face_and_group[(face_id, group)]

    def is_match(self, distance):
        self.is_match_calls.append(distance)
        return distance <= self._threshold


def _first_reference_scalar(references) -> float:
    ref_array = np.asarray(references, dtype=float)
    if ref_array.size == 0:
        return float("nan")
    if ref_array.ndim == 1:
        return float(ref_array[0])
    return float(ref_array[0][0])


def _make_face(face_id: int, bbox=None):
    payload = {"embedding": np.array([float(face_id)], dtype=np.float32)}
    if bbox is not None:
        payload["bbox"] = bbox
    return SimpleNamespace(**payload)


def test_compute_min_distances_uses_engine_min_distance() -> None:
    candidate_embeddings = [[1.0], [2.0]]
    reference_embeddings = [[10.0], [11.0]]

    class FakeEngine:
        def __init__(self):
            self.calls = []

        def min_distance(self, embedding, references):
            self.calls.append((tuple(embedding), references))
            return float(np.asarray(embedding, dtype=float).sum())

    engine = FakeEngine()

    distances = compute_min_distances(
        candidate_embeddings,
        reference_embeddings,
        engine=engine,
    )

    assert distances == pytest.approx([1.0, 2.0])
    assert [call[0] for call in engine.calls] == [(1.0,), (2.0,)]
    assert all(call[1] == reference_embeddings for call in engine.calls)


def test_evaluate_candidate_photo_uses_engine_distance_and_threshold(monkeypatch, tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "engine-distance-threshold.jpg")

    engine = _DistanceMappingEngine(
        faces=[_make_face(100), _make_face(200)],
        distance_by_face_and_group={
            (100, "A"): 0.1,
            (100, "B"): 0.9,
            (200, "A"): 0.9,
            (200, "B"): 0.1,
        },
        threshold=0.2,
    )

    monkeypatch.setattr(
        "hikbox_pictures.matcher._get_cached_matcher_engine",
        lambda: pytest.fail("传入显式 engine 时不应回退到缓存引擎"),
    )

    evaluation = evaluate_candidate_photo(
        photo,
        [[10.0]],
        [[30.0]],
        engine=engine,
    )

    assert evaluation.detected_face_count == 2
    assert evaluation.bucket is MatchBucket.ONLY_TWO
    assert sorted(engine.is_match_calls) == pytest.approx([0.1, 0.1, 0.9, 0.9])


def test_evaluate_candidate_photo_rejects_non_default_distance_threshold(tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "non-default-threshold.jpg")
    engine = _DistanceMappingEngine(
        faces=[_make_face(1), _make_face(2)],
        distance_by_face_and_group={
            (1, "A"): 0.1,
            (1, "B"): 0.9,
            (2, "A"): 0.9,
            (2, "B"): 0.1,
        },
    )

    with pytest.raises(ValueError, match="distance_threshold"):
        evaluate_candidate_photo(
            photo,
            [[10.0]],
            [[30.0]],
            engine=engine,
            distance_threshold=DEFAULT_DISTANCE_THRESHOLD + 0.1,
        )


def test_evaluate_candidate_photo_rejects_legacy_tolerance(tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "legacy-tolerance.jpg")
    engine = _DistanceMappingEngine(
        faces=[_make_face(1), _make_face(2)],
        distance_by_face_and_group={
            (1, "A"): 0.1,
            (1, "B"): 0.9,
            (2, "A"): 0.9,
            (2, "B"): 0.1,
        },
    )

    with pytest.raises(ValueError, match="tolerance"):
        evaluate_candidate_photo(
            photo,
            [[10.0]],
            [[30.0]],
            engine=engine,
            tolerance=0.3,
        )


def test_evaluate_candidate_photo_classifies_group(tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "group.jpg")

    engine = _DistanceMappingEngine(
        faces=[
            _make_face(1, bbox=(0, 10, 10, 0)),
            _make_face(2, bbox=(0, 10, 10, 0)),
            _make_face(3, bbox=(0, 6, 5, 0)),
        ],
        distance_by_face_and_group={
            (1, "A"): 0.1,
            (1, "B"): 0.9,
            (2, "A"): 0.9,
            (2, "B"): 0.1,
            (3, "A"): 0.9,
            (3, "B"): 0.9,
        },
    )

    evaluation = evaluate_candidate_photo(photo, [[10.0]], [[30.0]], engine=engine)

    assert evaluation.detected_face_count == 3
    assert evaluation.bucket is MatchBucket.GROUP


def test_evaluate_candidate_photo_ignores_small_extra_face(tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "small-extra.jpg")

    engine = _DistanceMappingEngine(
        faces=[
            _make_face(1, bbox=(0, 10, 10, 0)),
            _make_face(2, bbox=(0, 8, 10, 0)),
            _make_face(3, bbox=(0, 5, 2, 0)),
        ],
        distance_by_face_and_group={
            (1, "A"): 0.1,
            (1, "B"): 0.9,
            (2, "A"): 0.9,
            (2, "B"): 0.1,
            (3, "A"): 0.9,
            (3, "B"): 0.9,
        },
    )

    evaluation = evaluate_candidate_photo(photo, [[10.0]], [[30.0]], engine=engine)

    assert evaluation.detected_face_count == 3
    assert evaluation.bucket is MatchBucket.ONLY_TWO


def test_evaluate_candidate_photo_uses_largest_matching_pair_for_extra_face_baseline(tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "largest-pair.jpg")

    engine = _DistanceMappingEngine(
        faces=[
            _make_face(1, bbox=(0, 10, 10, 0)),
            _make_face(2, bbox=(0, 10, 10, 0)),
            _make_face(3, bbox=(0, 4, 4, 0)),
            _make_face(4, bbox=(0, 5, 4, 0)),
        ],
        distance_by_face_and_group={
            (1, "A"): 0.1,
            (1, "B"): 0.9,
            (2, "A"): 0.9,
            (2, "B"): 0.1,
            (3, "A"): 0.2,
            (3, "B"): 0.9,
            (4, "A"): 0.9,
            (4, "B"): 0.9,
        },
    )

    evaluation = evaluate_candidate_photo(photo, [[10.0], [11.0]], [[30.0]], engine=engine)

    assert evaluation.detected_face_count == 4
    assert evaluation.bucket is MatchBucket.ONLY_TWO


def test_evaluate_candidate_photo_requires_both_people(tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "solo.jpg")

    engine = _DistanceMappingEngine(
        faces=[_make_face(1), _make_face(2)],
        distance_by_face_and_group={
            (1, "A"): 0.1,
            (1, "B"): 0.9,
            (2, "A"): 0.2,
            (2, "B"): 0.8,
        },
    )

    evaluation = evaluate_candidate_photo(photo, [[10.0]], [[30.0]], engine=engine)

    assert evaluation.bucket is None
    assert evaluation.detected_face_count == 2


def test_evaluate_candidate_photo_requires_distinct_matching_faces(tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "ambiguous.jpg")

    engine = _DistanceMappingEngine(
        faces=[_make_face(1)],
        distance_by_face_and_group={
            (1, "A"): 0.1,
            (1, "B"): 0.1,
        },
    )

    evaluation = evaluate_candidate_photo(photo, [[10.0]], [[30.0]], engine=engine)

    assert evaluation.bucket is None
    assert evaluation.detected_face_count == 1


def test_evaluate_candidate_photo_wraps_detection_errors(tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "broken.jpg")

    class FakeEngine:
        def detect_faces(self, image_path):
            assert image_path == photo.path
            raise RuntimeError("detect boom")

        def min_distance(self, embedding, references):
            return 0.1

        def is_match(self, distance):
            return True

    with pytest.raises(CandidateDecodeError, match="detect boom"):
        evaluate_candidate_photo(photo, [[10.0]], [[30.0]], engine=FakeEngine())


def test_evaluate_candidate_photo_wraps_distance_errors(tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "distance-broken.jpg")

    class FakeEngine:
        def detect_faces(self, image_path):
            assert image_path == photo.path
            return [_make_face(1)]

        def min_distance(self, embedding, references):
            raise RuntimeError("distance boom")

        def is_match(self, distance):
            return distance < 0.5

    with pytest.raises(CandidateDecodeError, match="distance boom"):
        evaluate_candidate_photo(photo, [[10.0]], [[30.0]], engine=FakeEngine())


def test_evaluate_candidate_photo_fast_fails_on_incompatible_engine(tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "incompatible-engine.jpg")

    class IncompatibleEngine:
        def detect_faces(self, image_path):
            assert image_path == photo.path
            return [_make_face(1)]

    with pytest.raises(TypeError, match="min_distance"):
        evaluate_candidate_photo(photo, [[10.0]], [[30.0]], engine=IncompatibleEngine())


def test_evaluate_candidate_photo_reports_invalid_reference_embeddings(tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "invalid-references.jpg")

    engine = _DistanceMappingEngine(
        faces=[_make_face(1), _make_face(2)],
        distance_by_face_and_group={
            (1, "A"): 0.1,
            (1, "B"): 0.9,
            (2, "A"): 0.9,
            (2, "B"): 0.1,
        },
    )

    with pytest.raises(TypeError, match="reference_embeddings"):
        evaluate_candidate_photo(photo, 123, [[30.0]], engine=engine)


def test_evaluate_candidate_photo_wraps_cached_engine_init_errors(monkeypatch, tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "init-fail.jpg")

    def fake_cached_engine():
        raise RuntimeError("engine init boom")

    monkeypatch.setattr("hikbox_pictures.matcher._get_cached_matcher_engine", fake_cached_engine)

    with pytest.raises(CandidateDecodeError, match="engine init boom"):
        evaluate_candidate_photo(photo, [[10.0]], [[30.0]])


def test_evaluate_candidate_photo_accepts_numpy_reference_embeddings(tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "numpy-reference.jpg")

    engine = _DistanceMappingEngine(
        faces=[_make_face(1), _make_face(2)],
        distance_by_face_and_group={
            (1, "A"): 0.1,
            (1, "B"): 0.9,
            (2, "A"): 0.9,
            (2, "B"): 0.1,
        },
    )

    evaluation = evaluate_candidate_photo(
        photo,
        np.array([[10.0, 0.0], [11.0, 0.0]], dtype=np.float32),
        np.array([[30.0, 0.0]], dtype=np.float32),
        engine=engine,
    )

    assert evaluation.detected_face_count == 2
    assert evaluation.bucket is MatchBucket.ONLY_TWO


def test_evaluate_candidate_photo_accepts_flat_list_reference_embeddings(tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "flat-list-reference.jpg")

    engine = _DistanceMappingEngine(
        faces=[_make_face(1), _make_face(2)],
        distance_by_face_and_group={
            (1, "A"): 0.1,
            (1, "B"): 0.9,
            (2, "A"): 0.9,
            (2, "B"): 0.1,
        },
    )

    evaluation = evaluate_candidate_photo(
        photo,
        [10.0, 0.0],
        [30.0, 0.0],
        engine=engine,
    )

    assert evaluation.detected_face_count == 2
    assert evaluation.bucket is MatchBucket.ONLY_TWO


def test_evaluate_candidate_photo_prefers_explicit_engine_over_cached(monkeypatch, tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "explicit-engine.jpg")

    explicit_calls = []

    class ExplicitEngine:
        def detect_faces(self, image_path):
            explicit_calls.append(image_path)
            return [_make_face(1), _make_face(2)]

        def min_distance(self, embedding, references):
            face_id = int(np.asarray(embedding, dtype=float).reshape(-1)[0])
            group = "A" if _first_reference_scalar(references) < 20 else "B"
            if face_id == 1 and group == "A":
                return 0.1
            if face_id == 2 and group == "B":
                return 0.1
            return 0.9

        def is_match(self, distance):
            return distance <= 0.5

    monkeypatch.setattr(
        "hikbox_pictures.matcher._get_cached_matcher_engine",
        lambda: pytest.fail("传入显式 engine 时不应回退到缓存引擎"),
    )

    evaluation = evaluate_candidate_photo(
        photo,
        [[10.0]],
        [[30.0]],
        engine=ExplicitEngine(),
    )

    assert explicit_calls == [photo.path]
    assert evaluation.detected_face_count == 2
    assert evaluation.bucket is MatchBucket.ONLY_TWO
