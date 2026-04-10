from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from hikbox_pictures.deepface_engine import (
    DeepFaceEngine,
    DeepFaceInferenceError,
    DeepFaceInitError,
)


def test_create_uses_default_config_and_threshold(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_find_threshold(model_name: str, distance_metric: str) -> float:
        captured["args"] = (model_name, distance_metric)
        return 0.42

    monkeypatch.setattr("hikbox_pictures.deepface_engine.DeepFace", object())
    monkeypatch.setattr(
        "hikbox_pictures.deepface_engine.verification",
        SimpleNamespace(find_threshold=fake_find_threshold, find_distance=lambda *_args, **_kwargs: 0.0),
    )

    engine = DeepFaceEngine.create()

    assert engine.model_name == "ArcFace"
    assert engine.detector_backend == "retinaface"
    assert engine.distance_metric == "cosine"
    assert engine.align is True
    assert engine.distance_threshold == 0.42
    assert engine.threshold_source == "deepface-default"
    assert captured["args"] == ("ArcFace", "cosine")


def test_create_uses_explicit_distance_threshold(monkeypatch) -> None:
    def fail_find_threshold(*_args, **_kwargs):
        raise AssertionError("不应调用 find_threshold")

    monkeypatch.setattr("hikbox_pictures.deepface_engine.DeepFace", object())
    monkeypatch.setattr(
        "hikbox_pictures.deepface_engine.verification",
        SimpleNamespace(find_threshold=fail_find_threshold, find_distance=lambda *_args, **_kwargs: 0.0),
    )

    engine = DeepFaceEngine.create(distance_threshold=0.15)

    assert engine.distance_threshold == 0.15
    assert engine.threshold_source == "explicit"


@pytest.mark.parametrize(
    "represent_payload",
    [
        {
            "facial_area": {"x": 10, "y": 20, "w": 30, "h": 40},
            "embedding": [0.1, 0.2, 0.3],
        },
        [
            {
                "facial_area": {"x": 10, "y": 20, "w": 30, "h": 40},
                "embedding": [0.1, 0.2, 0.3],
            }
        ],
    ],
)
def test_detect_faces_maps_facial_area_to_bbox(tmp_path: Path, represent_payload) -> None:
    image_path = tmp_path / "sample.jpg"
    image_path.write_bytes(b"image")

    engine = DeepFaceEngine(
        model_name="ArcFace",
        detector_backend="retinaface",
        distance_metric="cosine",
        align=True,
        distance_threshold=0.42,
        threshold_source="explicit",
        deepface_module=SimpleNamespace(represent=lambda **_kwargs: represent_payload),
        verification_module=SimpleNamespace(find_distance=lambda *_args, **_kwargs: 0.0),
    )

    faces = engine.detect_faces(image_path)

    assert len(faces) == 1
    assert faces[0].bbox == (20, 40, 60, 10)
    np.testing.assert_allclose(faces[0].embedding, np.array([0.1, 0.2, 0.3], dtype=np.float32))
    assert faces[0].embedding.dtype == np.float32


def test_distance_uses_configured_metric(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_find_distance(lhs, rhs, distance_metric: str) -> float:
        captured["args"] = (lhs, rhs, distance_metric)
        return 0.33

    monkeypatch.setattr("hikbox_pictures.deepface_engine.DeepFace", object())
    monkeypatch.setattr(
        "hikbox_pictures.deepface_engine.verification",
        SimpleNamespace(find_threshold=lambda *_args, **_kwargs: 0.5, find_distance=fake_find_distance),
    )

    engine = DeepFaceEngine.create(distance_metric="euclidean")

    result = engine.distance([0.1], np.array([0.2]))

    assert result == 0.33
    lhs, rhs, metric = captured["args"]
    assert lhs == [0.1]
    np.testing.assert_allclose(rhs, np.array([0.2]))
    assert metric == "euclidean"


def test_min_distance_returns_inf_for_empty_references() -> None:
    engine = DeepFaceEngine(
        model_name="ArcFace",
        detector_backend="retinaface",
        distance_metric="cosine",
        align=True,
        distance_threshold=0.42,
        threshold_source="explicit",
        deepface_module=SimpleNamespace(represent=lambda **_kwargs: []),
        verification_module=SimpleNamespace(find_distance=lambda *_args, **_kwargs: 0.0),
    )

    result = engine.min_distance(np.array([0.1], dtype=np.float32), [])

    assert result == float("inf")


def test_min_distance_accepts_numpy_array_references() -> None:
    def fake_find_distance(lhs, rhs, _metric: str) -> float:
        lhs_array = np.array(lhs, dtype=np.float32)
        rhs_array = np.array(rhs, dtype=np.float32)
        return float(np.linalg.norm(lhs_array - rhs_array))

    engine = DeepFaceEngine(
        model_name="ArcFace",
        detector_backend="retinaface",
        distance_metric="cosine",
        align=True,
        distance_threshold=0.42,
        threshold_source="explicit",
        deepface_module=SimpleNamespace(represent=lambda **_kwargs: []),
        verification_module=SimpleNamespace(find_distance=fake_find_distance),
    )

    embedding = np.array([0.2, 0.4], dtype=np.float32)
    references = np.array([[0.1, 0.3], [0.2, 0.4], [0.5, 0.8]], dtype=np.float32)

    result = engine.min_distance(embedding, references)

    assert result == 0.0


def test_is_match_hits_threshold_boundary() -> None:
    engine = DeepFaceEngine(
        model_name="ArcFace",
        detector_backend="retinaface",
        distance_metric="cosine",
        align=True,
        distance_threshold=0.42,
        threshold_source="explicit",
        deepface_module=SimpleNamespace(represent=lambda **_kwargs: []),
        verification_module=SimpleNamespace(find_distance=lambda *_args, **_kwargs: 0.0),
    )

    assert engine.is_match(0.42) is True
    assert engine.is_match(0.420001) is False


def test_create_raises_when_deepface_missing(monkeypatch) -> None:
    monkeypatch.setattr("hikbox_pictures.deepface_engine.DeepFace", None)
    monkeypatch.setattr("hikbox_pictures.deepface_engine.verification", None)

    with pytest.raises(DeepFaceInitError, match="deepface 未安装或不可用"):
        DeepFaceEngine.create()


def test_detect_faces_wraps_inference_error(tmp_path: Path) -> None:
    image_path = tmp_path / "broken.jpg"
    image_path.write_bytes(b"image")

    def raise_error(**_kwargs):
        raise RuntimeError("infer failed")

    engine = DeepFaceEngine(
        model_name="ArcFace",
        detector_backend="retinaface",
        distance_metric="cosine",
        align=True,
        distance_threshold=0.42,
        threshold_source="explicit",
        deepface_module=SimpleNamespace(represent=raise_error),
        verification_module=SimpleNamespace(find_distance=lambda *_args, **_kwargs: 0.0),
    )

    with pytest.raises(DeepFaceInferenceError, match="infer failed"):
        engine.detect_faces(image_path)


@pytest.mark.parametrize(
    "represent_payload,error_message",
    [
        (
            {
                "facial_area": {"x": 10, "y": 20, "w": 30, "h": 40},
            },
            "embedding",
        ),
        (
            {
                "embedding": [0.1, 0.2, 0.3],
            },
            "facial_area",
        ),
    ],
)
def test_detect_faces_raises_for_dirty_payload(tmp_path: Path, represent_payload, error_message: str) -> None:
    image_path = tmp_path / "broken-payload.jpg"
    image_path.write_bytes(b"image")

    engine = DeepFaceEngine(
        model_name="ArcFace",
        detector_backend="retinaface",
        distance_metric="cosine",
        align=True,
        distance_threshold=0.42,
        threshold_source="explicit",
        deepface_module=SimpleNamespace(represent=lambda **_kwargs: represent_payload),
        verification_module=SimpleNamespace(find_distance=lambda *_args, **_kwargs: 0.0),
    )

    with pytest.raises(DeepFaceInferenceError, match=error_message):
        engine.detect_faces(image_path)
