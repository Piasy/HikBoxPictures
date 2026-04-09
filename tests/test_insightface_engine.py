from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
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
        def __init__(self, *, name, root, providers):
            captured["name"] = name
            captured["root"] = root
            captured["providers"] = providers

        def prepare(self, *, ctx_id, det_size) -> None:
            captured["ctx_id"] = ctx_id
            captured["det_size"] = det_size

    monkeypatch.setattr("hikbox_pictures.insightface_engine.FaceAnalysis", FakeAnalyzer)

    engine = InsightFaceEngine.create()

    assert isinstance(engine, InsightFaceEngine)
    assert captured == {
        "name": "antelopev2",
        "root": str(Path("~/.insightface").expanduser()),
        "providers": ["CPUExecutionProvider"],
        "ctx_id": 0,
        "det_size": (512, 512),
    }


def test_create_keeps_explicit_empty_providers(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeAnalyzer:
        def __init__(self, *, name, root, providers):
            captured["name"] = name
            captured["root"] = root
            captured["providers"] = providers

        def prepare(self, *, ctx_id, det_size) -> None:
            captured["ctx_id"] = ctx_id
            captured["det_size"] = det_size

    monkeypatch.setattr("hikbox_pictures.insightface_engine.FaceAnalysis", FakeAnalyzer)

    InsightFaceEngine.create(providers=[])

    assert captured["name"] == "antelopev2"
    assert captured["providers"] == []
    assert captured["ctx_id"] == 0
    assert captured["det_size"] == (512, 512)


def test_create_uses_custom_det_size(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeAnalyzer:
        def __init__(self, *, name, root, providers):
            captured["name"] = name
            captured["root"] = root
            captured["providers"] = providers

        def prepare(self, *, ctx_id, det_size) -> None:
            captured["ctx_id"] = ctx_id
            captured["det_size"] = det_size

    monkeypatch.setattr("hikbox_pictures.insightface_engine.FaceAnalysis", FakeAnalyzer)

    InsightFaceEngine.create(det_size=(512, 512))

    assert captured["det_size"] == (512, 512)


def test_detect_faces_reprepare_with_override_det_size(monkeypatch, tmp_path: Path) -> None:
    image_path = tmp_path / "sample.jpg"
    image_path.write_bytes(b"image")
    prepare_calls: list[tuple[int, int]] = []

    class FakeAnalyzer:
        def prepare(self, *, ctx_id, det_size) -> None:
            prepare_calls.append(det_size)

        def get(self, image):
            return []

    monkeypatch.setattr(
        "hikbox_pictures.insightface_engine.load_rgb_image",
        lambda _: np.array([[[1, 2, 3]]], dtype=np.uint8),
    )
    engine = InsightFaceEngine(analyzer=FakeAnalyzer(), default_det_size=(640, 640), prepared_det_size=(640, 640))

    faces = engine.detect_faces(image_path, det_size=(512, 512))

    assert faces == []
    assert prepare_calls == [(512, 512)]
    assert engine.prepared_det_size == (512, 512)


def test_detected_face_bbox_uses_named_tlbr_alias() -> None:
    assert DetectedFace.__annotations__["bbox"] == "BBoxTLBR"


def test_create_repairs_nested_model_cache_layout(monkeypatch, tmp_path: Path) -> None:
    nested_dir = tmp_path / "models" / "antelopev2" / "antelopev2"
    nested_dir.mkdir(parents=True)
    nested_model = nested_dir / "scrfd_10g_bnkps.onnx"
    nested_model.write_bytes(b"model")

    captured: dict[str, object] = {}

    class FakeAnalyzer:
        def __init__(self, *, name, root, providers):
            captured["name"] = name
            captured["root"] = root
            captured["providers"] = providers
            assert (tmp_path / "models" / "antelopev2" / "scrfd_10g_bnkps.onnx").is_file()
            assert not nested_model.exists()

        def prepare(self, *, ctx_id, det_size) -> None:
            captured["ctx_id"] = ctx_id
            captured["det_size"] = det_size

    monkeypatch.setattr("hikbox_pictures.insightface_engine.DEFAULT_INSIGHTFACE_ROOT", tmp_path)
    monkeypatch.setattr("hikbox_pictures.insightface_engine.FaceAnalysis", FakeAnalyzer)

    engine = InsightFaceEngine.create(root=tmp_path)

    assert isinstance(engine, InsightFaceEngine)
    assert captured["name"] == "antelopev2"
    assert captured["root"] == str(tmp_path)


def test_create_wraps_blank_init_error_with_exception_type(monkeypatch) -> None:
    class FakeAnalyzer:
        def __init__(self, *, name, root, providers):
            raise AssertionError()

    monkeypatch.setattr("hikbox_pictures.insightface_engine.FaceAnalysis", FakeAnalyzer)

    with pytest.raises(InsightFaceInitError, match="AssertionError"):
        InsightFaceEngine.create()


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
    monkeypatch.setattr(
        "hikbox_pictures.insightface_engine.load_rgb_image",
        lambda _: np.array([[[1, 2, 3]]], dtype=np.uint8),
    )
    engine = InsightFaceEngine(analyzer=analyzer)

    faces = engine.detect_faces(image_path)

    assert faces == [
        DetectedFace(
            bbox=(20, 30, 40, 10),
            embedding=[0.1, 0.2, 0.3],
        )
    ]


def test_detect_faces_converts_rgb_to_bgr_before_inference(monkeypatch, tmp_path: Path) -> None:
    image_path = tmp_path / "sample.jpg"
    image_path.write_bytes(b"image")

    rgb_image = np.array([[[1, 2, 3], [4, 5, 6]]], dtype=np.uint8)
    analyzer_input: dict[str, np.ndarray] = {}

    def fake_get(image: np.ndarray):
        analyzer_input["image"] = image
        return []

    monkeypatch.setattr("hikbox_pictures.insightface_engine.load_rgb_image", lambda _: rgb_image)
    engine = InsightFaceEngine(analyzer=SimpleNamespace(get=fake_get))

    faces = engine.detect_faces(image_path)

    assert faces == []
    np.testing.assert_array_equal(analyzer_input["image"], rgb_image[:, :, ::-1])


def test_create_wraps_init_errors(monkeypatch) -> None:
    def raise_init_error(*_args, **_kwargs):
        raise RuntimeError("init failed")

    monkeypatch.setattr("hikbox_pictures.insightface_engine.FaceAnalysis", raise_init_error)

    with pytest.raises(InsightFaceInitError, match="init failed"):
        InsightFaceEngine.create()


def test_create_wraps_missing_insightface_dependency(monkeypatch) -> None:
    monkeypatch.setattr("hikbox_pictures.insightface_engine.FaceAnalysis", None)

    with pytest.raises(InsightFaceInitError, match="insightface 未安装或不可用"):
        InsightFaceEngine.create()


def test_detect_faces_wraps_inference_errors(monkeypatch, tmp_path: Path) -> None:
    image_path = tmp_path / "broken.jpg"
    image_path.write_bytes(b"image")

    monkeypatch.setattr(
        "hikbox_pictures.insightface_engine.load_rgb_image",
        lambda _: np.array([[[1, 2, 3]]], dtype=np.uint8),
    )
    analyzer = SimpleNamespace(get=lambda _image: (_ for _ in ()).throw(RuntimeError("infer failed")))
    engine = InsightFaceEngine(analyzer=analyzer)

    with pytest.raises(InsightFaceInferenceError, match="infer failed"):
        engine.detect_faces(image_path)
