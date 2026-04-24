from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from tests.cli.conftest import 读取_json输出


def test_scan_status_latest_返回最新会话并与_db真值一致(
    seeded_workspace: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    查询行: Callable[[Path, str, Sequence[object]], tuple[object, ...] | None],
) -> None:
    lib_db = seeded_workspace / ".hikbox" / "library.db"
    conn = sqlite3.connect(lib_db)
    try:
        conn.execute(
            """
            INSERT INTO scan_session(run_kind, status, triggered_by, created_at, updated_at)
            VALUES ('scan_full', 'completed', 'manual_cli', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        )
        conn.commit()
        latest_id = int(conn.execute("SELECT id FROM scan_session ORDER BY id DESC LIMIT 1").fetchone()[0])
    finally:
        conn.close()

    result = 运行_cli(["--json", "scan", "status", "--latest", "--workspace", str(seeded_workspace)])
    payload = 读取_json输出(result.stdout)
    db_row = 查询行(
        seeded_workspace,
        """
        SELECT id, run_kind, status, triggered_by, resume_from_session_id, started_at, finished_at, last_error, created_at, updated_at
        FROM scan_session
        WHERE id=?
        """,
        (latest_id,),
    )

    assert db_row is not None
    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"] == {
        "session_id": db_row[0],
        "run_kind": db_row[1],
        "status": db_row[2],
        "triggered_by": db_row[3],
        "resume_from_session_id": db_row[4],
        "started_at": db_row[5],
        "finished_at": db_row[6],
        "last_error": db_row[7],
        "created_at": db_row[8],
        "updated_at": db_row[9],
    }


def test_scan_status_session_id_返回指定会话并与_db真值一致(
    seeded_workspace: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    查询行: Callable[[Path, str, Sequence[object]], tuple[object, ...] | None],
) -> None:
    result = 运行_cli(["--json", "scan", "status", "--session-id", "1", "--workspace", str(seeded_workspace)])
    payload = 读取_json输出(result.stdout)
    db_row = 查询行(
        seeded_workspace,
        """
        SELECT id, run_kind, status, triggered_by, resume_from_session_id, started_at, finished_at, last_error, created_at, updated_at
        FROM scan_session
        WHERE id=1
        """,
    )

    assert db_row is not None
    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"] == {
        "session_id": db_row[0],
        "run_kind": db_row[1],
        "status": db_row[2],
        "triggered_by": db_row[3],
        "resume_from_session_id": db_row[4],
        "started_at": db_row[5],
        "finished_at": db_row[6],
        "last_error": db_row[7],
        "created_at": db_row[8],
        "updated_at": db_row[9],
    }


def test_scan_list_limit_返回受限列表并与_db真值一致(
    seeded_workspace: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    查询多行: Callable[[Path, str, Sequence[object]], list[tuple[object, ...]]],
) -> None:
    lib_db = seeded_workspace / ".hikbox" / "library.db"
    conn = sqlite3.connect(lib_db)
    try:
        conn.execute(
            """
            INSERT INTO scan_session(run_kind, status, triggered_by, created_at, updated_at)
            VALUES ('scan_incremental', 'completed', 'manual_cli', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        )
        conn.execute(
            """
            INSERT INTO scan_session(run_kind, status, triggered_by, created_at, updated_at)
            VALUES ('scan_resume', 'interrupted', 'manual_cli', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        )
        conn.commit()
    finally:
        conn.close()

    result = 运行_cli(["--json", "scan", "list", "--limit", "2", "--workspace", str(seeded_workspace)])
    payload = 读取_json输出(result.stdout)
    db_rows = 查询多行(
        seeded_workspace,
        """
        SELECT id, run_kind, status, triggered_by, resume_from_session_id, started_at, finished_at, last_error, created_at, updated_at
        FROM scan_session
        ORDER BY id DESC
        LIMIT 2
        """,
    )

    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"]["items"] == [
        {
            "session_id": row[0],
            "run_kind": row[1],
            "status": row[2],
            "triggered_by": row[3],
            "resume_from_session_id": row[4],
            "started_at": row[5],
            "finished_at": row[6],
            "last_error": row[7],
            "created_at": row[8],
            "updated_at": row[9],
        }
        for row in db_rows
    ]


def test_export_run_status_返回单次运行并与_db真值一致(
    seeded_workspace: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    查询行: Callable[[Path, str, Sequence[object]], tuple[object, ...] | None],
) -> None:
    result = 运行_cli(["--json", "export", "run-status", "1", "--workspace", str(seeded_workspace)])
    payload = 读取_json输出(result.stdout)
    db_row = 查询行(
        seeded_workspace,
        "SELECT id, template_id, status, summary_json, started_at, finished_at FROM export_run WHERE id=1",
    )

    assert db_row is not None
    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"] == {
        "export_run_id": db_row[0],
        "template_id": db_row[1],
        "status": db_row[2],
        "summary": json.loads(db_row[3]),
        "started_at": db_row[4],
        "finished_at": db_row[5],
    }


def test_export_run_status_对_running任务保持纯查询语义(
    seeded_workspace: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    查询行: Callable[[Path, str, Sequence[object]], tuple[object, ...] | None],
) -> None:
    output_root = (seeded_workspace / "exports" / "run-status-driver").resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    create_result = 运行_cli(
        [
            "--json",
            "export",
            "template",
            "create",
            "--name",
            "run-status-driver",
            "--output-root",
            str(output_root),
            "--person-ids",
            "4",
            "--workspace",
            str(seeded_workspace),
        ]
    )
    template_id = int(读取_json输出(create_result.stdout)["data"]["template_id"])
    run_result = 运行_cli(["--json", "export", "run", str(template_id), "--workspace", str(seeded_workspace)])
    export_run_id = int(读取_json输出(run_result.stdout)["data"]["export_run_id"])

    result = 运行_cli(
        ["--json", "export", "run-status", str(export_run_id), "--workspace", str(seeded_workspace)]
    )
    payload = 读取_json输出(result.stdout)
    db_row = 查询行(
        seeded_workspace,
        "SELECT status, finished_at FROM export_run WHERE id=?",
        (export_run_id,),
    )

    assert create_result.returncode == 0, create_result.stderr
    assert run_result.returncode == 0, run_result.stderr
    assert db_row is not None
    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"]["export_run_id"] == export_run_id
    assert payload["data"]["status"] == "running"
    assert payload["data"]["summary"] == {
        "exported_count": 0,
        "skipped_exists_count": 0,
        "failed_count": 0,
    }
    assert db_row == ("running", None)


def test_export_execute_显式执行导出并写入_db(
    seeded_workspace: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    查询行: Callable[[Path, str, Sequence[object]], tuple[object, ...] | None],
) -> None:
    output_root = (seeded_workspace / "exports" / "execute-driver").resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    create_result = 运行_cli(
        [
            "--json",
            "export",
            "template",
            "create",
            "--name",
            "execute-driver",
            "--output-root",
            str(output_root),
            "--person-ids",
            "4",
            "--workspace",
            str(seeded_workspace),
        ]
    )
    template_id = int(读取_json输出(create_result.stdout)["data"]["template_id"])
    run_result = 运行_cli(["--json", "export", "run", str(template_id), "--workspace", str(seeded_workspace)])
    export_run_id = int(读取_json输出(run_result.stdout)["data"]["export_run_id"])

    result = 运行_cli(
        ["--json", "export", "execute", str(export_run_id), "--workspace", str(seeded_workspace)]
    )
    payload = 读取_json输出(result.stdout)
    db_row = 查询行(
        seeded_workspace,
        "SELECT status, finished_at FROM export_run WHERE id=?",
        (export_run_id,),
    )
    delivery_row = 查询行(
        seeded_workspace,
        """
        SELECT delivery_status
        FROM export_delivery
        WHERE export_run_id=?
        ORDER BY id ASC
        LIMIT 1
        """,
        (export_run_id,),
    )

    assert create_result.returncode == 0, create_result.stderr
    assert run_result.returncode == 0, run_result.stderr
    assert db_row is not None
    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"] == {
        "export_run_id": export_run_id,
        "status": "completed",
        "exported_count": 1,
        "skipped_exists_count": 0,
        "failed_count": 0,
    }
    assert db_row[0] == "completed"
    assert db_row[1] is not None
    assert delivery_row == ("exported",)


def test_export_run_list_按模板与_limit过滤并与_db真值一致(
    seeded_workspace: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    查询多行: Callable[[Path, str, Sequence[object]], list[tuple[object, ...]]],
) -> None:
    output_root = (seeded_workspace / "exports" / "for-run-list").resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    create_result = 运行_cli(
        [
            "--json",
            "export",
            "template",
            "create",
            "--name",
            "for-run-list",
            "--output-root",
            str(output_root),
            "--person-ids",
            "6",
            "--workspace",
            str(seeded_workspace),
        ]
    )
    template_id = int(读取_json输出(create_result.stdout)["data"]["template_id"])
    run_result = 运行_cli(["--json", "export", "run", str(template_id), "--workspace", str(seeded_workspace)])
    assert run_result.returncode == 0

    result = 运行_cli(
        [
            "--json",
            "export",
            "run-list",
            "--template-id",
            str(template_id),
            "--limit",
            "1",
            "--workspace",
            str(seeded_workspace),
        ]
    )
    payload = 读取_json输出(result.stdout)
    db_rows = 查询多行(
        seeded_workspace,
        """
        SELECT id, template_id, status, summary_json, started_at, finished_at
        FROM export_run
        WHERE template_id=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (template_id,),
    )

    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"]["items"] == [
        {
            "export_run_id": row[0],
            "template_id": row[1],
            "status": row[2],
            "summary": json.loads(row[3]),
            "started_at": row[4],
            "finished_at": row[5],
        }
        for row in db_rows
    ]


def test_logs_list_按过滤条件返回事件并与_db真值一致(
    seeded_workspace: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    查询多行: Callable[[Path, str, Sequence[object]], list[tuple[object, ...]]],
) -> None:
    lib_db = seeded_workspace / ".hikbox" / "library.db"
    conn = sqlite3.connect(lib_db)
    try:
        conn.execute(
            """
            INSERT INTO ops_event(event_type, severity, scan_session_id, export_run_id, payload_json, created_at)
            VALUES ('cli_probe', 'warning', 1, 1, ?, CURRENT_TIMESTAMP)
            """,
            ('{"probe":"target"}',),
        )
        conn.execute(
            """
            INSERT INTO ops_event(event_type, severity, scan_session_id, export_run_id, payload_json, created_at)
            VALUES ('cli_probe_other', 'info', 1, 1, ?, CURRENT_TIMESTAMP)
            """,
            ('{"probe":"other"}',),
        )
        conn.commit()
    finally:
        conn.close()

    result = 运行_cli(
        [
            "--json",
            "logs",
            "list",
            "--scan-session-id",
            "1",
            "--export-run-id",
            "1",
            "--severity",
            "warning",
            "--limit",
            "1",
            "--workspace",
            str(seeded_workspace),
        ]
    )
    payload = 读取_json输出(result.stdout)
    db_rows = 查询多行(
        seeded_workspace,
        """
        SELECT id, event_type, severity, scan_session_id, export_run_id, payload_json, created_at
        FROM ops_event
        WHERE event_type != 'audit.freeze'
          AND scan_session_id=1
          AND export_run_id=1
          AND severity='warning'
        ORDER BY id DESC
        LIMIT 1
        """,
    )

    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"]["items"] == [
        {
            "id": row[0],
            "event_type": row[1],
            "severity": row[2],
            "scan_session_id": row[3],
            "export_run_id": row[4],
            "payload": json.loads(row[5]),
            "created_at": row[6],
        }
        for row in db_rows
    ]
