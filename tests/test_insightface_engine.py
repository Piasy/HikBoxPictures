from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from hikbox_pictures.insightface_engine import (
    DetectedFace,
    InsightFaceEngine,
    InsightFaceInferenceError,
    InsightFaceInitError,
)


def test_create_uses_default_model_and_cpu_provider(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeAnalyzer:
        def __init__(self, *, name, providers):
            captured["name"] = name
            captured["providers"] = providers

        def prepare(self, *, ctx_id, det_size) -> None:
            captured["ctx_id"] = ctx_id
            captured["det_size"] = det_size

    monkeypatch.setattr("hikbox_pictures.insightface_engine.FaceAnalysis", FakeAnalyzer)

    engine = InsightFaceEngine.create()

    assert isinstance(engine, InsightFaceEngine)
    assert captured == {
        "name": "antelopev2",
        "providers": ["CPUExecutionProvider"],
        "ctx_id": 0,
        "det_size": (640, 640),
    }


def test_detect_faces_maps_bbox_and_returns_embedding(monkeypatch, tmp_path: Path) -> None:
    image_path = tmp_path / "sample.jpg"
    image_path.write_bytes(b"image")

    analyzer = SimpleNamespace(
        get=lambda image: [
            SimpleNamespace(
                bbox=[10.8, 20.2, 30.9, 40.1],
                embedding=[0.1, 0.2, 0.3],
            )
        ]
    )
    monkeypatch.setattr("hikbox_pictures.insightface_engine.load_rgb_image", lambda _: "rgb")
    engine = InsightFaceEngine(analyzer=analyzer)

    faces = engine.detect_faces(image_path)

    assert faces == [
        DetectedFace(
            bbox=(20, 30, 40, 10),
            embedding=[0.1, 0.2, 0.3],
        )
    ]


def test_create_wraps_init_errors(monkeypatch) -> None:
    def raise_init_error(*_args, **_kwargs):
        raise RuntimeError("init failed")

    monkeypatch.setattr("hikbox_pictures.insightface_engine.FaceAnalysis", raise_init_error)

    with pytest.raises(InsightFaceInitError, match="init failed"):
        InsightFaceEngine.create()


def test_detect_faces_wraps_inference_errors(monkeypatch, tmp_path: Path) -> None:
    image_path = tmp_path / "broken.jpg"
    image_path.write_bytes(b"image")

    monkeypatch.setattr("hikbox_pictures.insightface_engine.load_rgb_image", lambda _: "rgb")
    analyzer = SimpleNamespace(get=lambda _image: (_ for _ in ()).throw(RuntimeError("infer failed")))
    engine = InsightFaceEngine(analyzer=analyzer)

    with pytest.raises(InsightFaceInferenceError, match="infer failed"):
        engine.detect_faces(image_path)
