from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .conftest import create_scan_session, query_one, run_cli


def test_scan_start_or_resume_resumes_latest_interrupted(cli_bin: str, seeded_workspace: Path) -> None:
    older_interrupted = create_scan_session(seeded_workspace, status="interrupted", run_kind="scan_resume")
    latest_interrupted = create_scan_session(seeded_workspace, status="interrupted", run_kind="scan_resume")
    total_before = query_one(seeded_workspace, "SELECT COUNT(*) FROM scan_session")[0]

    resume = run_cli(cli_bin, "--json", "scan", "start-or-resume", "--workspace", str(seeded_workspace))
    data = json.loads(resume.stdout)["data"]
    assert resume.returncode == 0
    assert data["resumed"] is True
    assert int(data["session_id"]) == latest_interrupted
    assert data["status"] == "completed"
    assert query_one(seeded_workspace, "SELECT status FROM scan_session WHERE id=?", [latest_interrupted])[0] == data["status"]
    assert query_one(seeded_workspace, "SELECT COUNT(*) FROM scan_session")[0] == total_before

    resume_again = run_cli(cli_bin, "--json", "scan", "start-or-resume", "--workspace", str(seeded_workspace))
    data_again = json.loads(resume_again.stdout)["data"]
    assert resume_again.returncode == 0
    assert data_again["resumed"] is True
    assert int(data_again["session_id"]) == older_interrupted
    assert data_again["status"] == query_one(seeded_workspace, "SELECT status FROM scan_session WHERE id=?", [older_interrupted])[0]


def test_scan_start_new_and_abort_contract(cli_bin: str, seeded_workspace: Path) -> None:
    old_interrupted = create_scan_session(seeded_workspace, status="interrupted", run_kind="scan_resume")

    start_new_from_interrupted = run_cli(cli_bin, "--json", "scan", "start-new", "--workspace", str(seeded_workspace))
    start_new_data = json.loads(start_new_from_interrupted.stdout)["data"]
    assert start_new_from_interrupted.returncode == 0
    assert int(start_new_data["session_id"]) != old_interrupted
    assert start_new_data["resumed"] is False
    assert start_new_data["status"] == query_one(
        seeded_workspace,
        "SELECT status FROM scan_session WHERE id=?",
        [start_new_data["session_id"]],
    )[0]
    assert start_new_data["status"] == "completed"
    assert query_one(seeded_workspace, "SELECT status FROM scan_session WHERE id=?", [old_interrupted])[0] == "abandoned"

    running_id = create_scan_session(seeded_workspace, status="running")
    new_conflict = run_cli(cli_bin, "scan", "start-new", "--workspace", str(seeded_workspace))
    assert new_conflict.returncode == 4
    assert "SCAN_ACTIVE_CONFLICT" in (new_conflict.stdout + new_conflict.stderr)

    abort_run = run_cli(
        cli_bin,
        "--json",
        "scan",
        "abort",
        str(running_id),
        "--workspace",
        str(seeded_workspace),
    )
    assert abort_run.returncode == 0
    abort_data = json.loads(abort_run.stdout)["data"]
    assert abort_data["session_id"] == running_id
    assert abort_data["status"] == "aborting"
    assert query_one(seeded_workspace, "SELECT status FROM scan_session WHERE id=?", [running_id])[0] in {
        "aborting",
        "interrupted",
    }


def test_scan_abort_then_manual_interrupt_allows_start_new(cli_bin: str, seeded_workspace: Path) -> None:
    session_id = create_scan_session(seeded_workspace, status="running")
    abort_run = run_cli(cli_bin, "--json", "scan", "abort", str(session_id), "--workspace", str(seeded_workspace))
    assert abort_run.returncode == 0
    assert json.loads(abort_run.stdout)["data"]["status"] == "aborting"

    # CLI-only 验证时模拟扫描主流程收敛，把 aborting 置为 interrupted 后继续验证 start-new。
    with sqlite3.connect(seeded_workspace / ".hikbox" / "library.db") as conn:
        conn.execute(
            "UPDATE scan_session SET status='interrupted', updated_at=CURRENT_TIMESTAMP WHERE id=?",
            [session_id],
        )
        conn.commit()
    old_interrupted = session_id

    start_new = run_cli(cli_bin, "--json", "scan", "start-new", "--workspace", str(seeded_workspace))
    data = json.loads(start_new.stdout)["data"]
    assert start_new.returncode == 0
    assert data["resumed"] is False
    assert data["status"] == query_one(seeded_workspace, "SELECT status FROM scan_session WHERE id=?", [data["session_id"]])[0]
    assert query_one(seeded_workspace, "SELECT status FROM scan_session WHERE id=?", [old_interrupted])[0] == "abandoned"
