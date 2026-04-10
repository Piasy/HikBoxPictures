from pathlib import Path
import tomllib

import numpy as np

from hikbox_pictures import __version__
from hikbox_pictures.cli import main
from hikbox_pictures.models import (
    CandidatePhoto,
    MatchBucket,
    PhotoEvaluation,
    ReferenceSample,
    ReferenceTemplate,
    RunSummary,
    TemplateMatchResult,
)


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


def test_shared_models_have_expected_template_defaults(tmp_path: Path) -> None:
    sample = ReferenceSample(
        path=tmp_path / "ref.jpg",
        embedding=np.asarray([0.1, 0.2], dtype=np.float32),
        bbox=(1, 5, 6, 0),
        image_size=(100, 80),
        face_area_ratio=0.25,
        sharpness_score=12.0,
        quality_score=0.8,
        center_distance=0.15,
        kept=True,
        drop_reason=None,
    )
    template = ReferenceTemplate(
        name="A",
        samples=[sample],
        kept_samples=[sample],
        centroid_embedding=np.asarray([0.1, 0.2], dtype=np.float32),
        match_threshold=0.42,
        top_k=1,
    )
    result = TemplateMatchResult(
        template_distance=0.2,
        centroid_distance=0.25,
        matched=True,
        top_k_distances=[0.2],
    )
    candidate = CandidatePhoto(path=tmp_path / "pair.jpg")
    evaluation = PhotoEvaluation(
        candidate=candidate,
        detected_face_count=2,
        bucket=MatchBucket.ONLY_TWO,
        joint_distance=0.2,
        best_match_pair=(0, 1),
    )

    assert template.name == "A"
    assert template.dropped_samples == []
    assert template.match_threshold == 0.42
    assert template.top_k == 1
    assert result.matched is True
    assert result.top_k_distances == [0.2]
    assert evaluation.bucket is MatchBucket.ONLY_TWO
    assert evaluation.joint_distance == 0.2
    assert evaluation.best_match_pair == (0, 1)


def test_package_exports_version_and_minimal_cli() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert __version__ == pyproject["project"]["version"]

    assert main() == 0


def test_project_metadata_points_to_existing_files() -> None:
    assert Path("README.md").is_file()
    assert Path("src/hikbox_pictures/cli.py").is_file()
    assert Path("scripts/install.sh").is_file()
    assert Path("scripts/inspect_distances.py").is_file()

    readme = Path("README.md").read_text(encoding="utf-8")
    install_script = Path("scripts/install.sh").read_text(encoding="utf-8")
    install_script_lower = install_script.lower()

    assert "./scripts/install.sh" in readme
    assert "inspect_distances.py" in readme

    assert "deepface" in install_script_lower
    assert "tf-keras" in install_script_lower
    assert "insightface" not in install_script_lower
    assert "onnxruntime" not in install_script_lower
    assert "`deepface`" not in install_script
    assert "`tf-keras`" not in install_script

    assert "sys.version_info" in install_script
    assert "3.13" in install_script
    assert "VENV_PYTHON" in install_script
    assert '"${VENV_PYTHON}" -m pip install --upgrade pip' in install_script
    assert '"${VENV_PYTHON}" -m pip install -e' in install_script


def test_pyproject_dependencies_use_deepface_runtime() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert any(dep.startswith("deepface") for dep in dependencies)
    assert any(dep.startswith("tf-keras") for dep in dependencies)
    assert all(not dep.startswith("insightface") for dep in dependencies)
    assert all(not dep.startswith("onnxruntime") for dep in dependencies)
    assert all(not dep.startswith("face_recognition") for dep in dependencies)
