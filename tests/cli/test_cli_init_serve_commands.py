from __future__ import annotations

import socket
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path


def test_init_成功创建工作区数据库文件(
    tmp_path: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
) -> None:
    workspace = tmp_path / "workspace"

    result = 运行_cli(["init", "--workspace", str(workspace)])

    assert result.returncode == 0
    assert result.stderr == ""
    assert (workspace / ".hikbox" / "library.db").exists()
    assert (workspace / ".hikbox" / "embedding.db").exists()
    assert (workspace / ".hikbox" / "config.json").exists()
    assert "library_db:" in result.stdout
    assert "embedding_db:" in result.stdout


def test_serve_start_正常启动_http服务(
    已初始化工作区: Path,
    启动_cli进程: Callable[[Sequence[str]], subprocess.Popen[str]],
    等待_http_ok: Callable[[str], bool],
) -> None:
    port = "38766"
    process = 启动_cli进程(
        [
            "serve",
            "start",
            "--workspace",
            str(已初始化工作区),
            "--host",
            "127.0.0.1",
            "--port",
            port,
        ]
    )

    try:
        assert 等待_http_ok(f"http://127.0.0.1:{port}/") is True
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_serve_start_存在活动扫描时返回阻断退出码(
    已初始化工作区: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    插入扫描会话: Callable[..., int],
) -> None:
    插入扫描会话(已初始化工作区, status="running")

    result = 运行_cli(
        [
            "serve",
            "start",
            "--workspace",
            str(已初始化工作区),
            "--port",
            "38765",
        ]
    )

    assert result.returncode == 7
    assert "SERVE_BLOCKED_BY_ACTIVE_SCAN" in (result.stdout + result.stderr)


def test_serve_start_端口占用失败时不会先输出成功载荷(
    已初始化工作区: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]

    try:
        result = 运行_cli(
            [
                "--json",
                "serve",
                "start",
                "--workspace",
                str(已初始化工作区),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ]
        )
    finally:
        sock.close()

    assert result.returncode != 0
    assert result.stdout == ""
    assert '"ok": true' not in result.stdout
