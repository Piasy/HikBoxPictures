from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hikbox_pictures.product.audit.service import AssignmentAuditInput, AuditSamplingService

from .conftest import create_scan_session, query_one, run_cli


def _insert_assignment_run(db_path: Path, *, scan_session_id: int) -> int:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO assignment_run(
              scan_session_id,
              algorithm_version,
              param_snapshot_json,
              run_kind,
              started_at,
              finished_at,
              status
            )
            VALUES (?, 'v5.2026-04-21', '{}', 'scan_full', '2026-04-22T00:00:00+00:00', '2026-04-22T00:01:00+00:00', 'completed')
            """,
            (scan_session_id,),
        )
        conn.commit()
        return int(cursor.lastrowid)


def test_audit_list_and_source_list(cli_bin: str, seeded_workspace: Path) -> None:
    db_path = seeded_workspace / ".hikbox" / "library.db"
    session_id = create_scan_session(seeded_workspace, status="completed")
    run_id = _insert_assignment_run(db_path, scan_session_id=session_id)

    with sqlite3.connect(db_path) as conn:
        face_id = int(conn.execute("SELECT id FROM face_observation ORDER BY id LIMIT 1").fetchone()[0])
        person_id = int(conn.execute("SELECT id FROM person WHERE is_named=1 ORDER BY id LIMIT 1").fetchone()[0])

    AuditSamplingService(db_path).sample_assignment_run(
        scan_session_id=session_id,
        assignment_run_id=run_id,
        assignments=[
            AssignmentAuditInput(
                face_observation_id=face_id,
                person_id=person_id,
                assignment_source="hdbscan",
                margin=0.01,
                reassign_after_exclusion=True,
                new_anonymous_person=True,
                evidence={"sample": "yes"},
            )
        ],
    )

    audit_list = run_cli(
        cli_bin,
        "--json",
        "audit",
        "list",
        "--scan-session-id",
        str(session_id),
        "--workspace",
        str(seeded_workspace),
    )
    audit_items = json.loads(audit_list.stdout)["data"]["items"]
    db_audit_count = query_one(seeded_workspace, "SELECT COUNT(*) FROM scan_audit_item WHERE scan_session_id=?", [session_id])[0]
    assert audit_list.returncode == 0
    assert len(audit_items) == db_audit_count
    with sqlite3.connect(db_path) as conn:
        db_rows = conn.execute(
            """
            SELECT audit_type, face_observation_id, person_id, evidence_json
            FROM scan_audit_item
            WHERE scan_session_id=?
            ORDER BY id DESC
            """,
            [session_id],
        ).fetchall()
    expected = [
        {
            "audit_type": str(row[0]),
            "face_observation_id": int(row[1]),
            "person_id": None if row[2] is None else int(row[2]),
            "evidence_json": json.loads(str(row[3])),
        }
        for row in db_rows
    ]
    assert audit_items == expected

    source_list = run_cli(cli_bin, "--json", "source", "list", "--workspace", str(seeded_workspace))
    source_items = json.loads(source_list.stdout)["data"]["items"]
    db_source_count = query_one(seeded_workspace, "SELECT COUNT(*) FROM library_source")[0]
    assert source_list.returncode == 0
    assert len(source_items) == db_source_count
    with sqlite3.connect(db_path) as conn:
        db_sources = {
            row[0]: (row[1], row[2], bool(row[3]))
            for row in conn.execute("SELECT id, root_path, label, enabled FROM library_source")
        }
    for item in source_items:
        root_path, label, enabled = db_sources[item["source_id"]]
        assert item["root_path"] == root_path
        assert item["label"] == label
        assert item["enabled"] == enabled
