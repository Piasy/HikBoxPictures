from __future__ import annotations

import re
from types import SimpleNamespace
from pathlib import Path

from hikbox_pictures import cli as cli_module
from hikbox_pictures.cli import main


def test_main_returns_zero_when_called_without_argv() -> None:
    assert main() == 0


def test_main_prints_help_when_called_with_empty_argv(capsys) -> None:
    exit_code = main([])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "usage:" in captured.out
    assert "hikbox-pictures" in captured.out


def test_main_rejects_legacy_matching_flags(capsys) -> None:
    exit_code = main(["--input", "/tmp/in", "--ref-a-dir", "/tmp/a", "--ref-b-dir", "/tmp/b", "--output", "/tmp/out"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "invalid choice" in captured.err or "unrecognized arguments" in captured.err


def test_init_command_invokes_workspace_initializer(monkeypatch, tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "state" / "people_gallery.sqlite3"
    fake_paths = SimpleNamespace(root=tmp_path, db_path=db_path)

    monkeypatch.setattr(cli_module, "initialize_workspace", lambda workspace: fake_paths)

    exit_code = main(["init", "--workspace", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"Workspace initialized: {tmp_path}" in captured.out
    assert f"Database path: {db_path}" in captured.out


def test_source_commands_add_list_remove(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "workspace"
    source_root = tmp_path / "input"
    source_root.mkdir(parents=True)

    assert main(["init", "--workspace", str(workspace)]) == 0
    capsys.readouterr()

    rc_add = main(
        [
            "source",
            "add",
            "--workspace",
            str(workspace),
            "--name",
            "sample-input",
            "--root-path",
            str(source_root),
        ]
    )
    assert rc_add == 0
    out_add = capsys.readouterr().out
    assert "status=added" in out_add or "status=exists" in out_add
    assert f"root_path={source_root.resolve()}" in out_add

    match = re.search(r"id=(\d+)", out_add)
    assert match is not None
    source_id = match.group(1)

    rc_list = main(["source", "list", "--workspace", str(workspace)])
    assert rc_list == 0
    out_list = capsys.readouterr().out
    assert f"id={source_id}" in out_list
    assert "name=sample-input" in out_list
    assert f"root_path={source_root.resolve()}" in out_list
    assert "active=1" in out_list

    rc_remove = main(["source", "remove", "--workspace", str(workspace), "--source-id", source_id])
    assert rc_remove == 0
    out_remove = capsys.readouterr().out
    assert f"id={source_id}" in out_remove
    assert "status=removed" in out_remove or "status=already-removed" in out_remove

    rc_list2 = main(["source", "list", "--workspace", str(workspace)])
    assert rc_list2 == 0
    out_list2 = capsys.readouterr().out
    assert f"id={source_id}" in out_list2
    assert "active=0" in out_list2
