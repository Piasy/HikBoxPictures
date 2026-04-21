from __future__ import annotations

import json
from pathlib import Path

from .conftest import query_one, run_cli


def test_source_add_disable_enable_relabel_remove(cli_bin: str, workspace: Path, photos_dir: Path) -> None:
    assert run_cli(cli_bin, "init", "--workspace", str(workspace)).returncode == 0

    add = run_cli(
        cli_bin,
        "--json",
        "source",
        "add",
        str(photos_dir),
        "--label",
        "family",
        "--workspace",
        str(workspace),
    )
    source_id = int(json.loads(add.stdout)["data"]["source_id"])

    assert run_cli(cli_bin, "--json", "source", "disable", str(source_id), "--workspace", str(workspace)).returncode == 0
    assert query_one(workspace, "SELECT enabled FROM library_source WHERE id=?", [source_id])[0] == 0

    assert run_cli(cli_bin, "--json", "source", "enable", str(source_id), "--workspace", str(workspace)).returncode == 0
    assert query_one(workspace, "SELECT enabled FROM library_source WHERE id=?", [source_id])[0] == 1

    assert (
        run_cli(
            cli_bin,
            "--json",
            "source",
            "relabel",
            str(source_id),
            "family-2026",
            "--workspace",
            str(workspace),
        ).returncode
        == 0
    )
    assert query_one(workspace, "SELECT label FROM library_source WHERE id=?", [source_id])[0] == "family-2026"

    assert run_cli(cli_bin, "--json", "source", "remove", str(source_id), "--workspace", str(workspace)).returncode == 0
    assert query_one(workspace, "SELECT status, enabled FROM library_source WHERE id=?", [source_id]) == ("deleted", 0)


def test_source_add_without_label_uses_path_basename(cli_bin: str, workspace: Path, photos_dir: Path) -> None:
    assert run_cli(cli_bin, "init", "--workspace", str(workspace)).returncode == 0

    add = run_cli(
        cli_bin,
        "--json",
        "source",
        "add",
        str(photos_dir),
        "--workspace",
        str(workspace),
    )
    assert add.returncode == 0
    body = json.loads(add.stdout)
    source_id = int(body["data"]["source_id"])
    expected_label = photos_dir.name
    assert body["data"]["label"] == expected_label

    db_root, db_label, db_enabled, db_status = query_one(
        workspace,
        "SELECT root_path, label, enabled, status FROM library_source WHERE id=?",
        [source_id],
    )
    assert db_root == str(photos_dir.resolve())
    assert db_label == expected_label
    assert db_enabled == 1
    assert db_status == "active"
