from __future__ import annotations

import json
from pathlib import Path

from .conftest import run_cli


def test_json_and_quiet_output_modes(cli_bin: str, workspace: Path) -> None:
    assert run_cli(cli_bin, "init", "--workspace", str(workspace)).returncode == 0

    out_json = run_cli(cli_bin, "--json", "logs", "list", "--workspace", str(workspace)).stdout
    out_quiet = run_cli(cli_bin, "--quiet", "logs", "list", "--workspace", str(workspace)).stdout

    assert json.loads(out_json)["ok"] is True
    assert out_quiet.strip() == ""
