from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from tests.cli.conftest import 读取_json输出


def test_audit_list_返回指定扫描会话审计项并与_db真值一致(
    seeded_workspace: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    查询多行: Callable[[Path, str, Sequence[object]], list[tuple[object, ...]]],
) -> None:
    result = 运行_cli(
        [
            "--json",
            "audit",
            "list",
            "--scan-session-id",
            "1",
            "--workspace",
            str(seeded_workspace),
        ]
    )
    payload = 读取_json输出(result.stdout)
    db_rows = 查询多行(
        seeded_workspace,
        """
        SELECT id, scan_session_id, assignment_run_id, audit_type, face_observation_id, person_id, evidence_json, created_at
        FROM scan_audit_item
        WHERE scan_session_id=1
        ORDER BY id ASC
        """,
    )

    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"]["items"] == [
        {
            "id": row[0],
            "scan_session_id": row[1],
            "assignment_run_id": row[2],
            "audit_type": row[3],
            "face_observation_id": row[4],
            "person_id": row[5],
            "evidence": {"face_observation_id": row[4], "person_id": row[5]},
            "created_at": row[7],
        }
        for row in db_rows
    ]


def test_source_list_返回活动_source并与_db真值一致(
    seeded_workspace: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    查询多行: Callable[[Path, str, Sequence[object]], list[tuple[object, ...]]],
) -> None:
    result = 运行_cli(["--json", "source", "list", "--workspace", str(seeded_workspace)])
    payload = 读取_json输出(result.stdout)
    db_rows = 查询多行(
        seeded_workspace,
        """
        SELECT id, root_path, label, enabled, removed_at, created_at, updated_at
        FROM library_source
        WHERE removed_at IS NULL
        ORDER BY id ASC
        """,
    )

    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"]["total"] == len(db_rows)
    assert payload["data"]["items"] == [
        {
            "source_id": row[0],
            "root_path": row[1],
            "label": row[2],
            "enabled": bool(row[3]),
            "removed_at": row[4],
            "created_at": row[5],
            "updated_at": row[6],
        }
        for row in db_rows
    ]
