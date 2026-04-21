from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .conftest import create_scan_session, run_cli


def test_validation_not_found_scan_conflict_export_lock_illegal_state_and_serve_block_codes(
    cli_bin: str,
    seeded_workspace: Path,
) -> None:
    validation = run_cli(
        cli_bin,
        "people",
        "merge",
        "--selected-person-ids",
        "",
        "--workspace",
        str(seeded_workspace),
    )
    assert validation.returncode == 2

    not_found = run_cli(cli_bin, "scan", "abort", "999999", "--workspace", str(seeded_workspace))
    assert not_found.returncode == 3

    running_id = create_scan_session(seeded_workspace, status="running")
    scan_conflict = run_cli(cli_bin, "scan", "start-new", "--workspace", str(seeded_workspace))
    assert scan_conflict.returncode == 4

    serve_blocked = run_cli(cli_bin, "serve", "start", "--workspace", str(seeded_workspace), "--port", "38765")
    assert serve_blocked.returncode == 7

    db_path = seeded_workspace / ".hikbox" / "library.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE scan_session SET status='completed', updated_at=CURRENT_TIMESTAMP WHERE id=?", [running_id])
        template_id = int(
            conn.execute(
                "INSERT INTO export_template(name, output_root, enabled, created_at, updated_at) VALUES ('lock-tpl','/tmp/lock',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            ).lastrowid
        )
        conn.execute(
            "INSERT INTO export_template_person(template_id, person_id, created_at) VALUES (?, 1, CURRENT_TIMESTAMP)",
            [template_id],
        )
        conn.execute(
            "INSERT INTO export_run(template_id, status, summary_json, started_at, finished_at) VALUES (?, 'running', '{}', CURRENT_TIMESTAMP, NULL)",
            [template_id],
        )
        conn.commit()

    export_lock = run_cli(cli_bin, "people", "rename", "1", "锁期间改名", "--workspace", str(seeded_workspace))
    assert export_lock.returncode == 5

    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE export_run SET status='completed', finished_at=CURRENT_TIMESTAMP")
        conn.commit()

    face_id = int(
        sqlite3.connect(db_path).execute(
            "SELECT face_observation_id FROM person_face_assignment WHERE person_id=1 AND active=1 ORDER BY id LIMIT 1"
        ).fetchone()[0]
    )
    first_exclude = run_cli(
        cli_bin,
        "people",
        "exclude",
        "1",
        "--face-observation-id",
        str(face_id),
        "--workspace",
        str(seeded_workspace),
    )
    assert first_exclude.returncode == 0
    illegal = run_cli(
        cli_bin,
        "people",
        "exclude",
        "1",
        "--face-observation-id",
        str(face_id),
        "--workspace",
        str(seeded_workspace),
    )
    assert illegal.returncode == 6


def test_argparse_error_uses_json_error_contract(cli_bin: str, seeded_workspace: Path) -> None:
    proc = run_cli(cli_bin, "--json", "scan", "abort", "--workspace", str(seeded_workspace))
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "usage:" not in proc.stderr.lower()
    payload = json.loads(proc.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "VALIDATION_ERROR"
    assert "session_id" in str(payload["error"]["message"])


def test_argparse_error_uses_quiet_error_contract_without_usage_leak(cli_bin: str, seeded_workspace: Path) -> None:
    proc = run_cli(cli_bin, "--quiet", "scan", "abort", "--workspace", str(seeded_workspace))
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "VALIDATION_ERROR" in proc.stderr
    assert "session_id" in proc.stderr
    assert "usage:" not in proc.stderr.lower()
