from __future__ import annotations

import os
import tomllib
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path


def test_cli_command_signatures_match_spec_15_5(
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    cli帮助输出: Callable[[Sequence[str]], str],
) -> None:
    root_help = cli帮助输出([])
    assert "init" in root_help
    assert "config" in root_help
    assert "source" in root_help
    assert "scan" in root_help
    assert "serve" in root_help
    assert "people" in root_help
    assert "export" in root_help
    assert "logs" in root_help
    assert "audit" in root_help
    assert "db" in root_help

    assert "show" in cli帮助输出(["config"])
    assert "set-external-root" in cli帮助输出(["config"])
    assert "--workspace" in cli帮助输出(["config", "show"])
    assert "external_root" in cli帮助输出(["config", "set-external-root"])

    source_help = cli帮助输出(["source"])
    for command in ("list", "add", "remove", "enable", "disable", "relabel"):
        assert command in source_help
    assert "--label" in cli帮助输出(["source", "add"])

    scan_help = cli帮助输出(["scan"])
    for command in ("start-or-resume", "start-new", "abort", "status", "list"):
        assert command in scan_help
    assert "--latest" in cli帮助输出(["scan", "status"])
    assert "--session-id" in cli帮助输出(["scan", "status"])
    assert "--limit" in cli帮助输出(["scan", "list"])

    people_help = cli帮助输出(["people"])
    for command in ("list", "show", "rename", "exclude", "exclude-batch", "merge", "undo-last-merge"):
        assert command in people_help

    export_help = cli帮助输出(["export"])
    for command in ("template", "run", "run-status", "execute", "run-list"):
        assert command in export_help
    assert "--template-id" in cli帮助输出(["export", "run-list"])
    assert "--limit" in cli帮助输出(["export", "run-list"])

    template_help = cli帮助输出(["export", "template"])
    for command in ("list", "create", "update"):
        assert command in template_help
    assert "delete" not in template_help

    assert "list" in cli帮助输出(["logs"])
    assert "list" in cli帮助输出(["audit"])
    assert "vacuum" in cli帮助输出(["db"])

    delete_probe = 运行_cli(["export", "template", "delete", "1"])
    assert delete_probe.returncode != 0
    assert "delete" in delete_probe.stderr


def test_pyproject_scripts_入口与_cli_entry一致(
    仓库根目录: Path,
) -> None:
    pyproject = tomllib.loads((仓库根目录 / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = pyproject["project"]["scripts"]

    assert scripts["hikbox-pictures"] == "hikbox_pictures.cli:cli_entry"
    assert scripts["hikbox"] == "hikbox_pictures.cli:cli_entry"


def test_help_非法子命令与_people_list_不会在轻量路径触发_torch(
    tmp_path: Path,
    仓库根目录: Path,
    cli_python: Path,
    seeded_workspace: Path,
) -> None:
    sitecustomize = tmp_path / "sitecustomize.py"
    sitecustomize.write_text(
        "\n".join(
            [
                "import builtins",
                "_real_import = builtins.__import__",
                "def _guard(name, globals=None, locals=None, fromlist=(), level=0):",
                "    if name == 'torch' or name.startswith('torch.'):",
                "        raise RuntimeError('torch import blocked during lightweight cli path')",
                "    return _real_import(name, globals, locals, fromlist, level)",
                "builtins.__import__ = _guard",
                "",
            ]
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), str(仓库根目录), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)

    help_result = subprocess.run(
        [str(cli_python), "-m", "hikbox_pictures.cli", "--help"],
        cwd=仓库根目录,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    invalid_result = subprocess.run(
        [str(cli_python), "-m", "hikbox_pictures.cli", "export", "template", "delete", "1"],
        cwd=仓库根目录,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    people_list_result = subprocess.run(
        [
            str(cli_python),
            "-m",
            "hikbox_pictures.cli",
            "--json",
            "people",
            "list",
            "--workspace",
            str(seeded_workspace),
        ],
        cwd=仓库根目录,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert help_result.returncode == 0
    assert "usage:" in help_result.stdout
    assert "torch import blocked" not in (help_result.stdout + help_result.stderr)
    assert invalid_result.returncode != 0
    assert "invalid choice" in invalid_result.stderr
    assert "torch import blocked" not in (invalid_result.stdout + invalid_result.stderr)
    assert people_list_result.returncode == 0
    assert '"ok": true' in people_list_result.stdout
    assert "torch import blocked" not in (people_list_result.stdout + people_list_result.stderr)
