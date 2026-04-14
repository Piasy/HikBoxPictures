from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_review_queue_visual_check_smoke(tmp_path: Path) -> None:
    if os.environ.get("RUN_PLAYWRIGHT_VISUAL") != "1":
        pytest.skip("未启用 RUN_PLAYWRIGHT_VISUAL=1，跳过视觉检查 smoke。")
    if shutil.which("node") is None:
        pytest.skip("缺少 node，跳过视觉检查 smoke。")
    if shutil.which("npm") is None:
        pytest.skip("缺少 npm，跳过视觉检查 smoke。")

    repo_root = Path(__file__).resolve().parents[2]
    output_dir = tmp_path / "review-visual"
    workspace = repo_root / "sample" / "workspace"
    if not workspace.exists():
        pytest.skip("缺少 sample/workspace，跳过 reviews 视觉检查 smoke。")
    command = [
        sys.executable,
        str(repo_root / "tools" / "review_queue_playwright_check.py"),
        "--workspace",
        str(workspace),
        "--output-dir",
        str(output_dir),
    ]
    result = subprocess.run(
        command,
        cwd=str(repo_root),
        env={**os.environ, "PYTHONPATH": "src"},
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(
            "review_queue_playwright_check 执行失败\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

    summary_path = output_dir / "review-queue-visual-summary.json"
    assert summary_path.exists()
    assert (output_dir / "captures" / "workspace" / "reviews-desktop.png").exists()
    assert (output_dir / "captures" / "workspace" / "reviews-mobile.png").exists()
