from __future__ import annotations

import json
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

from .conftest import create_scan_session, query_one, run_cli


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_http_ok(url: str, timeout_seconds: float = 8.0) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:  # nosec B310
                if int(resp.status) == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    return False


def test_init_creates_workspace_files(cli_bin: str, workspace: Path) -> None:
    init = run_cli(cli_bin, "init", "--workspace", str(workspace))
    assert init.returncode == 0
    assert (workspace / ".hikbox" / "library.db").exists()
    assert (workspace / ".hikbox" / "embedding.db").exists()


def test_init_fails_when_existing_config_mismatch(cli_bin: str, workspace: Path) -> None:
    assert run_cli(cli_bin, "init", "--workspace", str(workspace)).returncode == 0
    config_path = workspace / ".hikbox" / "config.json"
    bad = {
        "version": 1,
        "external_root": str((workspace / "other-external").resolve()),
    }
    config_path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")

    run = run_cli(cli_bin, "--json", "init", "--workspace", str(workspace))
    assert run.returncode == 2
    assert "工作区配置不匹配" in (run.stdout + run.stderr)


def test_serve_start_success_path(cli_bin: str, workspace: Path) -> None:
    assert run_cli(cli_bin, "init", "--workspace", str(workspace)).returncode == 0

    port = _find_free_port()
    proc = subprocess.Popen(  # noqa: S603
        [
            cli_bin,
            "-m",
            "hikbox_pictures.cli",
            "serve",
            "start",
            "--workspace",
            str(workspace),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert _wait_http_ok(f"http://127.0.0.1:{port}/") is True
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_serve_start_blocked_when_scan_active(cli_bin: str, workspace: Path) -> None:
    assert run_cli(cli_bin, "init", "--workspace", str(workspace)).returncode == 0
    active_session_id = create_scan_session(workspace, status="running")

    run = run_cli(cli_bin, "serve", "start", "--workspace", str(workspace), "--host", "127.0.0.1", "--port", "8010")
    assert run.returncode == 7
    assert "SERVE_BLOCKED_BY_ACTIVE_SCAN" in (run.stderr + run.stdout)

    status = query_one(workspace, "SELECT status FROM scan_session WHERE id=?", [active_session_id])[0]
    assert status == "running"
