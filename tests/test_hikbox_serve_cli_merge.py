from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tests.helpers import (
    find_free_port,
    spawn_hikbox,
    terminate_process,
    wait_for_http_ready,
)
from tests.serve_cli_helpers import read_merge_slice_db_snapshot


def test_serve_merge_rejects_crafted_requests_without_db_changes(
    tmp_path: Path,
    scanned_workspace: tuple[Path, Path, Path, dict[str, object], dict[str, str]],
) -> None:
    workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]
    blair_person_id = target_person_ids["target_blair"]
    winner_person_id = min(alex_person_id, casey_person_id)
    loser_person_id = casey_person_id if winner_person_id == alex_person_id else alex_person_id

    port = find_free_port()
    process = spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        wait_for_http_ready(f"{base_url}/")
        invalid_cases = [
            ({"person_id": [alex_person_id]}, "必须恰好选择 2 个人物"),
            ({"person_id": [alex_person_id, alex_person_id]}, "不能重复选择同一个人物"),
            (
                {"person_id": [alex_person_id, "00000000-0000-0000-0000-000000000000"]},
                "未找到可合并的人物",
            ),
        ]
        for payload, expected_message in invalid_cases:
            snapshot_before = read_merge_slice_db_snapshot(library_db)
            response = httpx.post(
                f"{base_url}/people/merge",
                data=payload,
                follow_redirects=False,
                timeout=5.0,
            )
            assert response.status_code == 400
            assert expected_message in response.text
            assert read_merge_slice_db_snapshot(library_db) == snapshot_before

        valid_merge_response = httpx.post(
            f"{base_url}/people/merge",
            data={"person_id": [casey_person_id, alex_person_id]},
            follow_redirects=False,
            timeout=5.0,
        )
        assert valid_merge_response.status_code == 303

        snapshot_before_inactive_attempt = read_merge_slice_db_snapshot(library_db)
        inactive_response = httpx.post(
            f"{base_url}/people/merge",
            data={"person_id": [loser_person_id, blair_person_id]},
            follow_redirects=False,
            timeout=5.0,
        )
        assert inactive_response.status_code == 400
        assert "不能合并已失效的人物" in inactive_response.text
        assert read_merge_slice_db_snapshot(library_db) == snapshot_before_inactive_attempt
    finally:
        terminate_process(process)


def test_serve_merge_rolls_back_when_fault_injection_fails_mid_transaction(
    tmp_path: Path,
    scanned_workspace: tuple[Path, Path, Path, dict[str, object], dict[str, str]],
) -> None:
    workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]
    db_snapshot_before_merge = read_merge_slice_db_snapshot(library_db)

    port = find_free_port()
    process = spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
        env_updates={"HIKBOX_TEST_MERGE_FAIL_STAGE": "after_assignment_migration"},
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        wait_for_http_ready(f"{base_url}/")
        response = httpx.post(
            f"{base_url}/people/merge",
            data={"person_id": [casey_person_id, alex_person_id]},
            follow_redirects=False,
            timeout=5.0,
        )
        assert response.status_code == 500
        assert "人物合并失败" in response.text
        assert read_merge_slice_db_snapshot(library_db) == db_snapshot_before_merge

        people_page = httpx.get(f"{base_url}/people", timeout=5.0)
        alex_detail = httpx.get(f"{base_url}/people/{alex_person_id}", timeout=5.0)
        casey_detail = httpx.get(f"{base_url}/people/{casey_person_id}", timeout=5.0)
        assert people_page.status_code == 200
        assert alex_person_id in people_page.text
        assert casey_person_id in people_page.text
        assert alex_detail.status_code == 200
        assert casey_detail.status_code == 200
    finally:
        terminate_process(process)
