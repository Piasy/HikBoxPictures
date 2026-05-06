from __future__ import annotations

from pathlib import Path
import stat

import numpy as np
from PIL import Image
import pytest

from tests.helpers import (
    FIXTURE_DIR,
    add_source,
    fetch_all,
    init_workspace,
    load_manifest,
    run_hikbox,
)
from tests.scan_cli_helpers import (
    SUPPORTED_SCAN_SUFFIXES,
    count_rows,
    count_rows_matching,
    fetch_one,
    normalized_stderr,
    prepare_faceanalysis_spy,
    read_jsonl,
)


def test_scan_start_downgrades_unreadable_supported_file_to_asset_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-unreadable-file"
    external_root = tmp_path / "external-root-unreadable-file"
    source_dir = tmp_path / "source-unreadable-file"
    source_dir.mkdir()
    readable_path = source_dir / "readable.jpg"
    unreadable_path = source_dir / "unreadable.jpg"
    readable_path.write_bytes((FIXTURE_DIR / "pg_001_single_alex_01.jpg").read_bytes())
    unreadable_path.write_bytes((FIXTURE_DIR / "pg_002_single_alex_02.jpg").read_bytes())

    init_result = init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    add_result = add_source(workspace, source_dir)
    assert add_result.returncode == 0

    original_mode = stat.S_IMODE(unreadable_path.stat().st_mode)
    unreadable_path.chmod(0)
    try:
        result = run_hikbox(
            "scan",
            "start",
            "--workspace",
            str(workspace),
            "--batch-size",
            "10",
        )
    finally:
        unreadable_path.chmod(original_mode)

    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    assert normalized_stderr(result.stderr) == ""

    library_db = workspace / ".hikbox" / "library.db"
    embedding_db = workspace / ".hikbox" / "embedding.db"
    assert fetch_one(
        library_db,
        """
        SELECT processing_status, failure_reason
        FROM assets
        WHERE file_name = 'unreadable.jpg'
        """,
    )[0] == "failed"
    unreadable_reason = fetch_one(
        library_db,
        """
        SELECT failure_reason
        FROM assets
        WHERE file_name = 'unreadable.jpg'
        """,
    )[0]
    assert unreadable_reason
    assert fetch_one(
        library_db,
        """
        SELECT processing_status
        FROM assets
        WHERE file_name = 'readable.jpg'
        """,
    )[0] == "succeeded"
    assert count_rows(library_db, "face_observations") > 0
    assert count_rows(embedding_db, "face_embeddings") > 0
    assert fetch_one(
        library_db,
        """
        SELECT status, failed_assets
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    ) == ("completed", 1)
    scan_events = read_jsonl(external_root / "logs" / "scan.log.jsonl")
    assert any(
        event["event"] == "asset_failed" and event.get("asset_path") == str(unreadable_path.resolve())
        for event in scan_events
    )


def test_scan_start_handles_duplicate_content_assets_in_same_batch_without_artifact_name_collision(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace-duplicate-content"
    external_root = tmp_path / "external-root-duplicate-content"
    source_dir = tmp_path / "source-duplicate-content"
    source_dir.mkdir()
    duplicate_bytes = (FIXTURE_DIR / "pg_001_single_alex_01.jpg").read_bytes()
    first_path = source_dir / "duplicate_a.jpg"
    second_path = source_dir / "duplicate_b.jpg"
    first_path.write_bytes(duplicate_bytes)
    second_path.write_bytes(duplicate_bytes)

    init_result = init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    add_result = add_source(workspace, source_dir)
    assert add_result.returncode == 0

    result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )

    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    library_db = workspace / ".hikbox" / "library.db"
    embedding_db = workspace / ".hikbox" / "embedding.db"
    assert fetch_one(
        library_db,
        """
        SELECT status
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    )[0] == "completed"
    assert count_rows_matching(
        library_db,
        """
        SELECT COUNT(*)
        FROM assets
        WHERE file_name IN ('duplicate_a.jpg', 'duplicate_b.jpg')
        """,
    ) == 2
    face_rows = fetch_all(
        library_db,
        """
        SELECT assets.file_name, face_observations.crop_path, face_observations.context_path
        FROM face_observations
        INNER JOIN assets ON assets.id = face_observations.asset_id
        WHERE assets.file_name IN ('duplicate_a.jpg', 'duplicate_b.jpg')
        ORDER BY assets.file_name ASC, face_observations.face_index ASC
        """,
    )
    assert len(face_rows) >= 2
    crop_paths = [Path(str(row[1])) for row in face_rows]
    context_paths = [Path(str(row[2])) for row in face_rows]
    assert len({str(path) for path in crop_paths}) == len(crop_paths)
    assert len({str(path) for path in context_paths}) == len(context_paths)
    for path in [*crop_paths, *context_paths]:
        assert path.is_file()
        with Image.open(path) as image:
            image.load()
    assert count_rows(embedding_db, "face_embeddings") == count_rows(library_db, "face_observations")


def test_scan_start_fails_when_embedding_dimension_is_not_512_and_logs_failure(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-bad-embedding"
    external_root = tmp_path / "external-root-bad-embedding"
    init_result = init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    add_result = add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0
    spy_dir, spy_log_path = prepare_faceanalysis_spy(tmp_path / "spy-bad-embedding")

    result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
        env_updates={
            "HIKBOX_TEST_FACEANALYSIS_SPY_LOG": str(spy_log_path),
            "HIKBOX_TEST_FACEANALYSIS_FORCE_BAD_EMBEDDING": "1",
        },
        pythonpath_prepend=[spy_dir],
    )

    assert result.returncode != 0
    assert "embedding 维度错误" in result.stderr
    library_db = workspace / ".hikbox" / "library.db"
    embedding_db = workspace / ".hikbox" / "embedding.db"
    assert fetch_one(
        library_db,
        """
        SELECT status, completed_batches
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    ) == ("failed", 0)
    assert count_rows_matching(library_db, "SELECT COUNT(*) FROM scan_batches WHERE status = 'completed'") == 0
    assert count_rows(library_db, "face_observations") == 0
    assert count_rows(embedding_db, "face_embeddings") == 0
    scan_events = read_jsonl(external_root / "logs" / "scan.log.jsonl")
    assert any(event["event"] == "scan_failed" for event in scan_events)
    assert any("embedding 维度错误" in str(event.get("reason", "")) for event in scan_events if event.get("event") == "scan_failed")


def test_scan_start_marks_batch_and_session_failed_and_leaves_no_artifacts_when_main_process_commit_fails(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace-commit-failure"
    external_root = tmp_path / "external-root-commit-failure"
    init_result = init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    add_result = add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0
    spy_dir, spy_log_path = prepare_faceanalysis_spy(tmp_path / "spy-commit-failure")

    result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
        env_updates={
            "HIKBOX_TEST_FACEANALYSIS_SPY_LOG": str(spy_log_path),
            "HIKBOX_TEST_CORRUPT_WORKER_OUTPUT": "1",
        },
        pythonpath_prepend=[spy_dir],
    )

    assert result.returncode != 0
    assert "embedding 维度错误" in result.stderr
    library_db = workspace / ".hikbox" / "library.db"
    embedding_db = workspace / ".hikbox" / "embedding.db"
    assert fetch_one(
        library_db,
        """
        SELECT status, completed_batches
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    ) == ("failed", 0)
    failed_batch = fetch_one(
        library_db,
        """
        SELECT status, failure_message
        FROM scan_batches
        WHERE batch_index = 1
        """,
    )
    assert failed_batch[0] == "failed"
    assert failed_batch[1]
    assert "embedding 维度错误" in str(failed_batch[1])
    assert count_rows(library_db, "face_observations") == 0
    assert count_rows(embedding_db, "face_embeddings") == 0
    assert list((external_root / "artifacts" / "crops").iterdir()) == []
    assert list((external_root / "artifacts" / "context").iterdir()) == []
    scan_events = read_jsonl(external_root / "logs" / "scan.log.jsonl")
    assert any(event["event"] == "scan_failed" for event in scan_events)
    assert any("embedding 维度错误" in str(event.get("reason", "")) for event in scan_events if event.get("event") == "scan_failed")


def test_scan_start_rolls_back_partial_artifact_move_when_second_move_fails(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-move-failure"
    external_root = tmp_path / "external-root-move-failure"
    init_result = init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    add_result = add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0
    spy_dir, spy_log_path = prepare_faceanalysis_spy(tmp_path / "spy-move-failure")

    result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
        env_updates={
            "HIKBOX_TEST_FACEANALYSIS_SPY_LOG": str(spy_log_path),
            "HIKBOX_TEST_FAIL_SECOND_ARTIFACT_MOVE": "1",
        },
        pythonpath_prepend=[spy_dir],
    )

    assert result.returncode != 0
    library_db = workspace / ".hikbox" / "library.db"
    embedding_db = workspace / ".hikbox" / "embedding.db"
    assert fetch_one(
        library_db,
        """
        SELECT status, completed_batches
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    ) == ("failed", 0)
    assert fetch_one(
        library_db,
        """
        SELECT status
        FROM scan_batches
        WHERE batch_index = 1
        """,
    )[0] == "failed"
    assert count_rows(library_db, "face_observations") == 0
    assert count_rows(embedding_db, "face_embeddings") == 0
    assert list((external_root / "artifacts" / "crops").iterdir()) == []
    assert list((external_root / "artifacts" / "context").iterdir()) == []
    scan_events = read_jsonl(external_root / "logs" / "scan.log.jsonl")
    assert any(event["event"] == "scan_failed" for event in scan_events)
