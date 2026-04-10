from pathlib import Path

from hikbox_pictures.scanner import find_live_photo_video


TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
DATA_DIR = TESTS_DIR / "data"


def test_sample_heic_finds_bundled_live_photo_video() -> None:
    sample = DATA_DIR / "IMG_8175.HEIC"
    assert find_live_photo_video(sample) == DATA_DIR / ".IMG_8175_1771856408349261.MOV"


def test_sample_files_exist_as_placeholder_assets() -> None:
    heic = DATA_DIR / "IMG_8175.HEIC"
    mov = DATA_DIR / ".IMG_8175_1771856408349261.MOV"

    assert heic.is_file()
    assert mov.is_file()
    assert heic.stat().st_size == 0
    assert mov.stat().st_size == 0


def test_readme_mentions_deepface_runtime_basics() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    readme_lower = readme.lower()

    assert "macOS" in readme
    assert "Python 3.13+" in readme
    assert "./scripts/install.sh" in readme

    assert "deepface" in readme_lower
    assert "tf-keras" in readme_lower
    assert "insightface" not in readme_lower
    assert "onnxruntime" not in readme_lower
    assert "face_recognition" not in readme_lower

    for flag in ("--model-name", "--detector-backend", "--distance-metric", "--distance-threshold"):
        assert flag in readme
    assert "--align" in readme
    assert "--no-align" in readme

    assert "首次运行" in readme
    assert "下载" in readme
    assert "联网" in readme

    assert "许可" in readme
    assert "核对" in readme

    assert ("Pillow" in readme and "pillow-heif" in readme) or ("pyproject.toml" in readme)
