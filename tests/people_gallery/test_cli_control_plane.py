from __future__ import annotations

import sys
from pathlib import Path

from hikbox_pictures.cli import main


def test_cli_init_creates_workspace_and_db(tmp_path: Path) -> None:
    rc = main(["init", "--workspace", str(tmp_path)])

    assert rc == 0
    assert (tmp_path / ".hikbox" / "library.db").exists()


def test_cli_help_contains_control_plane_commands(capsys) -> None:
    rc = main(["--help"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "init" in out
    assert "source" in out
    assert "serve" in out
    assert "scan" in out
    assert "rebuild-artifacts" in out
    assert "export" in out
    assert "logs" in out


def test_cli_logs_help_contains_tail_and_prune(capsys) -> None:
    rc = main(["logs", "--help"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "tail" in out
    assert "prune" in out


def test_cli_export_help_contains_run(capsys) -> None:
    rc = main(["export", "--help"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "run" in out


def test_scan_status_command(tmp_path: Path, capsys) -> None:
    rc_init = main(["init", "--workspace", str(tmp_path)])
    assert rc_init == 0

    rc_status = main(["scan", "status", "--workspace", str(tmp_path)])
    assert rc_status == 0
    out = capsys.readouterr().out
    assert "scan session_id=" in out
    assert "status=idle" in out


def test_init_does_not_import_deepface_engine(tmp_path: Path) -> None:
    sys.modules.pop("hikbox_pictures.deepface_engine", None)

    rc = main(["init", "--workspace", str(tmp_path)])

    assert rc == 0
    assert "hikbox_pictures.deepface_engine" not in sys.modules


def test_unimplemented_commands_return_nonzero_and_stderr(tmp_path: Path, capsys) -> None:
    rc_logs = main(["logs", "prune", "--workspace", str(tmp_path)])
    assert rc_logs == 2
    err_logs = capsys.readouterr().err
    assert "logs prune 未实现" in err_logs
