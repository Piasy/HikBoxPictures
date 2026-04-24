from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path


def test_validation_not_found_scan_conflict_export_lock_illegal_state_and_serve_block_codes(
    已播种工作区: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    插入扫描会话: Callable[..., int],
) -> None:
    validation_result = 运行_cli(["people", "rename", "1", "   ", "--workspace", str(已播种工作区)])
    assert validation_result.returncode == 2
    assert "VALIDATION_ERROR" in (validation_result.stdout + validation_result.stderr)

    not_found_result = 运行_cli(["people", "show", "999999", "--workspace", str(已播种工作区)])
    assert not_found_result.returncode == 3
    assert "NOT_FOUND" in (not_found_result.stdout + not_found_result.stderr)

    conflict_session_id = 插入扫描会话(已播种工作区, status="running")
    scan_conflict_result = 运行_cli(["scan", "start-new", "--workspace", str(已播种工作区)])
    assert scan_conflict_result.returncode == 4
    assert str(conflict_session_id) in (scan_conflict_result.stdout + scan_conflict_result.stderr)

    conn = sqlite3.connect(已播种工作区 / ".hikbox" / "library.db")
    try:
        conn.execute(
            """
            INSERT INTO export_run(template_id, status, summary_json, started_at, finished_at)
            VALUES (1, 'running', '{"exported_count":0,"failed_count":0,"skipped_exists_count":0}', CURRENT_TIMESTAMP, NULL)
            """
        )
        conn.commit()
    finally:
        conn.close()
    export_lock_result = 运行_cli(
        [
            "people",
            "merge",
            "--selected-person-ids",
            "4,5",
            "--workspace",
            str(已播种工作区),
        ]
    )
    assert export_lock_result.returncode == 5
    assert "EXPORT_RUNNING_LOCK" in (export_lock_result.stdout + export_lock_result.stderr)

    conn = sqlite3.connect(已播种工作区 / ".hikbox" / "library.db")
    try:
        conn.execute("UPDATE export_run SET status='completed', finished_at=CURRENT_TIMESTAMP WHERE status='running'")
        conn.commit()
    finally:
        conn.close()

    illegal_state_result = 运行_cli(
        [
            "people",
            "exclude",
            "2",
            "--face-observation-id",
            "4",
            "--workspace",
            str(已播种工作区),
        ]
    )
    assert illegal_state_result.returncode == 6
    assert "ILLEGAL_STATE" in (illegal_state_result.stdout + illegal_state_result.stderr)

    serve_block_result = 运行_cli(["serve", "start", "--workspace", str(已播种工作区), "--port", "38765"])
    assert serve_block_result.returncode == 7
    assert "SERVE_BLOCKED_BY_ACTIVE_SCAN" in (serve_block_result.stdout + serve_block_result.stderr)


def test_error_codes_support_json_and_quiet_output_switching(
    已播种工作区: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    插入扫描会话: Callable[..., int],
) -> None:
    validation_result = 运行_cli(["--json", "people", "rename", "1", "   ", "--workspace", str(已播种工作区)])
    validation_payload = json.loads(validation_result.stderr)
    assert validation_result.returncode == 2
    assert validation_payload["error"]["code"] == "VALIDATION_ERROR"

    not_found_result = 运行_cli(["--json", "people", "show", "999999", "--workspace", str(已播种工作区)])
    not_found_payload = json.loads(not_found_result.stderr)
    assert not_found_result.returncode == 3
    assert not_found_payload["error"]["code"] == "NOT_FOUND"

    active_session_id = 插入扫描会话(已播种工作区, status="running")
    scan_conflict_result = 运行_cli(["--json", "scan", "start-new", "--workspace", str(已播种工作区)])
    scan_conflict_payload = json.loads(scan_conflict_result.stderr)
    assert scan_conflict_result.returncode == 4
    assert scan_conflict_payload["error"]["code"] == "SCAN_ACTIVE_CONFLICT"
    assert scan_conflict_payload["error"]["active_session_id"] == active_session_id

    conn = sqlite3.connect(已播种工作区 / ".hikbox" / "library.db")
    try:
        conn.execute(
            """
            INSERT INTO export_run(template_id, status, summary_json, started_at, finished_at)
            VALUES (1, 'running', '{"exported_count":0,"failed_count":0,"skipped_exists_count":0}', CURRENT_TIMESTAMP, NULL)
            """
        )
        conn.commit()
    finally:
        conn.close()

    export_lock_result = 运行_cli(
        ["--json", "people", "merge", "--selected-person-ids", "4,5", "--workspace", str(已播种工作区)]
    )
    export_lock_payload = json.loads(export_lock_result.stderr)
    assert export_lock_result.returncode == 5
    assert export_lock_payload["error"]["code"] == "EXPORT_RUNNING_LOCK"

    conn = sqlite3.connect(已播种工作区 / ".hikbox" / "library.db")
    try:
        conn.execute("UPDATE export_run SET status='completed', finished_at=CURRENT_TIMESTAMP WHERE status='running'")
        conn.commit()
    finally:
        conn.close()

    illegal_state_result = 运行_cli(
        ["--json", "people", "exclude", "2", "--face-observation-id", "4", "--workspace", str(已播种工作区)]
    )
    illegal_state_payload = json.loads(illegal_state_result.stderr)
    assert illegal_state_result.returncode == 6
    assert illegal_state_payload["error"]["code"] == "ILLEGAL_STATE"

    serve_block_result = 运行_cli(["--json", "serve", "start", "--workspace", str(已播种工作区), "--port", "38765"])
    serve_block_payload = json.loads(serve_block_result.stderr)
    assert serve_block_result.returncode == 7
    assert serve_block_payload["error"]["code"] == "SERVE_BLOCKED_BY_ACTIVE_SCAN"
    assert serve_block_payload["error"]["active_session_id"] == active_session_id

    quiet_success_result = 运行_cli(["--quiet", "scan", "abort", str(active_session_id), "--workspace", str(已播种工作区)])
    quiet_error_result = 运行_cli(["--quiet", "people", "rename", "1", "   ", "--workspace", str(已播种工作区)])
    assert quiet_success_result.returncode == 0
    assert quiet_success_result.stdout == ""
    assert quiet_success_result.stderr == ""
    assert quiet_error_result.returncode == 2
    assert quiet_error_result.stdout == ""
    assert "VALIDATION_ERROR" in quiet_error_result.stderr
