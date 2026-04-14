from pathlib import Path
import tomllib

import numpy as np
import pytest

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
    assert evaluation.joint_distance is None
    assert evaluation.best_match_pair is None
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


def test_reference_template_reports_dropped_samples_from_all_samples(tmp_path: Path) -> None:
    kept_sample = ReferenceSample(
        path=tmp_path / "kept.jpg",
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
    dropped_sample = ReferenceSample(
        path=tmp_path / "dropped.jpg",
        embedding=np.asarray([0.3, 0.4], dtype=np.float32),
        bbox=(2, 6, 7, 1),
        image_size=(120, 90),
        face_area_ratio=0.2,
        sharpness_score=8.0,
        quality_score=0.5,
        center_distance=0.45,
        kept=False,
        drop_reason="模糊",
    )

    template = ReferenceTemplate(
        name="A",
        samples=[kept_sample, dropped_sample],
        kept_samples=[kept_sample],
        centroid_embedding=np.asarray([0.1, 0.2], dtype=np.float32),
        match_threshold=0.42,
        top_k=1,
    )

    assert template.dropped_samples == [dropped_sample]


def test_reference_sample_rejects_inconsistent_keep_state(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="drop_reason"):
        ReferenceSample(
            path=tmp_path / "invalid.jpg",
            embedding=np.asarray([0.1, 0.2], dtype=np.float32),
            bbox=(1, 5, 6, 0),
            image_size=(100, 80),
            face_area_ratio=0.25,
            sharpness_score=12.0,
            quality_score=0.8,
            center_distance=0.15,
            kept=True,
            drop_reason="模糊",
        )


def test_reference_template_rejects_inconsistent_kept_samples(tmp_path: Path) -> None:
    kept_sample = ReferenceSample(
        path=tmp_path / "kept.jpg",
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
    dropped_sample = ReferenceSample(
        path=tmp_path / "dropped.jpg",
        embedding=np.asarray([0.3, 0.4], dtype=np.float32),
        bbox=(2, 6, 7, 1),
        image_size=(120, 90),
        face_area_ratio=0.2,
        sharpness_score=8.0,
        quality_score=0.5,
        center_distance=0.45,
        kept=False,
        drop_reason="模糊",
    )

    with pytest.raises(ValueError, match="kept_samples"):
        ReferenceTemplate(
            name="A",
            samples=[kept_sample, dropped_sample],
            kept_samples=[kept_sample, dropped_sample],
            centroid_embedding=np.asarray([0.1, 0.2], dtype=np.float32),
            match_threshold=0.42,
            top_k=1,
        )


def test_reference_template_rejects_same_path_but_different_kept_sample(tmp_path: Path) -> None:
    kept_sample = ReferenceSample(
        path=tmp_path / "kept.jpg",
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
    fake_kept_sample = ReferenceSample(
        path=tmp_path / "kept.jpg",
        embedding=np.asarray([0.9, 0.8], dtype=np.float32),
        bbox=(3, 7, 8, 2),
        image_size=(100, 80),
        face_area_ratio=0.4,
        sharpness_score=3.0,
        quality_score=0.2,
        center_distance=0.5,
        kept=True,
        drop_reason=None,
    )

    with pytest.raises(ValueError, match="kept_samples"):
        ReferenceTemplate(
            name="A",
            samples=[kept_sample],
            kept_samples=[fake_kept_sample],
            centroid_embedding=np.asarray([0.1, 0.2], dtype=np.float32),
            match_threshold=0.42,
            top_k=1,
        )


def test_reference_template_isolated_from_external_list_mutation(tmp_path: Path) -> None:
    kept_sample = ReferenceSample(
        path=tmp_path / "kept.jpg",
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
    dropped_sample = ReferenceSample(
        path=tmp_path / "dropped.jpg",
        embedding=np.asarray([0.3, 0.4], dtype=np.float32),
        bbox=(2, 6, 7, 1),
        image_size=(120, 90),
        face_area_ratio=0.2,
        sharpness_score=8.0,
        quality_score=0.5,
        center_distance=0.45,
        kept=False,
        drop_reason="模糊",
    )
    samples = [kept_sample, dropped_sample]
    kept_samples = [kept_sample]

    template = ReferenceTemplate(
        name="A",
        samples=samples,
        kept_samples=kept_samples,
        centroid_embedding=np.asarray([0.1, 0.2], dtype=np.float32),
        match_threshold=0.42,
        top_k=1,
    )

    samples.pop()
    kept_samples.clear()

    assert template.samples == (kept_sample, dropped_sample)
    assert template.kept_samples == (kept_sample,)
    assert template.dropped_samples == [dropped_sample]
    assert isinstance(template.samples, tuple)
    assert isinstance(template.kept_samples, tuple)


def test_package_exports_version_and_minimal_cli() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert __version__ == pyproject["project"]["version"]

    assert main() == 0


def test_project_metadata_points_to_existing_files() -> None:
    assert Path("README.md").is_file()
    assert Path("src/hikbox_pictures/cli.py").is_file()
    assert Path("scripts/install.sh").is_file()
    assert not Path("scripts/inspect_distances.py").exists()
    assert not Path("scripts/extract_faces.py").exists()
    assert not Path("scripts/calibrate_thresholds.py").exists()

    readme = Path("README.md").read_text(encoding="utf-8")
    install_script = Path("scripts/install.sh").read_text(encoding="utf-8")
    install_script_lower = install_script.lower()

    assert "./scripts/install.sh" in readme
    assert "--input" not in readme
    assert "--ref-a-dir" not in readme
    assert "--ref-b-dir" not in readme
    assert "inspect_distances.py" not in readme
    assert "extract_faces.py" not in readme
    assert "calibrate_thresholds.py" not in readme

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
