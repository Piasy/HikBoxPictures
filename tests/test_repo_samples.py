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
    assert "Python 3.12" in readme
    assert "./scripts/install.sh" in readme
    assert "./scripts/run_tests.sh" in readme

    assert "deepface" in readme_lower
    assert "tf-keras" in readme_lower
    assert "insightface" not in readme_lower
    assert "onnxruntime" not in readme_lower
    assert "face_recognition" not in readme_lower

    for command_snippet in (
        "init --workspace",
        "serve --workspace",
        "scan --workspace",
        "scan status --workspace",
        "rebuild-artifacts --workspace",
        "export run --workspace",
        "logs tail --workspace",
        "logs prune --workspace",
    ):
        assert command_snippet in readme

    phase1_title = "## v3 第一阶段：身份层重建与调参验收（phase1）"
    assert phase1_title in readme
    phase1_section = readme.split(phase1_title, maxsplit=1)[1]
    next_header_marker = "\n## "
    if next_header_marker in phase1_section:
        phase1_section = phase1_section.split(next_header_marker, maxsplit=1)[0]

    phase1_required_tokens = (
        ("scripts/rebuild_identities_v3.py", "--workspace <workspace>", "--dry-run"),
        ("scripts/rebuild_identities_v3.py", "--workspace <workspace>", "--backup-db"),
        (
            "scripts/evaluate_identity_thresholds.py",
            "--workspace <workspace>",
            "--output-dir .tmp/identity-threshold-tuning/<timestamp>/",
        ),
        (
            "scripts/rebuild_identities_v3.py",
            "--workspace <workspace-copy>",
            "--backup-db",
            "--threshold-profile",
        ),
        ("python -m hikbox_pictures.cli serve", "--workspace <workspace>", "--host 0.0.0.0", "--port 8000"),
    )
    for token_group in phase1_required_tokens:
        for token in token_group:
            assert token in phase1_section

    for phase1_phrase in (
        "/identity-tuning",
        "phase1 明确允许",
        "scan/review/actions/export",
        "主链验收必须包含真实图片路径",
        "seed/mock",
    ):
        assert phase1_phrase in phase1_section

    assert ("Pillow" in readme and "pillow-heif" in readme) or ("pyproject.toml" in readme)


def test_readme_removes_legacy_matching_and_debug_scripts() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "--input" not in readme
    assert "--ref-a-dir" not in readme
    assert "--ref-b-dir" not in readme
    assert "--distance-threshold-a" not in readme
    assert "--distance-threshold-b" not in readme
    assert "inspect_distances.py" not in readme
    assert "extract_faces.py" not in readme
    assert "calibrate_thresholds.py" not in readme
