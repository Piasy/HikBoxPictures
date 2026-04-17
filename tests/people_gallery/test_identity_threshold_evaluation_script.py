from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_evaluate_identity_thresholds_script_returns_deprecation_hint(tmp_path: Path) -> None:
    workspace = tmp_path / "deprecated-eval"
    workspace.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "evaluate_identity_thresholds.py"),
            "--workspace",
            str(workspace),
            "--output-dir",
            str(tmp_path / "out"),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "已弃用" in result.stderr
