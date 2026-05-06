from __future__ import annotations

from pathlib import Path
import shutil
import sqlite3

import httpx
import pytest

from tests.helpers import (
    FIXTURE_DIR,
    add_source,
    fetch_all,
    find_free_port,
    run_hikbox,
    spawn_hikbox,
    terminate_process,
    wait_for_http_ready,
)
from tests.serve_cli_helpers import (
    read_merge_slice_db_snapshot,
    read_person_page_status,
    read_person_write_revision,
)


def test_serve_undo_rejects_incomplete_merge_snapshot_without_db_changes(
    tmp_path: Path,
    scanned_workspace: tuple[Path, Path, Path, dict[str, object], dict[str, str]],
) -> None:
    workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]

    merge_port = find_free_port()
    merge_process = spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(merge_port),
    )
    merge_base_url = f"http://127.0.0.1:{merge_port}"
    try:
        wait_for_http_ready(f"{merge_base_url}/")
        merge_response = httpx.post(
            f"{merge_base_url}/people/merge",
            data={"person_id": [casey_person_id, alex_person_id]},
            follow_redirects=False,
            timeout=5.0,
        )
        assert merge_response.status_code == 303
    finally:
        terminate_process(merge_process)

    snapshot_before_undo_attempt = read_merge_slice_db_snapshot(library_db)

    port = find_free_port()
    process = spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
        env_updates={"HIKBOX_TEST_BREAK_LATEST_MERGE_SNAPSHOT": "1"},
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        wait_for_http_ready(f"{base_url}/")
        response = httpx.post(
            f"{base_url}/people/merge/undo",
            follow_redirects=False,
            timeout=5.0,
        )
        assert response.status_code == 400
        assert "最近一次合并快照不完整" in response.text
        assert read_merge_slice_db_snapshot(library_db) == snapshot_before_undo_attempt
        assert read_person_page_status(base_url, alex_person_id) in {200, 404}
        assert read_person_page_status(base_url, casey_person_id) in {200, 404}
    finally:
        terminate_process(process)


def test_serve_undo_rejects_after_scan_invalidation_deletes_winner_assignment(
    tmp_path: Path,
    scanned_workspace: tuple[Path, Path, Path, dict[str, object], dict[str, str]],
) -> None:
    workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
    source_dir = tmp_path / "scan-source"
    shutil.copytree(FIXTURE_DIR, source_dir)
    # 将 library_sources.path 更新为可写副本
    connection = sqlite3.connect(str(library_db))
    try:
        with connection:
            connection.execute(
                "UPDATE library_sources SET path = ?, scan_state = 'pending' WHERE id = 1",
                (str(source_dir.resolve()),),
            )
    finally:
        connection.close()
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]
    winner_person_id = min(alex_person_id, casey_person_id)

    merge_port = find_free_port()
    merge_process = spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(merge_port),
    )
    merge_base_url = f"http://127.0.0.1:{merge_port}"
    try:
        wait_for_http_ready(f"{merge_base_url}/")
        merge_response = httpx.post(
            f"{merge_base_url}/people/merge",
            data={"person_id": [casey_person_id, alex_person_id]},
            follow_redirects=False,
            timeout=5.0,
        )
        assert merge_response.status_code == 303
    finally:
        terminate_process(merge_process)

    merge_operation_row = fetch_all(
        library_db,
        """
        SELECT winner_write_revision_after_merge
        FROM person_merge_operations
        ORDER BY id DESC
        LIMIT 1
        """,
    )[0]
    winner_revision_after_merge = int(merge_operation_row[0])
    target_file = next(
        str(asset["file"])
        for asset in manifest["assets"]
        if asset["expected_target_people"] == ["target_alex"]
    )
    (source_dir / target_file).write_bytes(b"not-a-valid-image-anymore")

    rescan_result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert rescan_result.returncode == 0, rescan_result.stderr
    assert read_person_write_revision(library_db, winner_person_id) > winner_revision_after_merge

    undo_snapshot_before_attempt = read_merge_slice_db_snapshot(library_db)
    undo_port = find_free_port()
    undo_process = spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(undo_port),
    )
    undo_base_url = f"http://127.0.0.1:{undo_port}"
    try:
        wait_for_http_ready(f"{undo_base_url}/")
        undo_response = httpx.post(
            f"{undo_base_url}/people/merge/undo",
            follow_redirects=False,
            timeout=5.0,
        )
        assert undo_response.status_code == 400
        assert "合并之后已发生新的人物相关写入" in undo_response.text
        assert read_merge_slice_db_snapshot(library_db) == undo_snapshot_before_attempt
    finally:
        terminate_process(undo_process)
