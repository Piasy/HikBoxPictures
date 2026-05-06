from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
    read_person_page_status,
)


def test_serve_undo_rejects_crafted_request_when_no_merge_exists(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-undo-no-merge"
    external_root = tmp_path / "external-root-undo-no-merge"
    from tests.helpers import init_workspace
    init_result = init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    library_db = workspace / ".hikbox" / "library.db"
    snapshot_before = read_merge_slice_db_snapshot(library_db)

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
        response = httpx.post(
            f"{base_url}/people/merge/undo",
            follow_redirects=False,
            timeout=5.0,
        )
        assert response.status_code == 400
        assert "当前没有可撤销的最近一次合并" in response.text
        assert read_merge_slice_db_snapshot(library_db) == snapshot_before
    finally:
        terminate_process(process)


def test_serve_undo_rolls_back_when_fault_injection_fails_mid_transaction(
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

    db_snapshot_before_undo_attempt = read_merge_slice_db_snapshot(library_db)

    fault_port = find_free_port()
    fault_process = spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(fault_port),
        env_updates={"HIKBOX_TEST_UNDO_FAIL_STAGE": "after_assignment_restore"},
    )
    fault_base_url = f"http://127.0.0.1:{fault_port}"
    try:
        wait_for_http_ready(f"{fault_base_url}/")
        response = httpx.post(
            f"{fault_base_url}/people/merge/undo",
            follow_redirects=False,
            timeout=5.0,
        )
        assert response.status_code == 500
        assert "撤销最近一次合并失败" in response.text
        assert read_merge_slice_db_snapshot(library_db) == db_snapshot_before_undo_attempt

        merge_operations = fetch_all(
            library_db,
            """
            SELECT id, undone_at
            FROM person_merge_operations
            ORDER BY id ASC
            """,
        )
        assert len(merge_operations) == 1
        assert merge_operations[0][1] is None
    finally:
        terminate_process(fault_process)

    success_port = find_free_port()
    success_process = spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(success_port),
    )
    success_base_url = f"http://127.0.0.1:{success_port}"
    try:
        wait_for_http_ready(f"{success_base_url}/")
        success_response = httpx.post(
            f"{success_base_url}/people/merge/undo",
            follow_redirects=False,
            timeout=5.0,
        )
        assert success_response.status_code == 303
        people_page = httpx.get(f"{success_base_url}/people", timeout=5.0)
        assert people_page.status_code == 200
        assert read_person_page_status(success_base_url, alex_person_id) == 200
        assert read_person_page_status(success_base_url, casey_person_id) == 200
        merge_operations = fetch_all(
            library_db,
            """
            SELECT id, undone_at
            FROM person_merge_operations
            ORDER BY id ASC
            """,
        )
        assert len(merge_operations) == 1
        assert merge_operations[0][1] is not None
    finally:
        terminate_process(success_process)


def test_serve_undo_allows_only_one_real_rollback_under_concurrency(
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

    trace_file = tmp_path / ".tmp" / "people-gallery-merge-undo" / "undo-overlap-trace.log"
    port = find_free_port()
    process = spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
        env_updates={
            "HIKBOX_TEST_UNDO_HOLD_SECONDS": "0.5",
            "HIKBOX_TEST_UNDO_TRACE_FILE": str(trace_file),
        },
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        wait_for_http_ready(f"{base_url}/")

        def _post_undo() -> httpx.Response:
            return httpx.post(
                f"{base_url}/people/merge/undo",
                follow_redirects=False,
                timeout=10.0,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda _: _post_undo(), range(2)))

        status_codes = sorted(response.status_code for response in responses)
        assert status_codes == [303, 400]
        error_response = next(response for response in responses if response.status_code == 400)
        assert "最近一次成功合并已经撤销" in error_response.text or "当前没有可撤销的最近一次合并" in error_response.text
        trace_lines = trace_file.read_text(encoding="utf-8").splitlines()
        trace_events = [line.rsplit(" ", maxsplit=1)[1] for line in trace_lines]
        assert trace_events.count("handler_enter") == 2, trace_lines
        first_terminal_index = min(
            index
            for index, event in enumerate(trace_events)
            if event in {"request_succeeded", "validation_failed", "request_failed"}
        )
        second_handler_enter_index = [
            index for index, event in enumerate(trace_events) if event == "handler_enter"
        ][1]
        assert second_handler_enter_index < first_terminal_index, trace_lines

        merge_rows = fetch_all(
            library_db,
            """
            SELECT id, winner_person_id, loser_person_id, undone_at
            FROM person_merge_operations
            ORDER BY id ASC
            """,
        )
        assert len(merge_rows) == 1
        assert merge_rows[0][3] is not None
        assert read_person_page_status(base_url, alex_person_id) == 200
        assert read_person_page_status(base_url, casey_person_id) == 200
    finally:
        terminate_process(process)
