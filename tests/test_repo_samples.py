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


def test_readme_mentions_macos_dependencies() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "macOS" in readme
    assert "insightface" in readme
    assert "onnxruntime" in readme
    assert "face_recognition" not in readme
    assert "## 依赖要求" in readme
    assert "## 安装" in readme
    assert "## 用法" in readme
    assert "## 输出结构" in readme
    assert "## 限制" in readme
    assert "Python 3.13+" in readme
    assert "Xcode Command Line Tools" in readme
    assert "./scripts/install.sh" in readme
    assert "hikbox-pictures --input" in readme
    assert "--ref-a-dir" in readme
    assert "--ref-b-dir" in readme
    assert "only-two/YYYY-MM" in readme
    assert "group/YYYY-MM" in readme
    assert "首次运行会自动下载模型" in readme
    assert "需联网" in readme
    assert "首次会稍慢" in readme
    assert "非商业研究用途" in readme
    assert "工具只扫描图片文件，不分析视频内容。" in readme
