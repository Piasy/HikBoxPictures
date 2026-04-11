from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

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


def test_main_writes_annotated_image_and_uses_engine_distance_semantics(monkeypatch, tmp_path, capsys) -> None:
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

    create_calls: list[dict[str, object]] = []
    min_distance_calls: list[tuple[object, object]] = []
    is_match_calls: list[float] = []

    ref_a_embeddings = [[0.1]]
    ref_b_embeddings = [[0.2]]

    def fake_min_distance(embedding, references):
        min_distance_calls.append((embedding, references))
        if references is ref_a_embeddings:
            return 0.12
        if references is ref_b_embeddings:
            return 0.57
        raise AssertionError("unexpected references")

    def fake_is_match(distance: float) -> bool:
        is_match_calls.append(distance)
        return distance <= 0.5

    fake_engine = SimpleNamespace(
        model_name="ArcFace",
        detector_backend="retinaface",
        distance_metric="cosine",
        align=True,
        distance_threshold=0.5,
        threshold_source="deepface-default",
        min_distance=fake_min_distance,
        is_match=fake_is_match,
    )

    def fake_create(**kwargs):
        create_calls.append(kwargs)
        return fake_engine

    monkeypatch.setattr(script.DeepFaceEngine, "create", fake_create)

    load_calls: list[tuple[Path, object]] = []
    monkeypatch.setattr(
        script,
        "load_reference_embeddings",
        lambda path, engine: load_calls.append((path, engine))
        or ((ref_a_embeddings, [ref_a]) if path == ref_a_dir else (ref_b_embeddings, [ref_b])),
    )
    monkeypatch.setattr(script, "build_reference_samples_from_embeddings", lambda paths, embeddings, *, engine: [object() for _ in paths])
    monkeypatch.setattr(
        script,
        "build_reference_template",
        lambda name, samples, **kwargs: SimpleNamespace(match_threshold=0.5, top_k=1, kept_samples=[object()], dropped_samples=[]),
    )
    results = iter(
        [
            SimpleNamespace(template_distance=0.12, centroid_distance=0.2, matched=True),
            SimpleNamespace(template_distance=0.57, centroid_distance=0.3, matched=False),
        ]
    )
    monkeypatch.setattr(script, "compute_template_match", lambda embedding, template, *, engine: next(results))
    monkeypatch.setattr(script, "iter_candidate_photos", lambda root: iter([CandidatePhoto(path=candidate_path)]))
    monkeypatch.setattr(script, "_load_candidate_face_encodings", lambda path, engine: ([(2, 20, 20, 4)], [[0.1]]))

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
    assert create_calls == [
        {
            "model_name": "ArcFace",
            "detector_backend": "retinaface",
            "distance_metric": "cosine",
            "align": True,
            "distance_threshold": None,
        }
    ]
    assert generated.is_file()
    assert "运行配置" in output
    assert "model_name=ArcFace" in output
    assert "detector_backend=retinaface" in output
    assert "distance_metric=cosine" in output
    assert "align=True" in output
    assert "distance_threshold=0.50" in output
    assert "threshold_source=deepface-default" in output
    assert f"标注图: {generated}" in output
    assert "template_dist_a=0.12" in output
    assert "template_dist_b=0.57" in output
    assert "match_a=Y" in output
    assert "match_b=N" in output
    assert load_calls == [(ref_a_dir, fake_engine), (ref_b_dir, fake_engine)]

    with Image.open(generated) as image:
        assert image.size == (24, 24)


def test_main_prints_template_distances_and_joint_distance(monkeypatch, tmp_path, capsys) -> None:
    script = _load_script_module()
    input_dir = tmp_path / "input"
    ref_a_dir = tmp_path / "ref-a"
    ref_b_dir = tmp_path / "ref-b"
    input_dir.mkdir()
    ref_a_dir.mkdir()
    ref_b_dir.mkdir()

    candidate_path = input_dir / "candidate.jpg"
    candidate_path.write_bytes(b"img")

    fake_engine = SimpleNamespace(
        model_name="ArcFace",
        detector_backend="retinaface",
        distance_metric="cosine",
        align=True,
        distance_threshold=0.4,
        threshold_source="explicit",
    )
    monkeypatch.setattr(script.DeepFaceEngine, "create", lambda **kwargs: fake_engine)
    monkeypatch.setattr(script, "iter_candidate_photos", lambda root: iter([CandidatePhoto(path=candidate_path)]))
    monkeypatch.setattr(script, "_load_candidate_face_encodings", lambda path, engine: ([(0, 10, 10, 0), (0, 20, 20, 0)], [[1.0], [2.0]]))

    fake_template = SimpleNamespace(
        kept_samples=[SimpleNamespace(path=ref_a_dir / "a.jpg")],
        dropped_samples=[],
        match_threshold=0.32,
        top_k=1,
    )
    monkeypatch.setattr(script, "build_reference_template", lambda *args, **kwargs: fake_template)
    monkeypatch.setattr(script, "build_reference_samples_from_embeddings", lambda paths, embeddings, *, engine: [object()])
    monkeypatch.setattr(script, "load_reference_embeddings", lambda path, engine: ([[0.1]], [path / "sample.jpg"]))

    results = iter(
        [
            SimpleNamespace(template_distance=0.12, centroid_distance=0.13, matched=True),
            SimpleNamespace(template_distance=0.44, centroid_distance=0.40, matched=False),
            SimpleNamespace(template_distance=0.45, centroid_distance=0.41, matched=False),
            SimpleNamespace(template_distance=0.11, centroid_distance=0.14, matched=True),
        ]
    )
    monkeypatch.setattr(script, "compute_template_match", lambda embedding, template, *, engine: next(results))

    exit_code = script.main(
        [
            "--input",
            str(input_dir),
            "--ref-a-dir",
            str(ref_a_dir),
            "--ref-b-dir",
            str(ref_b_dir),
            "--distance-threshold-a",
            "0.32",
            "--distance-threshold-b",
            "0.35",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "template_threshold_a=0.32" in output
    assert "template_dist_a=0.12" in output
    assert "centroid_dist_b=0.41" in output
    assert "joint_distance=0.12" in output


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
        template_distances_a=[0.1111],
        template_distances_b=[0.2222],
        centroid_distances_a=[0.3333],
        centroid_distances_b=[0.4444],
    )

    assert loaded_sizes == [30]


def test_draw_label_uses_blue_text_without_background() -> None:
    script = _load_script_module()

    text_calls: list[dict[str, object]] = []

    class DummyDraw:
        def rectangle(self, *args, **kwargs):
            raise AssertionError("不应绘制标签背景矩形")

        def text(self, position, text, *, fill=None, font=None):
            text_calls.append({"position": position, "text": text, "fill": fill, "font": font})

        @staticmethod
        def textbbox(_position, text, font=None):
            return (0, 0, len(text) * 10, 12)

    lines = ["face[0]", "A 0.12", "B 0.57"]
    font = object()

    script._draw_label(
        DummyDraw(),
        font=font,
        left=10,
        top=30,
        lines=lines,
        image_width=200,
        image_height=200,
    )

    assert [call["text"] for call in text_calls] == lines
    assert [call["fill"] for call in text_calls] == [script.ANNOTATION_TEXT_COLOR] * len(lines)


def test_write_annotated_image_uses_exif_transposed_pixels(tmp_path) -> None:
    script = _load_script_module()

    candidate_path = tmp_path / "candidate.png"
    annotated_dir = tmp_path / "annotated"

    image = Image.new("RGB", (3, 2))
    image.putdata(
        [
            (255, 0, 0),
            (0, 255, 0),
            (0, 0, 255),
            (255, 255, 0),
            (255, 0, 255),
            (0, 255, 255),
        ]
    )
    exif = Image.Exif()
    exif[274] = 6
    image.save(candidate_path, exif=exif)

    output_path = script._write_annotated_image(
        candidate_path,
        input_root=tmp_path,
        annotated_dir=annotated_dir,
        locations=[],
        template_distances_a=[],
        template_distances_b=[],
        centroid_distances_a=[],
        centroid_distances_b=[],
    )

    with Image.open(output_path) as annotated:
        assert annotated.size == (2, 3)
        assert annotated.getpixel((0, 0)) == (255, 255, 0)
        assert annotated.getpixel((1, 0)) == (255, 0, 0)


def test_main_processes_all_candidates_without_skip_logic(monkeypatch, tmp_path, capsys) -> None:
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

    fake_engine = SimpleNamespace(
        model_name="ArcFace",
        detector_backend="retinaface",
        distance_metric="cosine",
        align=True,
        distance_threshold=0.5,
        threshold_source="deepface-default",
        min_distance=lambda _embedding, _references: 0.12,
        is_match=lambda _distance: True,
    )
    monkeypatch.setattr(script.DeepFaceEngine, "create", lambda **_kwargs: fake_engine)

    monkeypatch.setattr(
        script,
        "load_reference_embeddings",
        lambda path, engine: (([[0.1]], [ref_a]) if path == ref_a_dir else ([[0.2]], [ref_b])),
    )
    monkeypatch.setattr(script, "build_reference_samples_from_embeddings", lambda paths, embeddings, *, engine: [object() for _ in paths])
    monkeypatch.setattr(
        script,
        "build_reference_template",
        lambda name, samples, **kwargs: SimpleNamespace(match_threshold=0.5, top_k=1, kept_samples=[object()], dropped_samples=[]),
    )
    monkeypatch.setattr(
        script,
        "compute_template_match",
        lambda embedding, template, *, engine: SimpleNamespace(template_distance=0.12, centroid_distance=0.2, matched=True),
    )
    monkeypatch.setattr(
        script,
        "iter_candidate_photos",
        lambda root: iter([CandidatePhoto(path=candidate_path), CandidatePhoto(path=skipped_path)]),
    )
    monkeypatch.setattr(script, "_load_candidate_face_encodings", lambda path, engine: ([(2, 20, 20, 4)], [[0.1]]))

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
    assert f"文件: {skipped_path}" in output


def test_main_passes_custom_engine_args_and_prints_explicit_threshold(monkeypatch, tmp_path, capsys) -> None:
    script = _load_script_module()

    input_dir = tmp_path / "input"
    ref_a_dir = tmp_path / "ref-a"
    ref_b_dir = tmp_path / "ref-b"
    input_dir.mkdir()
    ref_a_dir.mkdir()
    ref_b_dir.mkdir()

    ref_a = ref_a_dir / "a.jpg"
    ref_b = ref_b_dir / "b.jpg"
    Image.new("RGB", (24, 24), color="white").save(ref_a)
    Image.new("RGB", (24, 24), color="white").save(ref_b)

    create_calls: list[dict[str, object]] = []
    fake_engine = SimpleNamespace(
        model_name="Facenet",
        detector_backend="mtcnn",
        distance_metric="euclidean_l2",
        align=False,
        distance_threshold=0.42,
        threshold_source="explicit",
        min_distance=lambda _embedding, _references: 0.1,
        is_match=lambda _distance: True,
    )

    def fake_create(**kwargs):
        create_calls.append(kwargs)
        return fake_engine

    monkeypatch.setattr(script.DeepFaceEngine, "create", fake_create)
    monkeypatch.setattr(
        script,
        "load_reference_embeddings",
        lambda path, engine: (([[0.1]], [ref_a]) if path == ref_a_dir else ([[0.2]], [ref_b])),
    )
    monkeypatch.setattr(script, "build_reference_samples_from_embeddings", lambda paths, embeddings, *, engine: [object() for _ in paths])
    monkeypatch.setattr(
        script,
        "build_reference_template",
        lambda name, samples, **kwargs: SimpleNamespace(match_threshold=0.42, top_k=1, kept_samples=[object()], dropped_samples=[]),
    )
    monkeypatch.setattr(
        script,
        "compute_template_match",
        lambda embedding, template, *, engine: SimpleNamespace(template_distance=0.1, centroid_distance=0.2, matched=True),
    )
    monkeypatch.setattr(script, "iter_candidate_photos", lambda root: iter([]))

    exit_code = script.main(
        [
            "--input",
            str(input_dir),
            "--ref-a-dir",
            str(ref_a_dir),
            "--ref-b-dir",
            str(ref_b_dir),
            "--model-name",
            "Facenet",
            "--detector-backend",
            "mtcnn",
            "--distance-metric",
            "euclidean_l2",
            "--distance-threshold",
            "0.42",
            "--no-align",
        ]
    )

    output = capsys.readouterr().out

    assert exit_code == 0
    assert create_calls == [
        {
            "model_name": "Facenet",
            "detector_backend": "mtcnn",
            "distance_metric": "euclidean_l2",
            "align": False,
            "distance_threshold": 0.42,
        }
    ]
    assert "model_name=Facenet" in output
    assert "detector_backend=mtcnn" in output
    assert "distance_metric=euclidean_l2" in output
    assert "align=False" in output
    assert "distance_threshold=0.42" in output
    assert "threshold_source=explicit" in output


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


def test_main_reports_non_directory_annotated_path(monkeypatch, tmp_path, capsys) -> None:
    script = _load_script_module()

    input_dir = tmp_path / "input"
    ref_a_dir = tmp_path / "ref-a"
    ref_b_dir = tmp_path / "ref-b"
    annotated_file = tmp_path / "annotated.txt"
    input_dir.mkdir()
    ref_a_dir.mkdir()
    ref_b_dir.mkdir()
    annotated_file.write_text("not a directory")

    called = {"create": False}

    def fake_create(**kwargs):
        called["create"] = True
        raise AssertionError("不应初始化引擎")

    monkeypatch.setattr(script.DeepFaceEngine, "create", fake_create)

    exit_code = script.main(
        [
            "--input",
            str(input_dir),
            "--ref-a-dir",
            str(ref_a_dir),
            "--ref-b-dir",
            str(ref_b_dir),
            "--annotated-dir",
            str(annotated_file),
        ]
    )

    error_output = capsys.readouterr().err

    assert exit_code == 2
    assert f"路径不是目录: {annotated_file}" in error_output
    assert called["create"] is False


def test_main_continues_when_single_annotated_write_fails(monkeypatch, tmp_path, capsys) -> None:
    script = _load_script_module()

    input_dir = tmp_path / "input"
    annotated_dir = tmp_path / "annotated"
    ref_a_dir = tmp_path / "ref-a"
    ref_b_dir = tmp_path / "ref-b"
    input_dir.mkdir()
    ref_a_dir.mkdir()
    ref_b_dir.mkdir()

    candidate_a = input_dir / "a.jpg"
    candidate_b = input_dir / "b.jpg"
    ref_a = ref_a_dir / "ref-a.jpg"
    ref_b = ref_b_dir / "ref-b.jpg"
    for path in (candidate_a, candidate_b, ref_a, ref_b):
        Image.new("RGB", (24, 24), color="white").save(path)

    fake_engine = SimpleNamespace(
        model_name="ArcFace",
        detector_backend="retinaface",
        distance_metric="cosine",
        align=True,
        distance_threshold=0.5,
        threshold_source="deepface-default",
        min_distance=lambda _embedding, _references: 0.12,
        is_match=lambda _distance: True,
    )
    monkeypatch.setattr(script.DeepFaceEngine, "create", lambda **_kwargs: fake_engine)
    monkeypatch.setattr(
        script,
        "load_reference_embeddings",
        lambda path, engine: (([[0.1]], [ref_a]) if path == ref_a_dir else ([[0.2]], [ref_b])),
    )
    monkeypatch.setattr(script, "build_reference_samples_from_embeddings", lambda paths, embeddings, *, engine: [object() for _ in paths])
    monkeypatch.setattr(
        script,
        "build_reference_template",
        lambda name, samples, **kwargs: SimpleNamespace(match_threshold=0.5, top_k=1, kept_samples=[object()], dropped_samples=[]),
    )
    monkeypatch.setattr(
        script,
        "compute_template_match",
        lambda embedding, template, *, engine: SimpleNamespace(template_distance=0.12, centroid_distance=0.2, matched=True),
    )
    monkeypatch.setattr(
        script,
        "iter_candidate_photos",
        lambda root: iter([CandidatePhoto(path=candidate_a), CandidatePhoto(path=candidate_b)]),
    )
    monkeypatch.setattr(
        script,
        "_load_candidate_face_encodings",
        lambda path, engine: ([(2, 20, 20, 4)], [[0.1]]),
    )

    def fake_write_annotated_image(
        candidate_path: Path,
        *,
        input_root: Path,
        annotated_dir: Path,
        locations,
        template_distances_a,
        template_distances_b,
        centroid_distances_a,
        centroid_distances_b,
    ) -> Path:
        if candidate_path == candidate_a:
            raise RuntimeError("mock write failed")
        return annotated_dir / f"{candidate_path.stem}__annotated.png"

    monkeypatch.setattr(script, "_write_annotated_image", fake_write_annotated_image)

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

    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"文件: {candidate_a}" in captured.out
    assert f"文件: {candidate_b}" in captured.out
    assert f"  标注图: {annotated_dir / 'b__annotated.png'}" in captured.out
    assert f"标注失败: {candidate_a} -> mock write failed" in captured.err
