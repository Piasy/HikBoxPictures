from __future__ import annotations

import subprocess
from pathlib import Path


def launch_scan_runner_process(*, python_executable: str, workspace_root: Path, session_id: int) -> None:
    """启动扫描会话后台执行子进程。"""
    cmd = [
        str(python_executable),
        "-m",
        "hikbox_pictures.cli",
        "--json",
        "scan",
        "_run-session",
        "--session-id",
        str(int(session_id)),
        "--workspace",
        str(Path(workspace_root).resolve()),
    ]
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
