from __future__ import annotations

import signal
from pathlib import Path

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
    count_rows_matching,
    fetch_one,
    wait_for_batch_status,
    write_named_source_copies,
)


def test_scan_start_recovers_killed_batch_and_downgrades_missing_file_to_asset_failure(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace-missing-file-recovery"
    external_root = tmp_path / "external-root-missing-file-recovery"
    source_dir = tmp_path / "source-missing-file-recovery"
    write_named_source_copies(
        source_dir,
        [f"photo_{index:02d}.jpg" for index in range(1, 13)],
    )

    init_result = init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    add_result = add_source(workspace, source_dir)
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
    deleted_path = (source_dir / "photo_11.jpg").resolve()
    try:
        wait_for_batch_status(library_db, batch_index=2, expected_status="running")
        deleted_path.unlink()
        process.send_signal(signal.SIGKILL)
        stdout_text, stderr_text = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=30)

    assert process.returncode != 0, (stdout_text, stderr_text)
    rerun_result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )

    assert rerun_result.returncode == 0, rerun_result.stderr
    assert "Traceback" not in rerun_result.stderr
    embedding_db = workspace / ".hikbox" / "embedding.db"
    assert fetch_one(
        library_db,
        """
        SELECT status, completed_batches, failed_assets
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    ) == ("completed", 2, 1)
    failed_asset = fetch_one(
        library_db,
        """
        SELECT processing_status, failure_reason
        FROM assets
        WHERE absolute_path = ?
        """,
        (str(deleted_path),),
    )
    assert failed_asset[0] == "failed"
    assert failed_asset[1]
    assert fetch_one(
        library_db,
        """
        SELECT status, failure_reason
        FROM scan_batch_items
        WHERE absolute_path = ?
        """,
        (str(deleted_path),),
    )[0] == "failed"
    assert count_rows_matching(
        library_db,
        "SELECT COUNT(*) FROM assets WHERE processing_status = 'succeeded'",
    ) == 11
    assert count_rows(library_db, "face_observations") > 0
    assert count_rows(embedding_db, "face_embeddings") == count_rows(library_db, "face_observations")
    from tests.scan_cli_helpers import read_jsonl
    scan_events = read_jsonl(external_root / "logs" / "scan.log.jsonl")
    assert any(
        event["event"] == "asset_failed" and event.get("asset_path") == str(deleted_path)
        for event in scan_events
    )
