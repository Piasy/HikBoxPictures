from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from tests.cli.conftest import 读取_json输出


def test_scan_start_or_resume_恢复最近中断会话并复用活动会话(
    已初始化工作区: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    插入扫描会话: Callable[..., int],
    查询单值: Callable[[Path, str, Sequence[object]], object],
) -> None:
    插入扫描会话(已初始化工作区, status="interrupted", run_kind="scan_resume")
    latest_interrupted_id = 插入扫描会话(已初始化工作区, status="interrupted", run_kind="scan_resume")
    total_before = int(查询单值(已初始化工作区, "SELECT COUNT(*) FROM scan_session"))

    first_result = 运行_cli(["--json", "scan", "start-or-resume", "--workspace", str(已初始化工作区)])
    first_payload = 读取_json输出(first_result.stdout)

    assert first_result.returncode == 0
    assert first_payload["ok"] is True
    assert first_payload["data"]["resumed"] is True
    assert first_payload["data"]["session_id"] == latest_interrupted_id
    assert (
        查询单值(
            已初始化工作区,
            "SELECT status FROM scan_session WHERE id=?",
            (latest_interrupted_id,),
        )
        == "running"
    )
    assert int(查询单值(已初始化工作区, "SELECT COUNT(*) FROM scan_session")) == total_before

    second_result = 运行_cli(["--json", "scan", "start-or-resume", "--workspace", str(已初始化工作区)])
    second_payload = 读取_json输出(second_result.stdout)

    assert second_result.returncode == 0
    assert second_payload["data"]["resumed"] is True
    assert second_payload["data"]["session_id"] == latest_interrupted_id
    assert int(查询单值(已初始化工作区, "SELECT COUNT(*) FROM scan_session")) == total_before


def test_scan_start_new_冲突放弃中断并支持_abort(
    已初始化工作区: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    插入扫描会话: Callable[..., int],
    查询单值: Callable[[Path, str, Sequence[object]], object],
) -> None:
    interrupted_id = 插入扫描会话(已初始化工作区, status="interrupted", run_kind="scan_resume")

    start_new_result = 运行_cli(["--json", "scan", "start-new", "--workspace", str(已初始化工作区)])
    start_new_payload = 读取_json输出(start_new_result.stdout)
    new_session_id = int(start_new_payload["data"]["session_id"])

    assert start_new_result.returncode == 0
    assert start_new_payload["ok"] is True
    assert start_new_payload["data"]["resumed"] is False
    assert new_session_id != interrupted_id
    assert (
        查询单值(
            已初始化工作区,
            "SELECT status FROM scan_session WHERE id=?",
            (interrupted_id,),
        )
        == "abandoned"
    )

    conflict_result = 运行_cli(["scan", "start-new", "--workspace", str(已初始化工作区)])

    assert conflict_result.returncode == 4
    assert "SCAN_ACTIVE_CONFLICT" in (conflict_result.stdout + conflict_result.stderr)

    abort_before_updated_at = str(
        查询单值(
            已初始化工作区,
            "SELECT updated_at FROM scan_session WHERE id=?",
            (new_session_id,),
        )
    )
    abort_result = 运行_cli(["--json", "scan", "abort", str(new_session_id), "--workspace", str(已初始化工作区)])
    abort_payload = 读取_json输出(abort_result.stdout)

    assert abort_result.returncode == 0
    assert abort_payload["ok"] is True
    assert abort_payload["data"]["session_id"] == new_session_id
    assert abort_payload["data"]["status"] == "aborting"
    assert (
        查询单值(
            已初始化工作区,
            "SELECT status FROM scan_session WHERE id=?",
            (new_session_id,),
        )
        == "aborting"
    )
    assert str(
        查询单值(
            已初始化工作区,
            "SELECT updated_at FROM scan_session WHERE id=?",
            (new_session_id,),
        )
    ) >= abort_before_updated_at


def test_scan_abort_不存在会话返回_not_found退出码(
    已初始化工作区: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
) -> None:
    result = 运行_cli(["scan", "abort", "999999", "--workspace", str(已初始化工作区)])

    assert result.returncode == 3
    assert "NOT_FOUND" in (result.stdout + result.stderr)


def test_scan_three_commands_smoke_校验退出码输出与_db状态(
    已初始化工作区: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    插入扫描会话: Callable[..., int],
    查询单值: Callable[[Path, str, Sequence[object]], object],
) -> None:
    latest_interrupted_id = 插入扫描会话(已初始化工作区, status="interrupted", run_kind="scan_resume")

    resume_result = 运行_cli(["--json", "scan", "start-or-resume", "--workspace", str(已初始化工作区)])
    resume_payload = 读取_json输出(resume_result.stdout)

    assert resume_result.returncode == 0
    assert resume_payload["ok"] is True
    assert resume_payload["data"]["session_id"] == latest_interrupted_id
    assert resume_payload["data"]["resumed"] is True
    assert resume_payload["data"]["status"] == "running"
    assert 查询单值(已初始化工作区, "SELECT status FROM scan_session WHERE id=?", (latest_interrupted_id,)) == "running"

    conflict_result = 运行_cli(["scan", "start-new", "--workspace", str(已初始化工作区)])

    assert conflict_result.returncode == 4
    assert "SCAN_ACTIVE_CONFLICT" in conflict_result.stderr

    abort_result = 运行_cli(["--json", "scan", "abort", str(latest_interrupted_id), "--workspace", str(已初始化工作区)])
    abort_payload = 读取_json输出(abort_result.stdout)

    assert abort_result.returncode == 0
    assert abort_payload["ok"] is True
    assert abort_payload["data"]["session_id"] == latest_interrupted_id
    assert abort_payload["data"]["status"] == "aborting"
    assert 查询单值(已初始化工作区, "SELECT status FROM scan_session WHERE id=?", (latest_interrupted_id,)) == "aborting"
