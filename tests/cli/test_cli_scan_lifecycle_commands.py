from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from tests.cli.conftest import 读取_json输出


def test_scan_start_or_resume_恢复最近_interrupted_会话并完成执行(
    已初始化工作区: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    插入扫描会话: Callable[..., int],
    查询单值: Callable[[Path, str, Sequence[object]], object],
) -> None:
    插入扫描会话(已初始化工作区, status="interrupted", run_kind="scan_resume")
    latest_interrupted_id = 插入扫描会话(已初始化工作区, status="interrupted", run_kind="scan_resume")
    total_before = int(查询单值(已初始化工作区, "SELECT COUNT(*) FROM scan_session"))

    result = 运行_cli(["--json", "scan", "start-or-resume", "--workspace", str(已初始化工作区)])
    payload = 读取_json输出(result.stdout)

    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"] == {
        "session_id": latest_interrupted_id,
        "resumed": True,
        "status": "completed",
    }
    assert 查询单值(已初始化工作区, "SELECT status FROM scan_session WHERE id=?", (latest_interrupted_id,)) == "completed"
    assert int(查询单值(已初始化工作区, "SELECT COUNT(*) FROM assignment_run WHERE scan_session_id=?", (latest_interrupted_id,))) == 1
    assert int(查询单值(已初始化工作区, "SELECT COUNT(*) FROM scan_session")) == total_before


@pytest.mark.parametrize("active_status", ["running", "aborting"])
def test_scan_start_or_resume_遇到活动会话直接复用且不新增会话(
    active_status: str,
    已初始化工作区: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    插入扫描会话: Callable[..., int],
    查询单值: Callable[[Path, str, Sequence[object]], object],
) -> None:
    active_session_id = 插入扫描会话(已初始化工作区, status=active_status, run_kind="scan_full")
    total_before = int(查询单值(已初始化工作区, "SELECT COUNT(*) FROM scan_session"))

    result = 运行_cli(["--json", "scan", "start-or-resume", "--workspace", str(已初始化工作区)])
    payload = 读取_json输出(result.stdout)

    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"] == {
        "session_id": active_session_id,
        "resumed": True,
        "status": active_status,
    }
    assert int(查询单值(已初始化工作区, "SELECT COUNT(*) FROM scan_session")) == total_before
    assert int(查询单值(已初始化工作区, "SELECT COUNT(*) FROM assignment_run WHERE scan_session_id=?", (active_session_id,))) == 0


@pytest.mark.parametrize("active_status", ["running", "aborting"])
def test_scan_start_new_遇到活动会话返回冲突退出码4(
    active_status: str,
    已初始化工作区: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    插入扫描会话: Callable[..., int],
) -> None:
    active_session_id = 插入扫描会话(已初始化工作区, status=active_status, run_kind="scan_full")

    result = 运行_cli(["--json", "scan", "start-new", "--workspace", str(已初始化工作区)])

    assert result.returncode == 4
    assert "SCAN_ACTIVE_CONFLICT" in result.stderr
    assert str(active_session_id) in result.stderr


def test_scan_abort_活动会话置为_aborting_且_updated_at前进(
    已初始化工作区: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    插入扫描会话: Callable[..., int],
    查询单值: Callable[[Path, str, Sequence[object]], object],
) -> None:
    session_id = 插入扫描会话(已初始化工作区, status="running", run_kind="scan_full")
    before_updated_at = str(查询单值(已初始化工作区, "SELECT updated_at FROM scan_session WHERE id=?", (session_id,)))
    time.sleep(1.1)

    result = 运行_cli(["--json", "scan", "abort", str(session_id), "--workspace", str(已初始化工作区)])
    payload = 读取_json输出(result.stdout)
    after_status = str(查询单值(已初始化工作区, "SELECT status FROM scan_session WHERE id=?", (session_id,)))
    after_updated_at = str(查询单值(已初始化工作区, "SELECT updated_at FROM scan_session WHERE id=?", (session_id,)))

    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"] == {"session_id": session_id, "status": "aborting"}
    assert after_status == "aborting"
    assert after_updated_at > before_updated_at


def test_scan_start_new_会先_abandoned_interrupted_再新建并完成执行(
    已初始化工作区: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    插入扫描会话: Callable[..., int],
    查询单值: Callable[[Path, str, Sequence[object]], object],
) -> None:
    interrupted_id = 插入扫描会话(已初始化工作区, status="interrupted", run_kind="scan_resume")
    total_before = int(查询单值(已初始化工作区, "SELECT COUNT(*) FROM scan_session"))

    result = 运行_cli(["--json", "scan", "start-new", "--workspace", str(已初始化工作区)])
    payload = 读取_json输出(result.stdout)
    new_session_id = int(payload["data"]["session_id"])

    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"] == {
        "session_id": new_session_id,
        "resumed": False,
        "status": "completed",
    }
    assert new_session_id != interrupted_id
    assert 查询单值(已初始化工作区, "SELECT status FROM scan_session WHERE id=?", (interrupted_id,)) == "abandoned"
    assert int(查询单值(已初始化工作区, "SELECT COUNT(*) FROM scan_session")) == total_before + 1
    assert int(查询单值(已初始化工作区, "SELECT COUNT(*) FROM assignment_run WHERE scan_session_id=?", (new_session_id,))) == 1
