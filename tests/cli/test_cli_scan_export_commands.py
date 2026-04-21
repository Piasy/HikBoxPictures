from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .conftest import run_cli


def test_scan_status_list_export_run_status_and_run_list(cli_bin: str, workspace: Path) -> None:
    assert run_cli(cli_bin, "init", "--workspace", str(workspace)).returncode == 0
    lib_db = workspace / ".hikbox" / "library.db"

    start = run_cli(cli_bin, "--json", "scan", "start-or-resume", "--workspace", str(workspace))
    assert start.returncode == 0
    session_id = int(json.loads(start.stdout)["data"]["session_id"])

    with sqlite3.connect(lib_db) as conn:
        conn.execute(
            """
            INSERT INTO person(person_uuid, display_name, is_named, status, merged_into_person_id, created_at, updated_at)
            VALUES ('00000000-0000-0000-0000-000000000201', '命名人物', 1, 'active', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        )
        conn.execute(
            "UPDATE scan_session SET status='completed', finished_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            [session_id],
        )
        conn.execute(
            "INSERT INTO scan_session(run_kind,status,triggered_by,created_at,updated_at) VALUES ('scan_full','completed','manual_cli',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
        )
        conn.execute(
            "INSERT INTO scan_session(run_kind,status,triggered_by,created_at,updated_at) VALUES ('scan_full','completed','manual_cli',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
        )
        conn.commit()
        latest_seed_id = int(conn.execute("SELECT id FROM scan_session ORDER BY id DESC LIMIT 1").fetchone()[0])

    status_latest = run_cli(cli_bin, "--json", "scan", "status", "--latest", "--workspace", str(workspace))
    assert status_latest.returncode == 0
    latest_data = json.loads(status_latest.stdout)["data"]
    with sqlite3.connect(lib_db) as conn:
        db_latest = conn.execute(
            "SELECT id, run_kind, status, triggered_by, created_at, updated_at FROM scan_session WHERE id=?",
            [latest_seed_id],
        ).fetchone()
    assert db_latest is not None
    assert latest_data == {
        "session_id": int(db_latest[0]),
        "run_kind": str(db_latest[1]),
        "status": str(db_latest[2]),
        "triggered_by": str(db_latest[3]),
        "created_at": str(db_latest[4]),
        "updated_at": str(db_latest[5]),
    }

    status = run_cli(cli_bin, "--json", "scan", "status", "--session-id", str(session_id), "--workspace", str(workspace))
    assert status.returncode == 0
    status_data = json.loads(status.stdout)["data"]
    with sqlite3.connect(lib_db) as conn:
        db_target = conn.execute(
            "SELECT id, run_kind, status, triggered_by, created_at, updated_at FROM scan_session WHERE id=?",
            [session_id],
        ).fetchone()
    assert db_target is not None
    assert status_data == {
        "session_id": int(db_target[0]),
        "run_kind": str(db_target[1]),
        "status": str(db_target[2]),
        "triggered_by": str(db_target[3]),
        "created_at": str(db_target[4]),
        "updated_at": str(db_target[5]),
    }

    scan_list = run_cli(cli_bin, "--json", "scan", "list", "--limit", "2", "--workspace", str(workspace))
    assert scan_list.returncode == 0
    scan_items = json.loads(scan_list.stdout)["data"]["items"]
    with sqlite3.connect(lib_db) as conn:
        db_scan_rows = conn.execute(
            """
            SELECT id, run_kind, status, triggered_by, created_at, updated_at
            FROM scan_session
            ORDER BY id DESC
            LIMIT 2
            """
        ).fetchall()
    expected_scan_items = [
        {
            "session_id": int(row[0]),
            "run_kind": str(row[1]),
            "status": str(row[2]),
            "triggered_by": str(row[3]),
            "created_at": str(row[4]),
            "updated_at": str(row[5]),
        }
        for row in db_scan_rows
    ]
    assert scan_items == expected_scan_items

    output_root = (workspace / "exports" / "for-run-status").resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    create_tpl = run_cli(
        cli_bin,
        "--json",
        "export",
        "template",
        "create",
        "--name",
        "for-run-status",
        "--output-root",
        str(output_root),
        "--person-ids",
        "1",
        "--workspace",
        str(workspace),
    )
    assert create_tpl.returncode == 0
    template_id = int(json.loads(create_tpl.stdout)["data"]["template_id"])
    run_export = run_cli(cli_bin, "--json", "export", "run", str(template_id), "--workspace", str(workspace))
    assert run_export.returncode == 0
    export_run_id = int(json.loads(run_export.stdout)["data"]["export_run_id"])

    run_status = run_cli(cli_bin, "--json", "export", "run-status", str(export_run_id), "--workspace", str(workspace))
    assert run_status.returncode == 0
    run_status_data = json.loads(run_status.stdout)["data"]
    with sqlite3.connect(lib_db) as conn:
        db_run = conn.execute(
            "SELECT id, template_id, status, summary_json, started_at, finished_at FROM export_run WHERE id=?",
            [export_run_id],
        ).fetchone()
    assert db_run is not None
    assert run_status_data == {
        "export_run_id": int(db_run[0]),
        "template_id": int(db_run[1]),
        "status": str(db_run[2]),
        "summary_json": json.loads(str(db_run[3]) if db_run[3] is not None else "{}"),
        "started_at": str(db_run[4]),
        "finished_at": None if db_run[5] is None else str(db_run[5]),
    }

    run_list = run_cli(
        cli_bin,
        "--json",
        "export",
        "run-list",
        "--template-id",
        str(template_id),
        "--limit",
        "1",
        "--workspace",
        str(workspace),
    )
    assert run_list.returncode == 0
    run_items = json.loads(run_list.stdout)["data"]["items"]
    with sqlite3.connect(lib_db) as conn:
        db_run_rows = conn.execute(
            """
            SELECT id, template_id, status, summary_json, started_at, finished_at
            FROM export_run
            WHERE template_id=?
            ORDER BY id DESC
            LIMIT 1
            """,
            [template_id],
        ).fetchall()
    expected_run_items = [
        {
            "export_run_id": int(row[0]),
            "template_id": int(row[1]),
            "status": str(row[2]),
            "summary_json": json.loads(str(row[3]) if row[3] is not None else "{}"),
            "started_at": str(row[4]),
            "finished_at": None if row[5] is None else str(row[5]),
        }
        for row in db_run_rows
    ]
    assert run_items == expected_run_items
