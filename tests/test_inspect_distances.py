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
    ref_a_dir = tmp_path / "ref-a"
    ref_b_dir = tmp_path / "ref-b"
    input_dir.mkdir()
    ref_a_dir.mkdir()
    ref_b_dir.mkdir()

    candidate_path = input_dir / "candidate.jpg"
    ref_a = ref_a_dir / "a.jpg"
    ref_b = ref_b_dir / "b.jpg"
    Image.new("RGB", (24, 24), color="white").save(candidate_path)
    Image.new("RGB", (24, 24), color="white").save(ref_a)
    Image.new("RGB", (24, 24), color="white").save(ref_b)

    fake_engine = object()

    monkeypatch.setattr(script.InsightFaceEngine, "create", lambda: fake_engine)

    load_calls: list[tuple[Path, object]] = []

    monkeypatch.setattr(
        script,
        "load_reference_embeddings",
        lambda path, engine: load_calls.append((path, engine))
        or (([[0.1]], [ref_a]) if path == ref_a_dir else ([[0.2]], [ref_b])),
    )
    monkeypatch.setattr(
        script,
        "iter_candidate_photos",
        lambda root: iter([CandidatePhoto(path=candidate_path)]),
    )

    candidate_detect_engines: list[object] = []

    monkeypatch.setattr(
        script,
        "_load_candidate_face_encodings",
        lambda path, engine: candidate_detect_engines.append(engine) or ([(2, 20, 20, 4)], [[0.1]]),
    )

    def fake_compute_min_distances(_encodings, ref_embeddings):
        return [0.1234] if ref_embeddings == [[0.1]] else [0.5678]

    monkeypatch.setattr(script, "compute_min_distances", fake_compute_min_distances, raising=False)
    monkeypatch.setattr(script, "DEFAULT_DISTANCE_THRESHOLD", 0.5, raising=False)

    exit_code = script.main(
        [
            "--input",
            str(input_dir),
            "--ref-a-dir",
            str(ref_a_dir),
            "--ref-b-dir",
            str(ref_b_dir),
            "--annotated-dir",
            str(annotated_dir),
        ]
    )

    output = capsys.readouterr().out
    generated = annotated_dir / "candidate__annotated.png"

    assert exit_code == 0
    assert generated.is_file()
    assert "标注输出目录" in output
    assert f"匹配阈值: {script.DEFAULT_DISTANCE_THRESHOLD:.2f}" in output
    assert f"标注图: {generated}" in output
    assert "dist_a=0.1234" in output
    assert "dist_b=0.5678" in output
    assert "match_a" not in output
    assert "match_b" not in output
    assert load_calls == [(ref_a_dir, fake_engine), (ref_b_dir, fake_engine)]
    assert candidate_detect_engines == [fake_engine]

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
    ref_a_dir = tmp_path / "ref-a"
    ref_b_dir = tmp_path / "ref-b"
    input_dir.mkdir()
    output_dir.mkdir()
    ref_a_dir.mkdir()
    ref_b_dir.mkdir()

    candidate_path = input_dir / "candidate.jpg"
    skipped_path = output_dir / "skipped.jpg"
    ref_a = ref_a_dir / "a.jpg"
    ref_b = ref_b_dir / "b.jpg"
    for path in (candidate_path, skipped_path, ref_a, ref_b):
        Image.new("RGB", (24, 24), color="white").save(path)

    fake_engine = object()
    monkeypatch.setattr(script.InsightFaceEngine, "create", lambda: fake_engine)

    monkeypatch.setattr(
        script,
        "load_reference_embeddings",
        lambda path, engine: (([[0.1]], [ref_a]) if path == ref_a_dir else ([[0.2]], [ref_b])),
    )
    monkeypatch.setattr(
        script,
        "iter_candidate_photos",
        lambda root: iter([CandidatePhoto(path=candidate_path), CandidatePhoto(path=skipped_path)]),
    )
    monkeypatch.setattr(
        script,
        "_load_candidate_face_encodings",
        lambda path, engine: ([(2, 20, 20, 4)], [[0.1]]),
    )
    monkeypatch.setattr(script, "compute_min_distances", lambda encodings, refs: [0.1234], raising=False)
    monkeypatch.setattr(script, "DEFAULT_DISTANCE_THRESHOLD", 0.5, raising=False)

    exit_code = script.main(
        [
            "--input",
            str(input_dir),
            "--ref-a-dir",
            str(ref_a_dir),
            "--ref-b-dir",
            str(ref_b_dir),
        ]
    )

    output = capsys.readouterr().out

    assert exit_code == 0
    assert f"文件: {candidate_path}" in output
    assert f"文件: {skipped_path}" not in output


def test_main_prints_effective_tolerance_from_argument(monkeypatch, tmp_path, capsys) -> None:
    script = _load_script_module()

    input_dir = tmp_path / "input"
    ref_a_dir = tmp_path / "ref-a"
    ref_b_dir = tmp_path / "ref-b"
    input_dir.mkdir()
    ref_a_dir.mkdir()
    ref_b_dir.mkdir()

    candidate_path = input_dir / "candidate.jpg"
    ref_a = ref_a_dir / "a.jpg"
    ref_b = ref_b_dir / "b.jpg"
    for path in (candidate_path, ref_a, ref_b):
        Image.new("RGB", (24, 24), color="white").save(path)

    fake_engine = object()
    monkeypatch.setattr(script.InsightFaceEngine, "create", lambda: fake_engine)
    monkeypatch.setattr(
        script,
        "load_reference_embeddings",
        lambda path, engine: (([[0.1]], [ref_a]) if path == ref_a_dir else ([[0.2]], [ref_b])),
    )
    monkeypatch.setattr(script, "iter_candidate_photos", lambda root: iter([CandidatePhoto(path=candidate_path)]))
    monkeypatch.setattr(
        script,
        "_load_candidate_face_encodings",
        lambda path, engine: ([(2, 20, 20, 4)], [[0.1]]),
    )
    monkeypatch.setattr(script, "compute_min_distances", lambda encodings, refs: [0.1234], raising=False)

    exit_code = script.main(
        [
            "--input",
            str(input_dir),
            "--ref-a-dir",
            str(ref_a_dir),
            "--ref-b-dir",
            str(ref_b_dir),
            "--tolerance",
            "0.42",
        ]
    )

    output = capsys.readouterr().out

    assert exit_code == 0
    assert "匹配阈值: 0.42" in output


def test_main_reports_non_directory_reference_path(monkeypatch, tmp_path, capsys) -> None:
    script = _load_script_module()

    input_dir = tmp_path / "input"
    ref_a_dir = tmp_path / "ref-a"
    ref_b_file = tmp_path / "ref-b.jpg"
    input_dir.mkdir()
    ref_a_dir.mkdir()
    ref_b_file.write_text("not a directory")

    exit_code = script.main(
        [
            "--input",
            str(input_dir),
            "--ref-a-dir",
            str(ref_a_dir),
            "--ref-b-dir",
            str(ref_b_file),
        ]
    )

    error_output = capsys.readouterr().err

    assert exit_code == 2
    assert f"路径不是目录: {ref_b_file}" in error_output
