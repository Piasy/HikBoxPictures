from pathlib import Path
import tomllib

import pytest

from hikbox_pictures import __version__
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
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert __version__ == pyproject["project"]["version"]

    # CLI 模块导入不应因 deepface 的可选依赖异常而在导入期崩溃。
    from hikbox_pictures.cli import main

    assert main() == 0


def test_deepface_import_non_importerror_is_guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """锁定导入期异常（例如 retinaface 因缺少 tf-keras 抛 ValueError）必须被兜底。"""

    import builtins
    import importlib
    import sys

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0 and name == "deepface":
            raise ValueError("requires tf-keras")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    sys.modules.pop("hikbox_pictures.deepface_engine", None)
    deepface_engine = importlib.import_module("hikbox_pictures.deepface_engine")

    assert deepface_engine.DeepFace is None
    with pytest.raises(deepface_engine.DeepFaceInitError):
        deepface_engine.DeepFaceEngine.create()


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
    assert "insightface" not in install_script_lower
    assert "onnxruntime" not in install_script_lower

    assert "sys.version_info" in install_script
    assert "3.13" in install_script
    assert "VENV_PYTHON" in install_script
    assert '"${VENV_PYTHON}" -m pip install --upgrade pip' in install_script
    assert '"${VENV_PYTHON}" -m pip install -e' in install_script


def test_pyproject_dependencies_use_deepface_runtime() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]

    assert any(dep.startswith("deepface") for dep in dependencies)
    assert all(not dep.startswith("insightface") for dep in dependencies)
    assert all(not dep.startswith("onnxruntime") for dep in dependencies)
    assert all(not dep.startswith("face_recognition") for dep in dependencies)
