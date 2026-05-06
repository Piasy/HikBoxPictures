"""从 test_hikbox_scan_cli.py 拆分的进程 kill 恢复测试。

原 parametrize 测试拆为 3 个独立测试函数，每个只测试一种信号。
"""

from __future__ import annotations

import json
import signal
from pathlib import Path
import time

import pytest

from tests.helpers import (
    FIXTURE_DIR,
    add_source,
    fetch_all,
    init_workspace,
    run_hikbox,
    spawn_hikbox,
    terminate_process,
)
from tests.scan_cli_helpers import (
    count_rows,
    count_batch_completed_events,
    fetch_one,
    wait_for_batch_status,
)


def test_scan_start_recovers_from_sigterm_without_rerunning_completed_batches(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-sigterm"
    external_root = tmp_path / "external-root-sigterm"
    init_result = init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    add_result = add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0

    process = spawn_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    library_db = workspace / ".hikbox" / "library.db"
    try:
        wait_for_batch_status(library_db, batch_index=2, expected_status="running")
        assert fetch_one(
            library_db,
            "SELECT status FROM scan_batches WHERE batch_index = 1",
        )[0] == "completed"
        process.send_signal(signal.SIGTERM)
        stdout_text, stderr_text = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=30)

    assert process.returncode != 0, (stdout_text, stderr_text)
    assert fetch_one(
        library_db,
        "SELECT status FROM scan_batches WHERE batch_index = 1",
    )[0] == "completed"
    assert fetch_one(
        library_db,
        "SELECT status FROM scan_batches WHERE batch_index = 2",
    )[0] != "completed"

    rerun_result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert rerun_result.returncode == 0, rerun_result.stderr
    assert fetch_one(
        library_db,
        """
        SELECT total_batches, completed_batches
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    ) == (6, 6)
    assert count_rows(workspace / ".hikbox" / "embedding.db", "face_embeddings") == count_rows(
        library_db,
        "face_observations",
    )
    batch_completed_events = count_batch_completed_events(
        external_root / "logs" / "scan.log.jsonl",
        batch_index=1,
    )
    assert batch_completed_events == 1


def test_scan_start_recovers_from_sigint_without_rerunning_completed_batches(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-sigint"
    external_root = tmp_path / "external-root-sigint"
    init_result = init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    add_result = add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0

    process = spawn_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    library_db = workspace / ".hikbox" / "library.db"
    try:
        wait_for_batch_status(library_db, batch_index=2, expected_status="running")
        assert fetch_one(
            library_db,
            "SELECT status FROM scan_batches WHERE batch_index = 1",
        )[0] == "completed"
        process.send_signal(signal.SIGINT)
        stdout_text, stderr_text = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=30)

    assert process.returncode != 0, (stdout_text, stderr_text)
    assert fetch_one(
        library_db,
        "SELECT status FROM scan_batches WHERE batch_index = 1",
    )[0] == "completed"
    assert fetch_one(
        library_db,
        "SELECT status FROM scan_batches WHERE batch_index = 2",
    )[0] != "completed"

    rerun_result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert rerun_result.returncode == 0, rerun_result.stderr
    assert fetch_one(
        library_db,
        """
        SELECT total_batches, completed_batches
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    ) == (6, 6)
    assert count_rows(workspace / ".hikbox" / "embedding.db", "face_embeddings") == count_rows(
        library_db,
        "face_observations",
    )
    batch_completed_events = count_batch_completed_events(
        external_root / "logs" / "scan.log.jsonl",
        batch_index=1,
    )
    assert batch_completed_events == 1


def test_scan_start_recovers_from_sigkill_without_rerunning_completed_batches(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-sigkill"
    external_root = tmp_path / "external-root-sigkill"
    init_result = init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    add_result = add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0

    process = spawn_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    library_db = workspace / ".hikbox" / "library.db"
    try:
        wait_for_batch_status(library_db, batch_index=2, expected_status="running")
        assert fetch_one(
            library_db,
            "SELECT status FROM scan_batches WHERE batch_index = 1",
        )[0] == "completed"
        process.send_signal(signal.SIGKILL)
        stdout_text, stderr_text = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=30)

    assert process.returncode != 0, (stdout_text, stderr_text)
    assert fetch_one(
        library_db,
        "SELECT status FROM scan_batches WHERE batch_index = 1",
    )[0] == "completed"
    assert fetch_one(
        library_db,
        "SELECT status FROM scan_batches WHERE batch_index = 2",
    )[0] != "completed"

    rerun_result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert rerun_result.returncode == 0, rerun_result.stderr
    assert fetch_one(
        library_db,
        """
        SELECT total_batches, completed_batches
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    ) == (6, 6)
    assert count_rows(workspace / ".hikbox" / "embedding.db", "face_embeddings") == count_rows(
        library_db,
        "face_observations",
    )
    batch_completed_events = count_batch_completed_events(
        external_root / "logs" / "scan.log.jsonl",
        batch_index=1,
    )
    assert batch_completed_events == 1
