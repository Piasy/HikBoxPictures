from __future__ import annotations

import ast
from pathlib import Path

from .conftest import run_cli


def test_cli_command_signatures_match_spec_15_5(cli_bin: str) -> None:
    root_help = run_cli(cli_bin, "--help")
    assert root_help.returncode == 0
    for command in ["init", "config", "source", "scan", "serve", "people", "export", "logs", "audit", "db"]:
        assert command in root_help.stdout

    checks: list[tuple[list[str], list[str]]] = [
        (["config", "--help"], ["show", "set-external-root"]),
        (["source", "--help"], ["list", "add", "remove", "enable", "disable", "relabel"]),
        (["scan", "--help"], ["start-or-resume", "start-new", "abort", "status", "list"]),
        (["serve", "--help"], ["start"]),
        (["people", "--help"], ["list", "show", "rename", "exclude", "exclude-batch", "merge", "undo-last-merge"]),
        (["export", "--help"], ["template", "run", "run-status", "run-list"]),
        (["export", "template", "--help"], ["list", "create", "update"]),
        (["logs", "--help"], ["list"]),
        (["audit", "--help"], ["list"]),
        (["db", "--help"], ["vacuum"]),
    ]
    for args, expected in checks:
        out = run_cli(cli_bin, *args)
        assert out.returncode == 0
        for token in expected:
            assert token in out.stdout


def test_cli_module_top_level_has_no_web_or_uvicorn_imports() -> None:
    cli_path = Path(__file__).resolve().parents[2] / "hikbox_pictures" / "cli.py"
    module = ast.parse(cli_path.read_text(encoding="utf-8"))

    top_level_imports: set[str] = set()
    for node in module.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level_imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top_level_imports.add(node.module)

    assert "uvicorn" not in top_level_imports
    assert "hikbox_pictures.web.app" not in top_level_imports
