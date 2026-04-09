from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image


SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "extract_faces.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("extract_faces_script", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_main_uses_insightface_and_keeps_output_rules(monkeypatch, tmp_path, capsys) -> None:
    script = _load_script_module()

    input_dir = tmp_path / "input"
    source_dir = input_dir / "nested"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    source_dir.mkdir(parents=True)

    source_path = source_dir / "sample.jpg"
    Image.new("RGB", (20, 20), color=(120, 130, 140)).save(source_path)

    fake_image = np.full((20, 20, 3), 200, dtype=np.uint8)

    class FakeEngine:
        def __init__(self) -> None:
            self.detect_calls: list[Path] = []

        def detect_faces(self, image_path: Path):
            self.detect_calls.append(image_path)
            return [
                SimpleNamespace(bbox=(2, 12, 14, 0)),
                SimpleNamespace(bbox=(4, 18, 16, 6)),
            ]

    fake_engine = FakeEngine()
    monkeypatch.setattr(script.InsightFaceEngine, "create", lambda: fake_engine)
    monkeypatch.setattr(script, "load_rgb_image", lambda _path: fake_image)

    exit_code = script.main([
        "--input",
        str(input_dir),
        "--output",
        str(output_dir),
        "--size",
        "64",
    ])

    output = capsys.readouterr().out
    first_output = output_dir / "nested" / "sample__face_01.png"
    second_output = output_dir / "nested" / "sample__face_02.png"

    assert exit_code == 0
    assert fake_engine.detect_calls == [source_path]
    assert first_output.is_file()
    assert second_output.is_file()
    assert "扫描图片数: 1" in output
    assert "输出人脸数: 2" in output
    assert "无人脸图片数: 0" in output
    assert "解码失败数: 0" in output

    with Image.open(first_output) as image:
        assert image.size == (64, 64)


def test_crop_with_edge_padding_keeps_black_border() -> None:
    script = _load_script_module()

    image = np.full((4, 4, 3), 255, dtype=np.uint8)
    crop = script._crop_with_edge_padding(image, (-1, 3, 3, -2))

    assert crop.shape == (4, 5, 3)
    assert crop[0, 0].tolist() == [0, 0, 0]
    assert crop[-1, -1].tolist() == [255, 255, 255]


def test_iter_image_paths_skips_output_directory_by_default(tmp_path) -> None:
    script = _load_script_module()

    input_dir = tmp_path / "input"
    output_dir = input_dir / "output"
    nested_dir = input_dir / "nested"
    input_dir.mkdir()
    output_dir.mkdir()
    nested_dir.mkdir()

    kept_file = nested_dir / "keep.jpg"
    skipped_file = output_dir / "skip.jpg"
    kept_file.write_bytes(b"ok")
    skipped_file.write_bytes(b"skip")

    paths = list(script._iter_image_paths(input_dir, output_dir))

    assert paths == [kept_file]


def test_main_counts_decode_failures_and_no_face(monkeypatch, tmp_path, capsys) -> None:
    script = _load_script_module()

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()

    noface = input_dir / "noface.jpg"
    broken = input_dir / "broken.jpg"
    Image.new("RGB", (12, 12), color=(255, 255, 255)).save(noface)
    Image.new("RGB", (12, 12), color=(255, 255, 255)).save(broken)

    class FakeEngine:
        def __init__(self) -> None:
            self.detect_calls: list[Path] = []

        def detect_faces(self, image_path: Path):
            self.detect_calls.append(image_path)
            return []

    fake_engine = FakeEngine()
    monkeypatch.setattr(script.InsightFaceEngine, "create", lambda: fake_engine)

    def fake_load(path: Path):
        if path == broken:
            raise RuntimeError("decode error")
        return np.full((12, 12, 3), 120, dtype=np.uint8)

    monkeypatch.setattr(script, "load_rgb_image", fake_load)

    exit_code = script.main([
        "--input",
        str(input_dir),
        "--output",
        str(output_dir),
    ])

    stdout = capsys.readouterr().out

    assert exit_code == 0
    assert fake_engine.detect_calls == [noface]
    assert "扫描图片数: 2" in stdout
    assert "输出人脸数: 0" in stdout
    assert "无人脸图片数: 1" in stdout
    assert "解码失败数: 1" in stdout
