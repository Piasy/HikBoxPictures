from datetime import datetime
from pathlib import Path

import pytest

from hikbox_pictures import cli as cli_module
from hikbox_pictures.cli import main
from hikbox_pictures.matcher import CandidateDecodeError
from hikbox_pictures.models import CandidatePhoto, MatchBucket, PhotoEvaluation


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

    def fake_create_engine():
        engine_create_calls.append(1)
        return fake_engine

    monkeypatch.setattr(cli_module.InsightFaceEngine, "create", fake_create_engine)

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
    assert len(engine_create_calls) == 1
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

    monkeypatch.setattr(cli_module.InsightFaceEngine, "create", lambda: object())

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

    from hikbox_pictures.insightface_engine import InsightFaceInitError

    def raise_init_error():
        raise InsightFaceInitError("init boom")

    monkeypatch.setattr(cli_module.InsightFaceEngine, "create", raise_init_error)

    exit_code = main(_build_argv(input_dir, ref_a_dir, ref_b_dir, output_dir))

    stderr = capsys.readouterr().err
    assert exit_code == 2
    assert "init boom" in stderr


def test_main_does_not_fallback_when_evaluate_rejects_engine(monkeypatch, tmp_path) -> None:
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
    monkeypatch.setattr(cli_module.InsightFaceEngine, "create", lambda: fake_engine)
    monkeypatch.setattr(
        "hikbox_pictures.cli.load_reference_embeddings",
        lambda path, engine: ([[0.1]], [path / "sample.jpg"]),
    )
    monkeypatch.setattr("hikbox_pictures.cli.iter_candidate_photos", lambda path: iter([candidate]))

    def fake_evaluate(candidate, person_a_embeddings, person_b_embeddings):
        return PhotoEvaluation(candidate=candidate, detected_face_count=2, bucket=MatchBucket.ONLY_TWO)

    monkeypatch.setattr("hikbox_pictures.cli.evaluate_candidate_photo", fake_evaluate)

    with pytest.raises(TypeError):
        main(_build_argv(input_dir, ref_a_dir, ref_b_dir, output_dir))


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
    monkeypatch.setattr(cli_module.InsightFaceEngine, "create", lambda: fake_engine)
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
    monkeypatch.setattr(cli_module.InsightFaceEngine, "create", lambda: fake_engine)
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
