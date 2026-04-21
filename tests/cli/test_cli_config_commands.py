from __future__ import annotations

import json
from pathlib import Path

from .conftest import run_cli


def test_config_show_and_set_external_root(cli_bin: str, workspace: Path) -> None:
    assert run_cli(cli_bin, "init", "--workspace", str(workspace)).returncode == 0

    show_before = run_cli(cli_bin, "--json", "config", "show", "--workspace", str(workspace))
    assert show_before.returncode == 0
    before_data = json.loads(show_before.stdout)["data"]
    assert before_data["workspace"] == str(workspace.resolve())
    assert before_data["external_root"].endswith("/external")

    new_external = (workspace / "ext-root").resolve()
    set_root = run_cli(
        cli_bin,
        "--json",
        "config",
        "set-external-root",
        str(new_external),
        "--workspace",
        str(workspace),
    )
    assert set_root.returncode == 0
    set_data = json.loads(set_root.stdout)["data"]
    assert set_data["external_root"] == str(new_external)

    show_after = run_cli(cli_bin, "--json", "config", "show", "--workspace", str(workspace))
    after_data = json.loads(show_after.stdout)["data"]
    assert show_after.returncode == 0
    assert after_data["external_root"] == str(new_external)


def test_config_set_external_root_rejects_relative_path(cli_bin: str, workspace: Path) -> None:
    assert run_cli(cli_bin, "init", "--workspace", str(workspace)).returncode == 0

    bad = run_cli(
        cli_bin,
        "config",
        "set-external-root",
        "relative/path",
        "--workspace",
        str(workspace),
    )
    assert bad.returncode == 2
    assert "external_root 必须是绝对路径" in (bad.stdout + bad.stderr)


def test_config_show_fails_when_external_root_missing(cli_bin: str, workspace: Path) -> None:
    assert run_cli(cli_bin, "init", "--workspace", str(workspace)).returncode == 0
    config_path = workspace / ".hikbox" / "config.json"
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw.pop("external_root", None)
    config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    proc = run_cli(cli_bin, "config", "show", "--workspace", str(workspace))
    assert proc.returncode == 2
    assert "external_root" in (proc.stdout + proc.stderr)


def test_config_show_fails_when_config_json_is_not_object(cli_bin: str, workspace: Path) -> None:
    assert run_cli(cli_bin, "init", "--workspace", str(workspace)).returncode == 0
    config_path = workspace / ".hikbox" / "config.json"
    config_path.write_text("[]", encoding="utf-8")

    proc = run_cli(cli_bin, "config", "show", "--workspace", str(workspace))
    assert proc.returncode == 2
    assert "VALIDATION_ERROR" in (proc.stdout + proc.stderr)
