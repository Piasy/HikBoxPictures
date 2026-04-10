from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from hikbox_pictures.models import ReferenceSample, ReferenceTemplate
from hikbox_pictures.reference_template import (
    build_reference_template,
    compute_template_match,
    select_template_threshold,
)


class FakeEngine:
    def __init__(
        self,
        distance_map: dict[tuple[int, int], float],
        *,
        complete_labels: set[int] | None = None,
    ) -> None:
        self.distance_map = dict(distance_map)
        self.calls: list[tuple[int, int]] = []

        if complete_labels is not None:
            for lhs in complete_labels:
                for rhs in complete_labels:
                    if (lhs, rhs) not in self.distance_map:
                        raise ValueError(f"缺少映射: ({lhs}, {rhs})")

    def _to_label(self, embedding: object) -> int:
        scalar = float(np.asarray(embedding, dtype=np.float32).reshape(-1)[0])
        if not float(scalar).is_integer():
            raise ValueError(f"embedding 第一维必须是整数标签，当前值: {scalar}")
        return int(scalar)

    def distance(self, lhs, rhs) -> float:
        lhs_key = self._to_label(lhs)
        rhs_key = self._to_label(rhs)
        self.calls.append((lhs_key, rhs_key))
        return self.distance_map[(lhs_key, rhs_key)]


def _sample(tmp_path: Path, name: str, embedding_scalar: float, *, area: float, sharpness: float) -> ReferenceSample:
    return ReferenceSample(
        path=tmp_path / name,
        embedding=np.asarray([embedding_scalar], dtype=np.float32),
        bbox=(0, 10, 10, 0),
        image_size=(20, 20),
        face_area_ratio=area,
        sharpness_score=sharpness,
        quality_score=0.0,
        center_distance=None,
        kept=True,
        drop_reason=None,
    )


def test_build_reference_template_does_not_drop_small_reference_sets(tmp_path: Path) -> None:
    samples = [
        _sample(tmp_path, "a.jpg", 10.0, area=0.7, sharpness=10.0),
        _sample(tmp_path, "b.jpg", 11.0, area=0.6, sharpness=9.0),
        _sample(tmp_path, "c.jpg", 50.0, area=0.1, sharpness=1.0),
    ]
    engine = FakeEngine(
        {
            (10, 10): 0.0,
            (10, 11): 0.1,
            (10, 50): 0.9,
            (11, 10): 0.1,
            (11, 11): 0.0,
            (11, 50): 0.8,
            (50, 10): 0.9,
            (50, 11): 0.8,
            (50, 50): 0.0,
        },
        complete_labels={10, 11, 50},
    )

    template = build_reference_template("A", samples, engine=engine, default_threshold=0.42)

    assert [sample.path.name for sample in template.kept_samples] == ["a.jpg", "b.jpg", "c.jpg"]
    assert template.top_k == 3
    assert template.match_threshold == 0.42
    assert [sample.drop_reason for sample in template.samples] == [None, None, None]
    assert [sample.center_distance for sample in template.samples] == pytest.approx([0.1, 0.1, 0.8])
    assert [sample.quality_score for sample in template.samples] == pytest.approx([1.0, 0.8555556, 0.0])
    assert float(template.centroid_embedding[0]) == pytest.approx(1.0, rel=1e-6)


def test_build_reference_template_drops_outlier_when_reference_set_is_large(tmp_path: Path) -> None:
    samples = [
        _sample(tmp_path, "a.jpg", 10.0, area=0.8, sharpness=10.0),
        _sample(tmp_path, "b.jpg", 11.0, area=0.7, sharpness=9.5),
        _sample(tmp_path, "c.jpg", 12.0, area=0.7, sharpness=9.0),
        _sample(tmp_path, "d.jpg", 13.0, area=0.6, sharpness=8.5),
        _sample(tmp_path, "outlier.jpg", 50.0, area=0.1, sharpness=1.0),
    ]
    engine = FakeEngine(
        {
            (10, 10): 0.0,
            (10, 11): 0.1,
            (10, 12): 0.2,
            (10, 13): 0.3,
            (10, 50): 2.5,
            (11, 10): 0.1,
            (11, 11): 0.0,
            (11, 12): 0.1,
            (11, 13): 0.2,
            (11, 50): 2.4,
            (12, 10): 0.2,
            (12, 11): 0.1,
            (12, 12): 0.0,
            (12, 13): 0.1,
            (12, 50): 2.3,
            (13, 10): 0.3,
            (13, 11): 0.2,
            (13, 12): 0.1,
            (13, 13): 0.0,
            (13, 50): 2.2,
            (50, 10): 2.5,
            (50, 11): 2.4,
            (50, 12): 2.3,
            (50, 13): 2.2,
            (50, 50): 0.0,
        },
        complete_labels={10, 11, 12, 13, 50},
    )

    template = build_reference_template("A", samples, engine=engine, default_threshold=0.5)

    assert [sample.path.name for sample in template.kept_samples] == ["a.jpg", "b.jpg", "c.jpg", "d.jpg"]
    assert [sample.path.name for sample in template.dropped_samples] == ["outlier.jpg"]
    assert template.top_k == 3
    assert [sample.drop_reason for sample in template.samples] == [None, None, None, None, "outlier"]

    outlier_sample = template.samples[-1]
    kept_center_distances = [sample.center_distance for sample in template.samples[:-1]]
    assert outlier_sample.center_distance is not None
    assert max(distance for distance in kept_center_distances if distance is not None) < outlier_sample.center_distance


@pytest.mark.parametrize("non_finite_distance", [float("nan"), float("inf"), float("-inf")])
def test_build_reference_template_rejects_non_finite_distance(tmp_path: Path, non_finite_distance: float) -> None:
    samples = [
        _sample(tmp_path, "a.jpg", 10.0, area=0.7, sharpness=10.0),
        _sample(tmp_path, "b.jpg", 11.0, area=0.6, sharpness=9.0),
    ]
    engine = FakeEngine(
        {
            (10, 10): 0.0,
            (10, 11): non_finite_distance,
            (11, 10): 0.1,
            (11, 11): 0.0,
        },
        complete_labels={10, 11},
    )

    with pytest.raises(ValueError, match="distance"):
        build_reference_template("A", samples, engine=engine, default_threshold=0.5)


def test_build_reference_template_rejects_external_centroid_non_vector(tmp_path: Path) -> None:
    samples = [
        _sample(tmp_path, "a.jpg", 10.0, area=0.7, sharpness=10.0),
        _sample(tmp_path, "b.jpg", 11.0, area=0.6, sharpness=9.0),
    ]
    engine = FakeEngine(
        {
            (10, 10): 0.0,
            (10, 11): 0.1,
            (11, 10): 0.1,
            (11, 11): 0.0,
        },
        complete_labels={10, 11},
    )

    with pytest.raises(ValueError, match="1 维"):
        build_reference_template(
            "A",
            samples,
            engine=engine,
            default_threshold=0.5,
            centroid_embedding=np.asarray([[99.0]], dtype=np.float32),
        )


def test_build_reference_template_rejects_external_centroid_dimension_mismatch(tmp_path: Path) -> None:
    samples = [
        _sample(tmp_path, "a.jpg", 10.0, area=0.7, sharpness=10.0),
        _sample(tmp_path, "b.jpg", 11.0, area=0.6, sharpness=9.0),
    ]
    engine = FakeEngine(
        {
            (10, 10): 0.0,
            (10, 11): 0.1,
            (11, 10): 0.1,
            (11, 11): 0.0,
        },
        complete_labels={10, 11},
    )

    with pytest.raises(ValueError, match="维度"):
        build_reference_template(
            "A",
            samples,
            engine=engine,
            default_threshold=0.5,
            centroid_embedding=np.asarray([99.0, 100.0], dtype=np.float32),
        )


@pytest.mark.parametrize("non_finite_value", [float("nan"), float("inf"), float("-inf")])
def test_build_reference_template_rejects_external_centroid_non_finite(tmp_path: Path, non_finite_value: float) -> None:
    samples = [
        _sample(tmp_path, "a.jpg", 10.0, area=0.7, sharpness=10.0),
        _sample(tmp_path, "b.jpg", 11.0, area=0.6, sharpness=9.0),
    ]
    engine = FakeEngine(
        {
            (10, 10): 0.0,
            (10, 11): 0.1,
            (11, 10): 0.1,
            (11, 11): 0.0,
        },
        complete_labels={10, 11},
    )

    with pytest.raises(ValueError, match="centroid_embedding"):
        build_reference_template(
            "A",
            samples,
            engine=engine,
            default_threshold=0.5,
            centroid_embedding=np.asarray([non_finite_value], dtype=np.float32),
        )


def test_compute_template_match_uses_top_k_mean_and_centroid_distance(tmp_path: Path) -> None:
    samples = [
        _sample(tmp_path, "a.jpg", 10.0, area=0.7, sharpness=10.0),
        _sample(tmp_path, "b.jpg", 11.0, area=0.7, sharpness=9.0),
        _sample(tmp_path, "c.jpg", 12.0, area=0.7, sharpness=8.0),
    ]
    engine = FakeEngine(
        {
            (10, 10): 0.0,
            (10, 11): 0.1,
            (10, 12): 0.2,
            (11, 10): 0.1,
            (11, 11): 0.0,
            (11, 12): 0.1,
            (12, 10): 0.2,
            (12, 11): 0.1,
            (12, 12): 0.0,
            (90, 10): 0.2,
            (90, 11): 0.1,
            (90, 12): 0.3,
            (90, 99): 0.15,
        },
        complete_labels={10, 11, 12},
    )

    template = build_reference_template(
        "A",
        samples,
        engine=engine,
        default_threshold=0.18,
        centroid_embedding=np.asarray([99.0], dtype=np.float32),
    )

    result = compute_template_match(np.asarray([90.0], dtype=np.float32), template, engine=engine)

    assert result.template_distance == pytest.approx(0.2)
    assert result.centroid_distance == pytest.approx(0.15)
    assert result.top_k_distances == pytest.approx([0.1, 0.2, 0.3])
    assert result.matched is False


def test_compute_template_match_works_with_internal_built_centroid(tmp_path: Path) -> None:
    samples = [
        _sample(tmp_path, "a.jpg", 10.0, area=0.7, sharpness=10.0),
        _sample(tmp_path, "b.jpg", 11.0, area=0.7, sharpness=9.0),
        _sample(tmp_path, "c.jpg", 12.0, area=0.7, sharpness=8.0),
    ]
    engine = FakeEngine(
        {
            (10, 10): 0.0,
            (10, 11): 0.1,
            (10, 12): 0.2,
            (11, 10): 0.1,
            (11, 11): 0.0,
            (11, 12): 0.1,
            (12, 10): 0.2,
            (12, 11): 0.1,
            (12, 12): 0.0,
            (90, 10): 0.2,
            (90, 11): 0.1,
            (90, 12): 0.3,
            (90, 1): 0.12,
        }
    )

    template = build_reference_template("A", samples, engine=engine, default_threshold=0.21)

    result = compute_template_match(np.asarray([90.0], dtype=np.float32), template, engine=engine)

    assert float(template.centroid_embedding[0]) == pytest.approx(1.0, rel=1e-6)
    assert result.template_distance == pytest.approx(0.2)
    assert result.centroid_distance == pytest.approx(0.12)
    assert result.top_k_distances == pytest.approx([0.1, 0.2, 0.3])
    assert result.matched is True


@pytest.mark.parametrize("non_finite_distance", [float("nan"), float("inf"), float("-inf")])
def test_compute_template_match_rejects_non_finite_distance(tmp_path: Path, non_finite_distance: float) -> None:
    samples = [
        _sample(tmp_path, "a.jpg", 10.0, area=0.7, sharpness=10.0),
        _sample(tmp_path, "b.jpg", 11.0, area=0.7, sharpness=9.0),
        _sample(tmp_path, "c.jpg", 12.0, area=0.7, sharpness=8.0),
    ]
    engine = FakeEngine(
        {
            (10, 10): 0.0,
            (10, 11): 0.1,
            (10, 12): 0.2,
            (11, 10): 0.1,
            (11, 11): 0.0,
            (11, 12): 0.1,
            (12, 10): 0.2,
            (12, 11): 0.1,
            (12, 12): 0.0,
            (90, 10): 0.2,
            (90, 11): non_finite_distance,
            (90, 12): 0.3,
            (90, 99): 0.15,
        },
        complete_labels={10, 11, 12},
    )
    template = build_reference_template(
        "A",
        samples,
        engine=engine,
        default_threshold=0.3,
        centroid_embedding=np.asarray([99.0], dtype=np.float32),
    )

    with pytest.raises(ValueError, match="distance"):
        compute_template_match(np.asarray([90.0], dtype=np.float32), template, engine=engine)


def test_compute_template_match_rejects_empty_kept_samples(tmp_path: Path) -> None:
    dropped_sample = replace(
        _sample(tmp_path, "dropped.jpg", 10.0, area=0.4, sharpness=3.0),
        kept=False,
        drop_reason="outlier",
    )
    template = ReferenceTemplate(
        name="A",
        samples=(dropped_sample,),
        kept_samples=tuple(),
        centroid_embedding=np.asarray([99.0], dtype=np.float32),
        match_threshold=0.3,
        top_k=1,
    )
    engine = FakeEngine({(90, 99): 0.2})

    with pytest.raises(ValueError, match="kept_samples"):
        compute_template_match(np.asarray([90.0], dtype=np.float32), template, engine=engine)


def test_compute_template_match_rejects_non_positive_top_k(tmp_path: Path) -> None:
    samples = [
        _sample(tmp_path, "a.jpg", 10.0, area=0.7, sharpness=10.0),
        _sample(tmp_path, "b.jpg", 11.0, area=0.7, sharpness=9.0),
        _sample(tmp_path, "c.jpg", 12.0, area=0.7, sharpness=8.0),
    ]
    engine = FakeEngine(
        {
            (10, 10): 0.0,
            (10, 11): 0.1,
            (10, 12): 0.2,
            (11, 10): 0.1,
            (11, 11): 0.0,
            (11, 12): 0.1,
            (12, 10): 0.2,
            (12, 11): 0.1,
            (12, 12): 0.0,
            (90, 99): 0.2,
        },
        complete_labels={10, 11, 12},
    )
    template = build_reference_template(
        "A",
        samples,
        engine=engine,
        default_threshold=0.3,
        centroid_embedding=np.asarray([99.0], dtype=np.float32),
    )
    bad_template = replace(template, top_k=0)

    with pytest.raises(ValueError, match="top_k"):
        compute_template_match(np.asarray([90.0], dtype=np.float32), bad_template, engine=engine)


def test_compute_template_match_missing_distance_pair_raises_key_error_without_mutating_engine_map(tmp_path: Path) -> None:
    samples = [_sample(tmp_path, "a.jpg", 10.0, area=0.7, sharpness=10.0)]
    engine = FakeEngine({(10, 10): 0.0}, complete_labels={10})
    original_map = engine.distance_map

    template = build_reference_template("A", samples, engine=engine, default_threshold=0.3)

    with pytest.raises(KeyError):
        compute_template_match(np.asarray([90.0], dtype=np.float32), template, engine=engine)

    assert engine.distance_map is original_map


def test_select_template_threshold_prefers_override_then_global_then_engine_default() -> None:
    assert select_template_threshold(override_threshold=0.3, fallback_threshold=0.4, engine_threshold=0.5) == 0.3
    assert select_template_threshold(override_threshold=None, fallback_threshold=0.4, engine_threshold=0.5) == 0.4
    assert select_template_threshold(override_threshold=None, fallback_threshold=None, engine_threshold=0.5) == 0.5


@pytest.mark.parametrize("non_finite_value", [float("nan"), float("inf"), float("-inf")])
def test_select_template_threshold_rejects_non_finite_values(non_finite_value: float) -> None:
    with pytest.raises(ValueError, match="有限值"):
        select_template_threshold(
            override_threshold=non_finite_value,
            fallback_threshold=0.4,
            engine_threshold=0.5,
        )

    with pytest.raises(ValueError, match="有限值"):
        select_template_threshold(
            override_threshold=None,
            fallback_threshold=non_finite_value,
            engine_threshold=0.5,
        )

    with pytest.raises(ValueError, match="有限值"):
        select_template_threshold(
            override_threshold=None,
            fallback_threshold=None,
            engine_threshold=non_finite_value,
        )
