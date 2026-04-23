from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from tests.cli.conftest import 读取_json输出


def test_json_and_quiet_output_modes_apply_to_logs_list(
    已播种工作区: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
) -> None:
    json_result = 运行_cli(["--json", "logs", "list", "--workspace", str(已播种工作区)])
    quiet_result = 运行_cli(["--quiet", "logs", "list", "--workspace", str(已播种工作区)])

    assert json_result.returncode == 0
    assert 读取_json输出(json_result.stdout)["ok"] is True
    assert quiet_result.returncode == 0
    assert quiet_result.stdout == ""


def test_quiet_mode_成功时无输出但错误时仍保留_stderr(
    已播种工作区: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
) -> None:
    success_result = 运行_cli(["--quiet", "config", "show", "--workspace", str(已播种工作区)])
    error_result = 运行_cli(["--quiet", "people", "rename", "1", "   ", "--workspace", str(已播种工作区)])

    assert success_result.returncode == 0
    assert success_result.stdout == ""
    assert success_result.stderr == ""

    assert error_result.returncode == 2
    assert error_result.stdout == ""
    assert "VALIDATION_ERROR" in error_result.stderr
