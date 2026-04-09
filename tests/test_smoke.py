from pathlib import Path

from hikbox_pictures import __version__
from hikbox_pictures.cli import main
from hikbox_pictures.models import CandidatePhoto, MatchBucket, PhotoEvaluation, RunSummary


def test_shared_models_have_expected_defaults(tmp_path: Path) -> None:
    candidate = CandidatePhoto(path=tmp_path / "sample.jpg")
    evaluation = PhotoEvaluation(candidate=candidate, detected_face_count=2, bucket=MatchBucket.ONLY_TWO)
    summary = RunSummary()

    assert candidate.live_photo_video is None
    assert evaluation.bucket is MatchBucket.ONLY_TWO
    assert summary.scanned_files == 0
    assert summary.only_two_matches == 0
    assert summary.group_matches == 0
    assert summary.skipped_decode_errors == 0
    assert summary.warnings == []


def test_package_exports_version_and_minimal_cli() -> None:
    assert __version__ == "0.1.0"
    assert main() == 0


def test_project_metadata_points_to_existing_files() -> None:
    assert Path("README.md").is_file()
    assert Path("src/hikbox_pictures/cli.py").is_file()

    gitignore = Path(".gitignore").read_text(encoding="utf-8")
    assert ".worktrees/\n" in gitignore
    assert "test\n" not in gitignore
