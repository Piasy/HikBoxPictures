from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tests.helpers import (
    fetch_all,
    find_free_port,
    spawn_hikbox,
    terminate_process,
    wait_for_http_ready,
)
from tests.serve_cli_helpers import (
    read_merge_slice_db_snapshot,
    read_person_face_exclusions,
)


def test_serve_exclude_rejects_crafted_requests_without_db_changes(
    tmp_path: Path,
    scanned_workspace: tuple[Path, Path, Path, dict[str, object], dict[str, str]],
) -> None:
    workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
    alex_person_id = target_person_ids["target_alex"]
    blair_person_id = target_person_ids["target_blair"]
    alex_assignment_ids = fetch_all(
        library_db,
        """
        SELECT id
        FROM person_face_assignments
        WHERE person_id = ?
          AND active = 1
        ORDER BY id ASC
        """,
        (alex_person_id,),
    )
    blair_assignment_ids = fetch_all(
        library_db,
        """
        SELECT id
        FROM person_face_assignments
        WHERE person_id = ?
          AND active = 1
        ORDER BY id ASC
        """,
        (blair_person_id,),
    )
    valid_assignment_id = int(alex_assignment_ids[0][0])
    foreign_assignment_id = int(blair_assignment_ids[0][0])

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
            (
                f"{base_url}/people/00000000-0000-0000-0000-000000000000/exclude",
                {"assignment_id": [str(valid_assignment_id)]},
                {400, 404},
                "未找到",
            ),
            (
                f"{base_url}/people/{alex_person_id}/exclude",
                {},
                {400},
                "至少选择",
            ),
            (
                f"{base_url}/people/{alex_person_id}/exclude",
                {"assignment_id": [str(valid_assignment_id), str(valid_assignment_id)]},
                {400},
                "重复",
            ),
            (
                f"{base_url}/people/{alex_person_id}/exclude",
                {"assignment_id": ["999999999"]},
                {400},
                "未找到",
            ),
            (
                f"{base_url}/people/{alex_person_id}/exclude",
                {"assignment_id": [str(foreign_assignment_id)]},
                {400},
                "不属于当前人物",
            ),
        ]
        for url, payload, expected_statuses, expected_message in invalid_cases:
            snapshot_before = read_merge_slice_db_snapshot(library_db)
            response = httpx.post(
                url,
                data=payload,
                follow_redirects=False,
                timeout=5.0,
            )
            assert response.status_code in expected_statuses
            assert expected_message in response.text
            assert read_merge_slice_db_snapshot(library_db) == snapshot_before

        valid_response = httpx.post(
            f"{base_url}/people/{alex_person_id}/exclude",
            data={"assignment_id": [str(valid_assignment_id)]},
            follow_redirects=False,
            timeout=5.0,
        )
        assert valid_response.status_code == 303

        snapshot_before_replay = read_merge_slice_db_snapshot(library_db)
        replay_response = httpx.post(
            f"{base_url}/people/{alex_person_id}/exclude",
            data={"assignment_id": [str(valid_assignment_id)]},
            follow_redirects=False,
            timeout=5.0,
        )
        assert replay_response.status_code == 400
        assert "active" in replay_response.text or "未找到" in replay_response.text
        assert read_merge_slice_db_snapshot(library_db) == snapshot_before_replay
    finally:
        terminate_process(process)


def test_serve_exclude_rolls_back_when_fault_injection_fails_mid_transaction(
    tmp_path: Path,
    scanned_workspace: tuple[Path, Path, Path, dict[str, object], dict[str, str]],
) -> None:
    workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
    blair_person_id = target_person_ids["target_blair"]
    assignment_ids = [
        int(assignment_id)
        for assignment_id, in fetch_all(
            library_db,
            """
            SELECT id
            FROM person_face_assignments
            WHERE person_id = ?
              AND active = 1
            ORDER BY id ASC
            LIMIT 2
            """,
            (blair_person_id,),
        )
    ]
    assert len(assignment_ids) == 2
    snapshot_before = read_merge_slice_db_snapshot(library_db)

    port = find_free_port()
    process = spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
        env_updates={"HIKBOX_TEST_EXCLUSION_FAIL_STAGE": "after_first_exclusion_insert"},
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        wait_for_http_ready(f"{base_url}/")
        response = httpx.post(
            f"{base_url}/people/{blair_person_id}/exclude",
            data={"assignment_id": [str(assignment_id) for assignment_id in assignment_ids]},
            follow_redirects=False,
            timeout=5.0,
        )
        assert response.status_code == 500
        assert "排除失败" in response.text
        assert read_merge_slice_db_snapshot(library_db) == snapshot_before

        detail_response = httpx.get(f"{base_url}/people/{blair_person_id}", timeout=5.0)
        assert detail_response.status_code == 200
        for assignment_id in assignment_ids:
            assert f'data-assignment-id="{assignment_id}"' in detail_response.text
    finally:
        terminate_process(process)


def test_serve_exclude_invalidates_latest_merge_undo_with_real_http(
    tmp_path: Path,
    scanned_workspace: tuple[Path, Path, Path, dict[str, object], dict[str, str]],
) -> None:
    workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]
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
        merge_response = httpx.post(
            f"{base_url}/people/merge",
            data={"person_id": [casey_person_id, alex_person_id]},
            follow_redirects=False,
            timeout=5.0,
        )
        assert merge_response.status_code == 303

        winner_assignment_row = fetch_all(
            library_db,
            """
            SELECT id, face_observation_id
            FROM person_face_assignments
            WHERE person_id = ?
              AND active = 1
            ORDER BY id ASC
            LIMIT 1
            """,
            (winner_person_id,),
        )[0]
        assignment_id = int(winner_assignment_row[0])
        face_observation_id = int(winner_assignment_row[1])
        exclude_response = httpx.post(
            f"{base_url}/people/{winner_person_id}/exclude",
            data={"assignment_id": [str(assignment_id)]},
            follow_redirects=False,
            timeout=5.0,
        )
        assert exclude_response.status_code == 303
        exclusion_rows = read_person_face_exclusions(
            library_db,
            face_observation_id=face_observation_id,
            excluded_person_id=winner_person_id,
        )
        assert len(exclusion_rows) == 1

        snapshot_before_undo = read_merge_slice_db_snapshot(library_db)
        undo_response = httpx.post(
            f"{base_url}/people/merge/undo",
            follow_redirects=False,
            timeout=5.0,
        )
        assert undo_response.status_code == 400
        assert "合并之后已发生新的人物相关写入" in undo_response.text
        assert read_merge_slice_db_snapshot(library_db) == snapshot_before_undo
        merge_rows = fetch_all(
            library_db,
            """
            SELECT winner_person_id, loser_person_id, undone_at
            FROM person_merge_operations
            ORDER BY id ASC
            """,
        )
        assert merge_rows == [(winner_person_id, loser_person_id, None)]
    finally:
        terminate_process(process)
