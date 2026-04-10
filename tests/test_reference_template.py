from pathlib import Path

import numpy as np
import pytest

from hikbox_pictures.models import ReferenceSample
from hikbox_pictures.reference_template import (
    build_reference_template,
    compute_template_match,
    select_template_threshold,
)


class FakeEngine:
    def __init__(self, distance_map: dict[tuple[float, float], float]) -> None:
        self.distance_map = distance_map

    def distance(self, lhs, rhs) -> float:
        lhs_key = float(np.asarray(lhs, dtype=np.float32).reshape(-1)[0])
        rhs_key = float(np.asarray(rhs, dtype=np.float32).reshape(-1)[0])
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
        _sample(tmp_path, "a.jpg", 1.0, area=0.7, sharpness=10.0),
        _sample(tmp_path, "b.jpg", 1.1, area=0.6, sharpness=9.0),
        _sample(tmp_path, "c.jpg", 5.0, area=0.1, sharpness=1.0),
    ]
    engine = FakeEngine(
        {
            (1.0, 1.0): 0.0,
            (1.0, 1.1): 0.1,
            (1.0, 5.0): 0.9,
            (1.1, 1.0): 0.1,
            (1.1, 1.1): 0.0,
            (1.1, 5.0): 0.8,
            (5.0, 1.0): 0.9,
            (5.0, 1.1): 0.8,
            (5.0, 5.0): 0.0,
        }
    )

    template = build_reference_template("A", samples, engine=engine, default_threshold=0.42)

    assert [sample.path.name for sample in template.kept_samples] == ["a.jpg", "b.jpg", "c.jpg"]
    assert template.top_k == 3
    assert template.match_threshold == 0.42


def test_build_reference_template_drops_outlier_when_reference_set_is_large(tmp_path: Path) -> None:
    samples = [
        _sample(tmp_path, "a.jpg", 1.0, area=0.8, sharpness=10.0),
        _sample(tmp_path, "b.jpg", 1.1, area=0.7, sharpness=9.5),
        _sample(tmp_path, "c.jpg", 1.2, area=0.7, sharpness=9.0),
        _sample(tmp_path, "d.jpg", 1.3, area=0.6, sharpness=8.5),
        _sample(tmp_path, "outlier.jpg", 5.0, area=0.1, sharpness=1.0),
    ]
    engine = FakeEngine({
        (1.0, 1.0): 0.0, (1.0, 1.1): 0.1, (1.0, 1.2): 0.2, (1.0, 1.3): 0.3, (1.0, 5.0): 2.5,
        (1.1, 1.0): 0.1, (1.1, 1.1): 0.0, (1.1, 1.2): 0.1, (1.1, 1.3): 0.2, (1.1, 5.0): 2.4,
        (1.2, 1.0): 0.2, (1.2, 1.1): 0.1, (1.2, 1.2): 0.0, (1.2, 1.3): 0.1, (1.2, 5.0): 2.3,
        (1.3, 1.0): 0.3, (1.3, 1.1): 0.2, (1.3, 1.2): 0.1, (1.3, 1.3): 0.0, (1.3, 5.0): 2.2,
        (5.0, 1.0): 2.5, (5.0, 1.1): 2.4, (5.0, 1.2): 2.3, (5.0, 1.3): 2.2, (5.0, 5.0): 0.0,
    })

    template = build_reference_template("A", samples, engine=engine, default_threshold=0.5)

    assert [sample.path.name for sample in template.kept_samples] == ["a.jpg", "b.jpg", "c.jpg", "d.jpg"]
    assert [sample.path.name for sample in template.dropped_samples] == ["outlier.jpg"]
    assert template.top_k == 3


def test_compute_template_match_uses_top_k_mean_and_centroid_distance(tmp_path: Path) -> None:
    samples = [
        _sample(tmp_path, "a.jpg", 1.0, area=0.7, sharpness=10.0),
        _sample(tmp_path, "b.jpg", 1.1, area=0.7, sharpness=9.0),
        _sample(tmp_path, "c.jpg", 1.2, area=0.7, sharpness=8.0),
    ]
    engine = FakeEngine({
        (9.0, 1.0): 0.2,
        (9.0, 1.1): 0.1,
        (9.0, 1.2): 0.3,
        (9.0, 9.9): 0.15,
    })

    template = build_reference_template(
        "A",
        samples,
        engine=engine,
        default_threshold=0.18,
        centroid_embedding=np.asarray([9.9], dtype=np.float32),
    )

    result = compute_template_match(np.asarray([9.0], dtype=np.float32), template, engine=engine)

    assert result.template_distance == pytest.approx(0.2)
    assert result.centroid_distance == pytest.approx(0.15)
    assert result.top_k_distances == pytest.approx([0.1, 0.2, 0.3])
    assert result.matched is False


def test_select_template_threshold_prefers_override_then_global_then_engine_default() -> None:
    assert select_template_threshold(override_threshold=0.3, fallback_threshold=0.4, engine_threshold=0.5) == 0.3
    assert select_template_threshold(override_threshold=None, fallback_threshold=0.4, engine_threshold=0.5) == 0.4
    assert select_template_threshold(override_threshold=None, fallback_threshold=None, engine_threshold=0.5) == 0.5
