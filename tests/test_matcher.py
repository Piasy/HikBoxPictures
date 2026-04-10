from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from hikbox_pictures.matcher import (
    CandidateDecodeError,
    DEFAULT_DISTANCE_THRESHOLD,
    _select_largest_matching_pair,
    evaluate_candidate_photo,
)
from hikbox_pictures.models import CandidatePhoto, MatchBucket, ReferenceSample, ReferenceTemplate


class _TemplateDistanceEngine:
    def __init__(self, *, faces, distance_by_label_pair):
        self._faces = faces
        self._distance_by_label_pair = dict(distance_by_label_pair)
        self.detect_calls = []
        self.distance_calls = []

    def detect_faces(self, image_path):
        self.detect_calls.append(image_path)
        return self._faces

    def distance(self, lhs, rhs):
        lhs_label = int(np.asarray(lhs, dtype=float).reshape(-1)[0])
        rhs_label = int(np.asarray(rhs, dtype=float).reshape(-1)[0])
        self.distance_calls.append((lhs_label, rhs_label))
        return self._distance_by_label_pair[(lhs_label, rhs_label)]


def _make_face(face_id: int, bbox=None):
    payload = {"embedding": np.asarray([float(face_id)], dtype=np.float32)}
    if bbox is not None:
        payload["bbox"] = bbox
    return SimpleNamespace(**payload)


def _make_template(tmp_path: Path, *, name: str, sample_label: int, threshold: float) -> ReferenceTemplate:
    sample = ReferenceSample(
        path=tmp_path / f"{name}-sample.jpg",
        embedding=np.asarray([float(sample_label)], dtype=np.float32),
        bbox=(0, 10, 10, 0),
        image_size=(20, 20),
        face_area_ratio=0.5,
        sharpness_score=10.0,
        quality_score=1.0,
        center_distance=0.0,
        kept=True,
        drop_reason=None,
    )
    return ReferenceTemplate(
        name=name,
        samples=(sample,),
        kept_samples=(sample,),
        centroid_embedding=np.asarray([float(sample_label)], dtype=np.float32),
        match_threshold=threshold,
        top_k=1,
    )


def test_evaluate_candidate_photo_uses_template_threshold_and_joint_distance(monkeypatch, tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "template-threshold.jpg")
    template_a = _make_template(tmp_path, name="A", sample_label=10, threshold=0.35)
    template_b = _make_template(tmp_path, name="B", sample_label=20, threshold=0.45)

    engine = _TemplateDistanceEngine(
        faces=[_make_face(1), _make_face(2)],
        distance_by_label_pair={
            (1, 10): 0.2,
            (1, 20): 0.1,
            (2, 10): 0.3,
            (2, 20): 0.45,
        },
    )

    monkeypatch.setattr(
        "hikbox_pictures.matcher._get_cached_matcher_engine",
        lambda: pytest.fail("传入显式 engine 时不应回退到缓存引擎"),
    )

    evaluation = evaluate_candidate_photo(
        photo,
        template_a,
        template_b,
        engine=engine,
    )

    assert evaluation.detected_face_count == 2
    assert evaluation.bucket is MatchBucket.ONLY_TWO
    assert evaluation.joint_distance == pytest.approx(0.3)
    assert evaluation.best_match_pair == (1, 0)


def test_evaluate_candidate_photo_requires_distinct_matching_faces(tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "ambiguous.jpg")
    template_a = _make_template(tmp_path, name="A", sample_label=10, threshold=0.2)
    template_b = _make_template(tmp_path, name="B", sample_label=20, threshold=0.2)

    engine = _TemplateDistanceEngine(
        faces=[_make_face(1)],
        distance_by_label_pair={
            (1, 10): 0.1,
            (1, 20): 0.1,
        },
    )

    evaluation = evaluate_candidate_photo(photo, template_a, template_b, engine=engine)

    assert evaluation.detected_face_count == 1
    assert evaluation.bucket is None
    assert evaluation.joint_distance is None
    assert evaluation.best_match_pair is None


def test_evaluate_candidate_photo_classifies_group(tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "group.jpg")
    template_a = _make_template(tmp_path, name="A", sample_label=10, threshold=0.2)
    template_b = _make_template(tmp_path, name="B", sample_label=20, threshold=0.2)

    engine = _TemplateDistanceEngine(
        faces=[
            _make_face(1, bbox=(0, 10, 10, 0)),
            _make_face(2, bbox=(0, 10, 10, 0)),
            _make_face(3, bbox=(0, 6, 5, 0)),
        ],
        distance_by_label_pair={
            (1, 10): 0.1,
            (1, 20): 0.9,
            (2, 10): 0.9,
            (2, 20): 0.1,
            (3, 10): 0.9,
            (3, 20): 0.9,
        },
    )

    evaluation = evaluate_candidate_photo(photo, template_a, template_b, engine=engine)

    assert evaluation.detected_face_count == 3
    assert evaluation.bucket is MatchBucket.GROUP
    assert evaluation.joint_distance == pytest.approx(0.1)
    assert evaluation.best_match_pair == (0, 1)


def test_evaluate_candidate_photo_ignores_small_extra_face(tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "small-extra.jpg")
    template_a = _make_template(tmp_path, name="A", sample_label=10, threshold=0.2)
    template_b = _make_template(tmp_path, name="B", sample_label=20, threshold=0.2)

    engine = _TemplateDistanceEngine(
        faces=[
            _make_face(1, bbox=(0, 10, 10, 0)),
            _make_face(2, bbox=(0, 8, 10, 0)),
            _make_face(3, bbox=(0, 5, 2, 0)),
        ],
        distance_by_label_pair={
            (1, 10): 0.1,
            (1, 20): 0.9,
            (2, 10): 0.9,
            (2, 20): 0.1,
            (3, 10): 0.9,
            (3, 20): 0.9,
        },
    )

    evaluation = evaluate_candidate_photo(photo, template_a, template_b, engine=engine)

    assert evaluation.detected_face_count == 3
    assert evaluation.bucket is MatchBucket.ONLY_TWO
    assert evaluation.joint_distance == pytest.approx(0.1)
    assert evaluation.best_match_pair == (0, 1)


def test_evaluate_candidate_photo_requires_both_people(tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "solo.jpg")
    template_a = _make_template(tmp_path, name="A", sample_label=10, threshold=0.3)
    template_b = _make_template(tmp_path, name="B", sample_label=20, threshold=0.3)

    engine = _TemplateDistanceEngine(
        faces=[_make_face(1), _make_face(2)],
        distance_by_label_pair={
            (1, 10): 0.1,
            (1, 20): 0.9,
            (2, 10): 0.2,
            (2, 20): 0.8,
        },
    )

    evaluation = evaluate_candidate_photo(photo, template_a, template_b, engine=engine)

    assert evaluation.detected_face_count == 2
    assert evaluation.bucket is None
    assert evaluation.joint_distance is None
    assert evaluation.best_match_pair is None


def test_evaluate_candidate_photo_rejects_non_default_distance_threshold(tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "non-default-threshold.jpg")
    template_a = _make_template(tmp_path, name="A", sample_label=10, threshold=0.3)
    template_b = _make_template(tmp_path, name="B", sample_label=20, threshold=0.3)
    engine = _TemplateDistanceEngine(faces=[_make_face(1)], distance_by_label_pair={(1, 10): 0.1, (1, 20): 0.1})

    with pytest.raises(ValueError, match="distance_threshold"):
        evaluate_candidate_photo(
            photo,
            template_a,
            template_b,
            engine=engine,
            distance_threshold=DEFAULT_DISTANCE_THRESHOLD + 0.1,
        )


def test_evaluate_candidate_photo_rejects_legacy_tolerance(tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "legacy-tolerance.jpg")
    template_a = _make_template(tmp_path, name="A", sample_label=10, threshold=0.3)
    template_b = _make_template(tmp_path, name="B", sample_label=20, threshold=0.3)
    engine = _TemplateDistanceEngine(faces=[_make_face(1)], distance_by_label_pair={(1, 10): 0.1, (1, 20): 0.1})

    with pytest.raises(ValueError, match="tolerance"):
        evaluate_candidate_photo(
            photo,
            template_a,
            template_b,
            engine=engine,
            tolerance=0.3,
        )


def test_evaluate_candidate_photo_wraps_detection_errors(tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "broken.jpg")
    template_a = _make_template(tmp_path, name="A", sample_label=10, threshold=0.2)
    template_b = _make_template(tmp_path, name="B", sample_label=20, threshold=0.2)

    class FakeEngine:
        def detect_faces(self, image_path):
            assert image_path == photo.path
            raise RuntimeError("detect boom")

        def distance(self, lhs, rhs):
            return 0.1

    with pytest.raises(CandidateDecodeError, match="detect boom"):
        evaluate_candidate_photo(photo, template_a, template_b, engine=FakeEngine())


def test_evaluate_candidate_photo_wraps_missing_embedding_errors(tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "missing-embedding.jpg")
    template_a = _make_template(tmp_path, name="A", sample_label=10, threshold=0.2)
    template_b = _make_template(tmp_path, name="B", sample_label=20, threshold=0.2)

    class FakeEngine:
        def detect_faces(self, image_path):
            assert image_path == photo.path
            return [SimpleNamespace(bbox=(0, 10, 10, 0))]

        def distance(self, lhs, rhs):
            return 0.1

    with pytest.raises(CandidateDecodeError, match="Failed to decode"):
        evaluate_candidate_photo(photo, template_a, template_b, engine=FakeEngine())


def test_evaluate_candidate_photo_wraps_distance_errors(tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "distance-broken.jpg")
    template_a = _make_template(tmp_path, name="A", sample_label=10, threshold=0.2)
    template_b = _make_template(tmp_path, name="B", sample_label=20, threshold=0.2)

    class FakeEngine:
        def detect_faces(self, image_path):
            assert image_path == photo.path
            return [_make_face(1)]

        def distance(self, lhs, rhs):
            raise RuntimeError("distance boom")

    with pytest.raises(CandidateDecodeError, match="distance boom"):
        evaluate_candidate_photo(photo, template_a, template_b, engine=FakeEngine())


def test_evaluate_candidate_photo_fast_fails_on_incompatible_engine(tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "incompatible-engine.jpg")
    template_a = _make_template(tmp_path, name="A", sample_label=10, threshold=0.2)
    template_b = _make_template(tmp_path, name="B", sample_label=20, threshold=0.2)

    class IncompatibleEngine:
        def detect_faces(self, image_path):
            assert image_path == photo.path
            return [_make_face(1)]

    with pytest.raises(TypeError, match="distance"):
        evaluate_candidate_photo(photo, template_a, template_b, engine=IncompatibleEngine())


def test_evaluate_candidate_photo_wraps_cached_engine_init_errors(monkeypatch, tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "init-fail.jpg")
    template_a = _make_template(tmp_path, name="A", sample_label=10, threshold=0.2)
    template_b = _make_template(tmp_path, name="B", sample_label=20, threshold=0.2)

    def fake_cached_engine():
        raise RuntimeError("engine init boom")

    monkeypatch.setattr("hikbox_pictures.matcher._get_cached_matcher_engine", fake_cached_engine)

    with pytest.raises(CandidateDecodeError, match="engine init boom"):
        evaluate_candidate_photo(photo, template_a, template_b)


def test_evaluate_candidate_photo_prefers_explicit_engine_over_cached(monkeypatch, tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "explicit-engine.jpg")
    template_a = _make_template(tmp_path, name="A", sample_label=10, threshold=0.3)
    template_b = _make_template(tmp_path, name="B", sample_label=20, threshold=0.3)

    explicit_calls = []

    class ExplicitEngine:
        def detect_faces(self, image_path):
            explicit_calls.append(image_path)
            return [_make_face(1), _make_face(2)]

        def distance(self, lhs, rhs):
            lhs_label = int(np.asarray(lhs, dtype=float).reshape(-1)[0])
            rhs_label = int(np.asarray(rhs, dtype=float).reshape(-1)[0])
            if (lhs_label, rhs_label) in ((1, 10), (2, 20)):
                return 0.1
            return 0.9

    monkeypatch.setattr(
        "hikbox_pictures.matcher._get_cached_matcher_engine",
        lambda: pytest.fail("传入显式 engine 时不应回退到缓存引擎"),
    )

    evaluation = evaluate_candidate_photo(
        photo,
        template_a,
        template_b,
        engine=ExplicitEngine(),
    )

    assert explicit_calls == [photo.path]
    assert evaluation.detected_face_count == 2
    assert evaluation.bucket is MatchBucket.ONLY_TWO
    assert evaluation.joint_distance == pytest.approx(0.1)
    assert evaluation.best_match_pair == (0, 1)


def test_select_largest_matching_pair_uses_stable_tie_break() -> None:
    class OrderedIndexSet(set):
        def __init__(self, values, order):
            super().__init__(values)
            self._order = tuple(order)

        def __iter__(self):
            return iter(self._order)

    matches_a = OrderedIndexSet({0, 1}, [0, 1])
    matches_b = OrderedIndexSet({0, 2}, [2, 0])
    face_areas = [1, 2, 1]

    # 面积和相同时，先比较较小面积，再按索引稳定选择 (1, 0)。
    assert _select_largest_matching_pair(matches_a, matches_b, face_areas) == (1, 0)
