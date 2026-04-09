from __future__ import annotations

import importlib.util
from pathlib import Path

from PIL import Image

from hikbox_pictures.models import CandidatePhoto


SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "inspect_distances.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("inspect_distances_script", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_main_writes_annotated_image_when_directory_is_provided(monkeypatch, tmp_path, capsys) -> None:
    script = _load_script_module()

    input_dir = tmp_path / "input"
    annotated_dir = tmp_path / "annotated"
    input_dir.mkdir()

    candidate_path = input_dir / "candidate.jpg"
    ref_a = tmp_path / "ref-a.jpg"
    ref_b = tmp_path / "ref-b.jpg"
    Image.new("RGB", (24, 24), color="white").save(candidate_path)
    Image.new("RGB", (24, 24), color="white").save(ref_a)
    Image.new("RGB", (24, 24), color="white").save(ref_b)

    ref_a_encoding = object()
    ref_b_encoding = object()

    monkeypatch.setattr(
        script,
        "load_reference_encoding",
        lambda path: ref_a_encoding if path == ref_a else ref_b_encoding,
    )
    monkeypatch.setattr(
        script,
        "iter_candidate_photos",
        lambda root: iter([CandidatePhoto(path=candidate_path)]),
    )
    monkeypatch.setattr(
        script,
        "_load_candidate_face_encodings",
        lambda path: ([(2, 20, 20, 4)], [[0.1]]),
    )

    def fake_face_distance(encodings, ref_encoding):
        return [0.1234] if ref_encoding is ref_a_encoding else [0.5678]

    monkeypatch.setattr(script.face_recognition, "face_distance", fake_face_distance)

    exit_code = script.main(
        [
            "--input",
            str(input_dir),
            "--ref-a",
            str(ref_a),
            "--ref-b",
            str(ref_b),
            "--annotated-dir",
            str(annotated_dir),
        ]
    )

    output = capsys.readouterr().out
    generated = annotated_dir / "candidate__annotated.png"

    assert exit_code == 0
    assert generated.is_file()
    assert "标注输出目录" in output
    assert f"标注图: {generated}" in output
    assert "match_a" not in output
    assert "match_b" not in output

    with Image.open(generated) as image:
        assert image.size == (24, 24)


def test_write_annotated_image_uses_three_times_larger_font(monkeypatch, tmp_path) -> None:
    script = _load_script_module()

    candidate_path = tmp_path / "candidate.jpg"
    input_dir = tmp_path
    annotated_dir = tmp_path / "annotated"
    Image.new("RGB", (120, 80), color="white").save(candidate_path)

    loaded_sizes: list[int] = []

    class DummyFont:
        pass

    monkeypatch.setattr(script.ImageFont, "load_default", lambda size=None: loaded_sizes.append(size) or DummyFont())

    def fake_textbbox(_position, text, font=None):
        return (0, 0, len(text) * 10, 12)

    class DummyDraw:
        def rectangle(self, *args, **kwargs):
            return None

        def text(self, *args, **kwargs):
            return None

        textbbox = staticmethod(fake_textbbox)

    monkeypatch.setattr(script.ImageDraw, "Draw", lambda image: DummyDraw())

    script._write_annotated_image(
        candidate_path,
        input_root=input_dir,
        annotated_dir=annotated_dir,
        locations=[(10, 50, 60, 5)],
        distances_a=[0.1111],
        distances_b=[0.2222],
    )

    assert loaded_sizes == [30]


def test_main_skips_output_directory_by_default(monkeypatch, tmp_path, capsys) -> None:
    script = _load_script_module()

    input_dir = tmp_path / "input"
    output_dir = input_dir / "output"
    input_dir.mkdir()
    output_dir.mkdir()

    candidate_path = input_dir / "candidate.jpg"
    skipped_path = output_dir / "skipped.jpg"
    ref_a = tmp_path / "ref-a.jpg"
    ref_b = tmp_path / "ref-b.jpg"
    for path in (candidate_path, skipped_path, ref_a, ref_b):
        Image.new("RGB", (24, 24), color="white").save(path)

    ref_a_encoding = object()
    ref_b_encoding = object()

    monkeypatch.setattr(
        script,
        "load_reference_encoding",
        lambda path: ref_a_encoding if path == ref_a else ref_b_encoding,
    )
    monkeypatch.setattr(
        script,
        "iter_candidate_photos",
        lambda root: iter([CandidatePhoto(path=candidate_path), CandidatePhoto(path=skipped_path)]),
    )
    monkeypatch.setattr(
        script,
        "_load_candidate_face_encodings",
        lambda path: ([(2, 20, 20, 4)], [[0.1]]),
    )
    monkeypatch.setattr(script.face_recognition, "face_distance", lambda encodings, ref_encoding: [0.1234])

    exit_code = script.main(
        [
            "--input",
            str(input_dir),
            "--ref-a",
            str(ref_a),
            "--ref-b",
            str(ref_b),
        ]
    )

    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"文件: {candidate_path}" in output
    assert f"文件: {skipped_path}" not in output
