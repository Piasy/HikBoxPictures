from datetime import datetime

import pytest

from hikbox_pictures.cli import main
from hikbox_pictures.matcher import CandidateDecodeError
from hikbox_pictures.models import CandidatePhoto, MatchBucket, PhotoEvaluation


def _build_argv(input_dir, ref_a, ref_b, output_dir) -> list[str]:
    return [
        "--input",
        str(input_dir),
        "--ref-a",
        str(ref_a),
        "--ref-b",
        str(ref_b),
        "--output",
        str(output_dir),
    ]


def test_main_returns_zero_when_called_without_argv() -> None:
    assert main() == 0


def test_main_exports_only_hits_and_prints_summary(monkeypatch, tmp_path, capsys) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    ref_a = tmp_path / "ref-a.jpg"
    ref_b = tmp_path / "ref-b.jpg"
    input_dir.mkdir()
    output_dir.mkdir()
    ref_a.write_bytes(b"a")
    ref_b.write_bytes(b"b")

    candidate_hit = CandidatePhoto(path=input_dir / "pair.jpg")
    candidate_miss = CandidatePhoto(path=input_dir / "miss.jpg")
    candidate_hit.path.write_bytes(b"pair")
    candidate_miss.path.write_bytes(b"miss")

    monkeypatch.setattr(
        "hikbox_pictures.cli.load_reference_encoding",
        lambda path: [0.1] if path == ref_a else [0.2],
    )
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
    monkeypatch.setattr(
        "hikbox_pictures.cli.evaluate_candidate_photo",
        lambda candidate, enc_a, enc_b: next(evaluations),
    )
    monkeypatch.setattr(
        "hikbox_pictures.cli.resolve_capture_datetime",
        lambda path: datetime(2025, 4, 3, 10, 30),
    )
    exported = []
    monkeypatch.setattr(
        "hikbox_pictures.cli.export_match",
        lambda evaluation, output_root, capture_datetime: exported.append(evaluation.candidate.path),
    )

    exit_code = main(_build_argv(input_dir, ref_a, ref_b, output_dir))

    stdout = capsys.readouterr().out
    assert exit_code == 0
    assert exported == [candidate_hit.path]
    assert "Scanned files: 2" in stdout
    assert "only-two matches: 1" in stdout
    assert "group matches: 0" in stdout


def test_main_exits_nonzero_for_invalid_reference_photo(monkeypatch, tmp_path, capsys) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    ref_a = tmp_path / "ref-a.jpg"
    ref_b = tmp_path / "ref-b.jpg"
    input_dir.mkdir()
    output_dir.mkdir()
    ref_a.write_bytes(b"a")
    ref_b.write_bytes(b"b")

    from hikbox_pictures.reference_loader import ReferenceImageError

    def raise_reference_error(path):
        raise ReferenceImageError(f"bad ref: {path.name}")

    monkeypatch.setattr("hikbox_pictures.cli.load_reference_encoding", raise_reference_error)

    exit_code = main(_build_argv(input_dir, ref_a, ref_b, output_dir))

    stderr = capsys.readouterr().err
    assert exit_code == 2
    assert "bad ref: ref-a.jpg" in stderr


def test_main_counts_decode_errors_and_missing_live_photo_videos(monkeypatch, tmp_path, capsys) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    ref_a = tmp_path / "ref-a.jpg"
    ref_b = tmp_path / "ref-b.jpg"
    input_dir.mkdir()
    output_dir.mkdir()
    ref_a.write_bytes(b"a")
    ref_b.write_bytes(b"b")

    candidate_heic = CandidatePhoto(path=input_dir / "pair.heic")
    candidate_broken = CandidatePhoto(path=input_dir / "broken.jpg")
    candidate_heic.path.write_bytes(b"pair")
    candidate_broken.path.write_bytes(b"broken")

    monkeypatch.setattr("hikbox_pictures.cli.load_reference_encoding", lambda path: [0.1])
    monkeypatch.setattr(
        "hikbox_pictures.cli.iter_candidate_photos",
        lambda path: iter([candidate_heic, candidate_broken]),
    )

    def fake_evaluate(candidate, enc_a, enc_b):
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

    exit_code = main(_build_argv(input_dir, ref_a, ref_b, output_dir))

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
    ref_a = tmp_path / "ref-a.jpg"
    ref_b = tmp_path / "ref-b.jpg"
    input_dir.mkdir()
    output_dir.mkdir()
    ref_a.write_bytes(b"a")
    ref_b.write_bytes(b"b")

    candidate = CandidatePhoto(path=input_dir / "noface.jpg")
    candidate.path.write_bytes(b"noface")

    monkeypatch.setattr("hikbox_pictures.cli.load_reference_encoding", lambda path: [0.1])
    monkeypatch.setattr("hikbox_pictures.cli.iter_candidate_photos", lambda path: iter([candidate]))
    monkeypatch.setattr(
        "hikbox_pictures.cli.evaluate_candidate_photo",
        lambda candidate, enc_a, enc_b: PhotoEvaluation(candidate=candidate, detected_face_count=0, bucket=None),
    )

    exit_code = main(_build_argv(input_dir, ref_a, ref_b, output_dir))

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
