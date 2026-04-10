import ast
from datetime import datetime
from importlib.util import resolve_name
from pathlib import Path

import pytest

from hikbox_pictures import cli as cli_module
from hikbox_pictures.cli import main
from hikbox_pictures.matcher import CandidateDecodeError
from hikbox_pictures.models import CandidatePhoto, MatchBucket, PhotoEvaluation


REPO_ROOT = Path(__file__).resolve().parent.parent


def _is_legacy_insightface_module(module_name: str) -> bool:
    return (
        module_name == "insightface"
        or module_name.startswith("insightface.")
        or module_name == "hikbox_pictures.insightface_engine"
    )


def _is_import_module_call(node: ast.Call) -> bool:
    function = node.func
    if isinstance(function, ast.Name):
        return function.id in {"__import__", "import_module"}
    if isinstance(function, ast.Attribute):
        return function.attr == "import_module"
    return False


def _legacy_import_from_matches(node: ast.ImportFrom) -> list[str]:
    module_name = node.module or ""
    if _is_legacy_insightface_module(module_name):
        return [module_name]

    matches: list[str] = []
    if module_name == "hikbox_pictures":
        for alias in node.names:
            if alias.name == "insightface_engine":
                matches.append("hikbox_pictures.insightface_engine")
    if node.level > 0:
        if module_name == "insightface_engine":
            matches.append("insightface_engine")
        elif module_name == "":
            for alias in node.names:
                if alias.name == "insightface_engine":
                    matches.append("insightface_engine")
    return matches


def _import_target_from_call(node: ast.Call) -> str | None:
    if node.args:
        first_arg = node.args[0]
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            return first_arg.value
    for keyword in node.keywords:
        if keyword.arg != "name":
            continue
        if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            return keyword.value.value
    return None


def _package_from_call(node: ast.Call) -> str | None:
    for keyword in node.keywords:
        if keyword.arg != "package":
            continue
        if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            return keyword.value.value
    return None


def _normalize_import_target(import_target: str, *, package: str | None = None) -> str | None:
    if _is_legacy_insightface_module(import_target):
        return import_target

    if package is None or not import_target.startswith("."):
        return None

    try:
        resolved = resolve_name(import_target, package)
    except ImportError:
        return None

    if _is_legacy_insightface_module(resolved):
        return resolved
    return None


def _find_legacy_insightface_imports(paths: list[Path]) -> list[tuple[Path, int, str]]:
    matches: list[tuple[Path, int, str]] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_legacy_insightface_module(alias.name):
                        matches.append((path, node.lineno, alias.name))
            elif isinstance(node, ast.ImportFrom):
                for module_name in _legacy_import_from_matches(node):
                    matches.append((path, node.lineno, module_name))
            elif isinstance(node, ast.Call) and _is_import_module_call(node):
                import_target = _import_target_from_call(node)
                if import_target is None:
                    continue
                normalized_target = _normalize_import_target(
                    import_target,
                    package=_package_from_call(node),
                )
                if normalized_target is not None:
                    matches.append((path, node.lineno, normalized_target))
    return matches


def _build_argv(input_dir: Path, ref_a_dir: Path, ref_b_dir: Path, output_dir: Path) -> list[str]:
    return [
        "--input",
        str(input_dir),
        "--ref-a-dir",
        str(ref_a_dir),
        "--ref-b-dir",
        str(ref_b_dir),
        "--output",
        str(output_dir),
    ]


def test_main_returns_zero_when_called_without_argv() -> None:
    assert main() == 0


def test_find_legacy_insightface_imports_detects_static_and_dynamic_imports(tmp_path: Path) -> None:
    sample = tmp_path / "legacy_sample.py"
    sample.write_text(
        "\n".join(
            [
                "import insightface",
                "from insightface.app import FaceAnalysis",
                "from hikbox_pictures import insightface_engine",
                "from hikbox_pictures.insightface_engine import InsightFaceEngine",
                "from . import insightface_engine",
                "from .insightface_engine import InsightFaceEngine",
                "import importlib",
                'importlib.import_module("hikbox_pictures.insightface_engine")',
                'importlib.import_module(name="hikbox_pictures.insightface_engine")',
                'importlib.import_module(".insightface_engine", package="hikbox_pictures")',
            ]
        ),
        encoding="utf-8",
    )

    matches = _find_legacy_insightface_imports([sample])

    assert [(lineno, module_name) for _, lineno, module_name in matches] == [
        (1, "insightface"),
        (2, "insightface.app"),
        (3, "hikbox_pictures.insightface_engine"),
        (4, "hikbox_pictures.insightface_engine"),
        (5, "insightface_engine"),
        (6, "insightface_engine"),
        (8, "hikbox_pictures.insightface_engine"),
        (9, "hikbox_pictures.insightface_engine"),
        (10, "hikbox_pictures.insightface_engine"),
    ]


def test_find_legacy_insightface_imports_ignores_plain_strings(tmp_path: Path) -> None:
    sample = tmp_path / "clean_sample.py"
    sample.write_text(
        "\n".join(
            [
                'message = "insightface should not appear in README"',
                'assert "insightface" not in message',
            ]
        ),
        encoding="utf-8",
    )

    assert _find_legacy_insightface_imports([sample]) == []


def test_codebase_no_longer_references_insightface_imports() -> None:
    python_files = [
        *sorted((REPO_ROOT / "src").rglob("*.py")),
        *sorted((REPO_ROOT / "scripts").rglob("*.py")),
        *sorted((REPO_ROOT / "tests").rglob("*.py")),
    ]

    assert _find_legacy_insightface_imports(python_files) == []


def test_main_exports_only_hits_and_prints_summary(monkeypatch, tmp_path, capsys) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    ref_a_dir = tmp_path / "ref-a"
    ref_b_dir = tmp_path / "ref-b"
    input_dir.mkdir()
    output_dir.mkdir()
    ref_a_dir.mkdir()
    ref_b_dir.mkdir()

    candidate_hit = CandidatePhoto(path=input_dir / "pair.jpg")
    candidate_miss = CandidatePhoto(path=input_dir / "miss.jpg")
    candidate_hit.path.write_bytes(b"pair")
    candidate_miss.path.write_bytes(b"miss")

    fake_engine = object()
    engine_create_calls = []

    def fake_create_engine(**kwargs):
        engine_create_calls.append(kwargs)
        return fake_engine

    monkeypatch.setattr(cli_module.DeepFaceEngine, "create", fake_create_engine)

    def fake_load_reference_embeddings(path, engine):
        assert engine is fake_engine
        if path == ref_a_dir:
            return ([[0.1], [0.11]], [path / "a1.jpg", path / "a2.jpg"])
        return ([[0.2]], [path / "b1.jpg"])

    monkeypatch.setattr("hikbox_pictures.cli.load_reference_embeddings", fake_load_reference_embeddings)
    monkeypatch.setattr(
        "hikbox_pictures.cli.iter_candidate_photos",
        lambda path: iter([candidate_hit, candidate_miss]),
    )
    evaluations = iter(
        [
            PhotoEvaluation(candidate=candidate_hit, detected_face_count=2, bucket=MatchBucket.ONLY_TWO),
            PhotoEvaluation(candidate=candidate_miss, detected_face_count=1, bucket=None),
        ]
    )
    seen_reference_args = []

    def fake_evaluate(candidate, person_a_embeddings, person_b_embeddings, *, engine):
        assert engine is fake_engine
        seen_reference_args.append((person_a_embeddings, person_b_embeddings))
        return next(evaluations)

    monkeypatch.setattr("hikbox_pictures.cli.evaluate_candidate_photo", fake_evaluate)
    monkeypatch.setattr(
        "hikbox_pictures.cli.resolve_capture_datetime",
        lambda path: datetime(2025, 4, 3, 10, 30),
    )
    exported = []
    monkeypatch.setattr(
        "hikbox_pictures.cli.export_match",
        lambda evaluation, output_root, capture_datetime: exported.append(evaluation.candidate.path),
    )

    exit_code = main(_build_argv(input_dir, ref_a_dir, ref_b_dir, output_dir))

    stdout = capsys.readouterr().out
    assert exit_code == 0
    assert engine_create_calls == [
        {
            "model_name": "ArcFace",
            "detector_backend": "retinaface",
            "distance_metric": "cosine",
            "align": True,
            "distance_threshold": None,
        }
    ]
    assert seen_reference_args == [
        ([[0.1], [0.11]], [[0.2]]),
        ([[0.1], [0.11]], [[0.2]]),
    ]
    assert exported == [candidate_hit.path]
    assert "Scanned files: 2" in stdout
    assert "only-two matches: 1" in stdout
    assert "group matches: 0" in stdout


def test_main_exits_nonzero_when_reference_dir_loading_fails(monkeypatch, tmp_path, capsys) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    ref_a_dir = tmp_path / "ref-a"
    ref_b_dir = tmp_path / "ref-b"
    input_dir.mkdir()
    output_dir.mkdir()
    ref_a_dir.mkdir()
    ref_b_dir.mkdir()

    from hikbox_pictures.reference_loader import ReferenceImageError

    monkeypatch.setattr(cli_module.DeepFaceEngine, "create", lambda **kwargs: object())

    def raise_reference_error(path, engine):
        raise ReferenceImageError(f"bad ref dir: {path.name}")

    monkeypatch.setattr("hikbox_pictures.cli.load_reference_embeddings", raise_reference_error)

    exit_code = main(_build_argv(input_dir, ref_a_dir, ref_b_dir, output_dir))

    stderr = capsys.readouterr().err
    assert exit_code == 2
    assert "bad ref dir: ref-a" in stderr


@pytest.mark.parametrize("path_kind", ["missing", "file"])
def test_main_exits_nonzero_when_reference_arg_is_not_directory(path_kind, tmp_path, capsys) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    ref_a_dir = tmp_path / "ref-a"
    ref_b_dir = tmp_path / "ref-b"
    input_dir.mkdir()
    output_dir.mkdir()
    ref_a_dir.mkdir()
    ref_b_dir.mkdir()

    invalid_path = ref_a_dir
    if path_kind == "missing":
        invalid_path = tmp_path / "missing-dir"
    if path_kind == "file":
        invalid_path = tmp_path / "ref-a-file"
        invalid_path.write_text("x")

    exit_code = main(_build_argv(input_dir, invalid_path, ref_b_dir, output_dir))

    stderr = capsys.readouterr().err
    assert exit_code == 2
    assert "Reference path is not a directory" in stderr


def test_main_exits_nonzero_when_engine_init_fails(monkeypatch, tmp_path, capsys) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    ref_a_dir = tmp_path / "ref-a"
    ref_b_dir = tmp_path / "ref-b"
    input_dir.mkdir()
    output_dir.mkdir()
    ref_a_dir.mkdir()
    ref_b_dir.mkdir()

    from hikbox_pictures.deepface_engine import DeepFaceInitError

    def raise_init_error(**kwargs):
        raise DeepFaceInitError("init boom")

    monkeypatch.setattr(cli_module.DeepFaceEngine, "create", raise_init_error)

    exit_code = main(_build_argv(input_dir, ref_a_dir, ref_b_dir, output_dir))

    stderr = capsys.readouterr().err
    assert exit_code == 2
    assert "init boom" in stderr


def test_main_executes_without_fallback_when_evaluate_accepts_engine(monkeypatch, tmp_path, capsys) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    ref_a_dir = tmp_path / "ref-a"
    ref_b_dir = tmp_path / "ref-b"
    input_dir.mkdir()
    output_dir.mkdir()
    ref_a_dir.mkdir()
    ref_b_dir.mkdir()

    candidate = CandidatePhoto(path=input_dir / "pair.jpg")
    candidate.path.write_bytes(b"pair")

    fake_engine = object()
    monkeypatch.setattr(cli_module.DeepFaceEngine, "create", lambda **kwargs: fake_engine)
    monkeypatch.setattr(
        "hikbox_pictures.cli.load_reference_embeddings",
        lambda path, engine: ([[0.1]], [path / "sample.jpg"]),
    )
    monkeypatch.setattr("hikbox_pictures.cli.iter_candidate_photos", lambda path: iter([candidate]))

    seen_engines = []

    def fake_evaluate(candidate, person_a_embeddings, person_b_embeddings, *, engine):
        seen_engines.append(engine)
        return PhotoEvaluation(candidate=candidate, detected_face_count=2, bucket=MatchBucket.ONLY_TWO)

    monkeypatch.setattr("hikbox_pictures.cli.evaluate_candidate_photo", fake_evaluate)
    monkeypatch.setattr(
        "hikbox_pictures.cli.resolve_capture_datetime",
        lambda path: datetime(2025, 4, 3, 10, 30),
    )
    monkeypatch.setattr("hikbox_pictures.cli.export_match", lambda evaluation, output_root, capture_datetime: None)

    exit_code = main(_build_argv(input_dir, ref_a_dir, ref_b_dir, output_dir))

    stdout = capsys.readouterr().out
    assert exit_code == 0
    assert seen_engines == [fake_engine]
    assert "Scanned files: 1" in stdout
    assert "only-two matches: 1" in stdout


def test_main_counts_decode_errors_and_missing_live_photo_videos(monkeypatch, tmp_path, capsys) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    ref_a_dir = tmp_path / "ref-a"
    ref_b_dir = tmp_path / "ref-b"
    input_dir.mkdir()
    output_dir.mkdir()
    ref_a_dir.mkdir()
    ref_b_dir.mkdir()

    candidate_heic = CandidatePhoto(path=input_dir / "pair.heic")
    candidate_broken = CandidatePhoto(path=input_dir / "broken.jpg")
    candidate_heic.path.write_bytes(b"pair")
    candidate_broken.path.write_bytes(b"broken")

    fake_engine = object()
    monkeypatch.setattr(cli_module.DeepFaceEngine, "create", lambda **kwargs: fake_engine)
    monkeypatch.setattr(
        "hikbox_pictures.cli.load_reference_embeddings",
        lambda path, engine: ([[0.1]], [path / "sample.jpg"]),
    )
    monkeypatch.setattr(
        "hikbox_pictures.cli.iter_candidate_photos",
        lambda path: iter([candidate_heic, candidate_broken]),
    )

    def fake_evaluate(candidate, enc_a, enc_b, *, engine):
        assert engine is fake_engine
        if candidate is candidate_heic:
            return PhotoEvaluation(candidate=candidate_heic, detected_face_count=3, bucket=MatchBucket.GROUP)
        raise CandidateDecodeError(f"Failed to decode {candidate.path}")

    monkeypatch.setattr("hikbox_pictures.cli.evaluate_candidate_photo", fake_evaluate)
    monkeypatch.setattr(
        "hikbox_pictures.cli.resolve_capture_datetime",
        lambda path: datetime(2025, 4, 3, 10, 30),
    )
    exported = []
    monkeypatch.setattr(
        "hikbox_pictures.cli.export_match",
        lambda evaluation, output_root, capture_datetime: exported.append(evaluation.candidate.path),
    )

    exit_code = main(_build_argv(input_dir, ref_a_dir, ref_b_dir, output_dir))

    captured = capsys.readouterr()
    assert exit_code == 0
    assert exported == [candidate_heic.path]
    assert "Scanned files: 2" in captured.out
    assert "group matches: 1" in captured.out
    assert "Skipped decode errors: 1" in captured.out
    assert "Missing Live Photo videos: 1" in captured.out
    assert f"WARNING: Failed to decode {candidate_broken.path}" in captured.err
    assert f"WARNING: Missing Live Photo MOV for {candidate_heic.path}" in captured.err


def test_main_counts_no_face_photos(monkeypatch, tmp_path, capsys) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    ref_a_dir = tmp_path / "ref-a"
    ref_b_dir = tmp_path / "ref-b"
    input_dir.mkdir()
    output_dir.mkdir()
    ref_a_dir.mkdir()
    ref_b_dir.mkdir()

    candidate = CandidatePhoto(path=input_dir / "noface.jpg")
    candidate.path.write_bytes(b"noface")

    fake_engine = object()
    monkeypatch.setattr(cli_module.DeepFaceEngine, "create", lambda **kwargs: fake_engine)
    monkeypatch.setattr(
        "hikbox_pictures.cli.load_reference_embeddings",
        lambda path, engine: ([[0.1]], [path / "sample.jpg"]),
    )
    monkeypatch.setattr("hikbox_pictures.cli.iter_candidate_photos", lambda path: iter([candidate]))
    monkeypatch.setattr(
        "hikbox_pictures.cli.evaluate_candidate_photo",
        lambda candidate, enc_a, enc_b, *, engine: PhotoEvaluation(
            candidate=candidate,
            detected_face_count=0,
            bucket=None,
        ),
    )

    exit_code = main(_build_argv(input_dir, ref_a_dir, ref_b_dir, output_dir))

    stdout = capsys.readouterr().out
    assert exit_code == 0
    assert "Scanned files: 1" in stdout
    assert "Skipped no-face photos: 1" in stdout


def test_cli_entry_uses_process_argv(monkeypatch) -> None:
    captured = {}

    def fake_main(argv):
        captured["argv"] = argv
        return 7

    monkeypatch.setattr("hikbox_pictures.cli.main", fake_main)
    monkeypatch.setattr("hikbox_pictures.cli.sys.argv", ["hikbox-pictures", "--help"])

    from hikbox_pictures.cli import cli_entry

    assert cli_entry() == 7
    assert captured["argv"] == ["--help"]


def test_main_passes_deepface_tuning_options_to_engine(monkeypatch, tmp_path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    ref_a_dir = tmp_path / "ref-a"
    ref_b_dir = tmp_path / "ref-b"
    input_dir.mkdir()
    output_dir.mkdir()
    ref_a_dir.mkdir()
    ref_b_dir.mkdir()

    create_kwargs = []

    def fake_create_engine(**kwargs):
        create_kwargs.append(kwargs)
        return object()

    monkeypatch.setattr(cli_module.DeepFaceEngine, "create", fake_create_engine)
    monkeypatch.setattr(
        "hikbox_pictures.cli.load_reference_embeddings",
        lambda path, engine: ([[0.1]], [path / "sample.jpg"]),
    )
    monkeypatch.setattr("hikbox_pictures.cli.iter_candidate_photos", lambda path: iter([]))

    argv = _build_argv(input_dir, ref_a_dir, ref_b_dir, output_dir) + [
        "--model-name",
        "Facenet512",
        "--detector-backend",
        "mtcnn",
        "--distance-metric",
        "euclidean_l2",
        "--distance-threshold",
        "0.42",
        "--no-align",
    ]
    exit_code = main(argv)

    assert exit_code == 0
    assert create_kwargs == [
        {
            "model_name": "Facenet512",
            "detector_backend": "mtcnn",
            "distance_metric": "euclidean_l2",
            "align": False,
            "distance_threshold": 0.42,
        }
    ]
