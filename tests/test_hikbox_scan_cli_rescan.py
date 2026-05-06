from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3

from PIL import Image
import pytest

from tests.conftest import copy_scanned_workspace
from tests.helpers import (
    REPO_ROOT,
    FIXTURE_DIR,
    add_source,
    fetch_all,
    init_workspace,
    load_manifest,
    run_hikbox,
)
from tests.scan_cli_helpers import (
    count_rows,
    count_rows_matching,
    fetch_one,
    prepare_discover_counter,
    prepare_faceanalysis_spy,
    read_jsonl,
)


FIXTURE_DIR_2 = REPO_ROOT / "tests" / "fixtures" / "people_gallery_scan_2"


def test_scan_start_failed_rescan_keeps_previously_committed_artifacts(tmp_path: Path) -> None:
    workspace, external_root, library_db, manifest, _ = copy_scanned_workspace(tmp_path)
    embedding_db = workspace / ".hikbox" / "embedding.db"
    original_crop_path, original_context_path = fetch_one(
        library_db,
        """
        SELECT crop_path, context_path
        FROM face_observations
        ORDER BY id ASC
        LIMIT 1
        """,
    )
    original_crop = Path(str(original_crop_path))
    original_context = Path(str(original_context_path))
    assert original_crop.is_file()
    assert original_context.is_file()
    with Image.open(original_crop) as image:
        image.load()
    with Image.open(original_context) as image:
        image.load()
    crops_dir = external_root / "artifacts" / "crops"
    context_dir = external_root / "artifacts" / "context"
    crop_names_before = {path.name for path in crops_dir.iterdir()}
    context_names_before = {path.name for path in context_dir.iterdir()}
    face_count_before = count_rows(library_db, "face_observations")
    embedding_count_before = count_rows(embedding_db, "face_embeddings")

    # 重置 source scan_state 为 pending，确保全量重扫以触发 artifact move
    conn = sqlite3.connect(library_db)
    try:
        with conn:
            conn.execute("UPDATE library_sources SET scan_state = 'pending'")
    finally:
        conn.close()

    spy_dir, spy_log_path = prepare_faceanalysis_spy(tmp_path / "spy-rescan-move-failure")
    second_result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "7",
        env_updates={
            "HIKBOX_TEST_FACEANALYSIS_SPY_LOG": str(spy_log_path),
            "HIKBOX_TEST_FAIL_SECOND_ARTIFACT_MOVE": "1",
        },
        pythonpath_prepend=[spy_dir],
    )

    assert second_result.returncode != 0
    assert fetch_one(
        library_db,
        """
        SELECT status
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    )[0] == "failed"
    assert fetch_one(
        library_db,
        """
        SELECT status
        FROM scan_batches
        WHERE session_id = (
          SELECT id FROM scan_sessions ORDER BY id DESC LIMIT 1
        )
        ORDER BY batch_index ASC
        LIMIT 1
        """,
    )[0] == "failed"
    assert count_rows(library_db, "face_observations") == face_count_before
    assert count_rows(embedding_db, "face_embeddings") == embedding_count_before
    assert original_crop.is_file()
    assert original_context.is_file()
    with Image.open(original_crop) as image:
        image.load()
    with Image.open(original_context) as image:
        image.load()
    assert {path.name for path in crops_dir.iterdir()} == crop_names_before
    assert {path.name for path in context_dir.iterdir()} == context_names_before
    scan_events = read_jsonl(external_root / "logs" / "scan.log.jsonl")
    assert any(event["event"] == "scan_failed" for event in scan_events)


def test_scan_start_keeps_committed_new_artifacts_when_old_cleanup_fails_after_commit(tmp_path: Path) -> None:
    workspace, external_root, library_db, manifest, _ = copy_scanned_workspace(tmp_path)
    original_crop_path, original_context_path = fetch_one(
        library_db,
        """
        SELECT crop_path, context_path
        FROM face_observations
        ORDER BY id ASC
        LIMIT 1
        """,
    )
    original_crop = Path(str(original_crop_path))
    original_context = Path(str(original_context_path))
    assert original_crop.is_file()
    assert original_context.is_file()

    # 重置 source scan_state 为 pending，确保全量重扫以触发旧 artifact 清理路径
    conn = sqlite3.connect(library_db)
    try:
        with conn:
            conn.execute("UPDATE library_sources SET scan_state = 'pending'")
    finally:
        conn.close()

    spy_dir, spy_log_path = prepare_faceanalysis_spy(tmp_path / "spy-old-cleanup-failure")
    second_result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "7",
        env_updates={
            "HIKBOX_TEST_FACEANALYSIS_SPY_LOG": str(spy_log_path),
            "HIKBOX_TEST_FAIL_OLD_ARTIFACT_CLEANUP": "1",
        },
        pythonpath_prepend=[spy_dir],
    )

    assert second_result.returncode == 0, second_result.stderr
    latest_session = fetch_one(
        library_db,
        """
        SELECT id, status, completed_batches, total_batches
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    assert latest_session[1:] == ("completed", latest_session[3], latest_session[3])
    new_crop_path, new_context_path = fetch_one(
        library_db,
        """
        SELECT crop_path, context_path
        FROM face_observations
        ORDER BY id ASC
        LIMIT 1
        """,
    )
    new_crop = Path(str(new_crop_path))
    new_context = Path(str(new_context_path))
    assert new_crop != original_crop
    assert new_context != original_context
    assert new_crop.is_file()
    assert new_context.is_file()
    assert original_crop.is_file()
    assert original_context.is_file()
    with Image.open(new_crop) as image:
        image.load()
    with Image.open(new_context) as image:
        image.load()
    scan_events = read_jsonl(external_root / "logs" / "scan.log.jsonl")
    assert any(
        event["event"] == "artifact_cleanup_warning"
        and event.get("session_id") == latest_session[0]
        for event in scan_events
    )
    assert not any(
        event["event"] == "scan_failed"
        and event.get("session_id") == latest_session[0]
        for event in scan_events
    )


def test_scan_start_is_idempotent_after_completed_scan(tmp_path: Path) -> None:
    workspace, external_root, library_db, manifest, _ = copy_scanned_workspace(tmp_path)
    embedding_db = workspace / ".hikbox" / "embedding.db"

    corrupt_retry_before = int(
        fetch_one(
            library_db,
            "SELECT scan_retry_count FROM assets WHERE file_name = 'pg_902_corrupt.jpg'",
        )[0]
    )

    before_counts = (
        count_rows(library_db, "assets"),
        count_rows(library_db, "face_observations"),
        count_rows(embedding_db, "face_embeddings"),
        len(list((external_root / "artifacts" / "crops").iterdir())),
        len(list((external_root / "artifacts" / "context").iterdir())),
    )

    counter_dir, count_file = prepare_discover_counter(tmp_path)

    second_result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
        env_updates={"HIKBOX_TEST_DISCOVER_COUNT_FILE": str(count_file)},
        pythonpath_prepend=[counter_dir],
    )
    assert second_result.returncode == 0, second_result.stderr

    counts = json.loads(count_file.read_text(encoding="utf-8"))
    assert counts["iterdir"] == 0, f"预期零 iterdir 调用，实际 {counts['iterdir']}"
    assert counts["scandir"] == 0, f"预期零 scandir 调用，实际 {counts['scandir']}"

    after_counts = (
        count_rows(library_db, "assets"),
        count_rows(library_db, "face_observations"),
        count_rows(embedding_db, "face_embeddings"),
        len(list((external_root / "artifacts" / "crops").iterdir())),
        len(list((external_root / "artifacts" / "context").iterdir())),
    )
    assert after_counts == before_counts

    latest_session = fetch_one(
        library_db,
        """
        SELECT id, total_batches, completed_batches, failed_assets
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    assert latest_session[1] == 1
    assert latest_session[2] == 1
    assert latest_session[3] == 1

    batch_items = fetch_all(
        library_db,
        """
        SELECT absolute_path
        FROM scan_batch_items
        WHERE batch_id IN (
            SELECT id FROM scan_batches WHERE session_id = ?
        )
        """,
        (int(latest_session[0]),),
    )
    assert len(batch_items) == 1
    assert "pg_902_corrupt.jpg" in str(batch_items[0][0])

    corrupt_retry_after = int(
        fetch_one(
            library_db,
            "SELECT scan_retry_count FROM assets WHERE file_name = 'pg_902_corrupt.jpg'",
        )[0]
    )
    assert corrupt_retry_after == corrupt_retry_before + 1

    source_state = fetch_one(
        library_db,
        "SELECT scan_state FROM library_sources LIMIT 1",
    )[0]
    assert source_state == "scanned_with_retries"


def test_scan_start_includes_new_pending_source_and_retries_old_source(tmp_path: Path) -> None:
    workspace, external_root, library_db, manifest, _ = copy_scanned_workspace(tmp_path)
    embedding_db = workspace / ".hikbox" / "embedding.db"

    old_source_path = str(FIXTURE_DIR.resolve())
    old_source_id = int(
        fetch_one(
            library_db,
            "SELECT id FROM library_sources WHERE path = ?",
            (old_source_path,),
        )[0]
    )

    before_old_faces = count_rows_matching(
        library_db,
        """
        SELECT COUNT(*)
        FROM face_observations
        WHERE asset_id IN (
            SELECT id FROM assets WHERE source_id = ?
        )
        """,
        params=(old_source_id,),
    )
    before_old_face_ids = [
        int(row[0])
        for row in fetch_all(
            library_db,
            """
            SELECT id FROM face_observations
            WHERE asset_id IN (
                SELECT id FROM assets WHERE source_id = ?
            )
            """,
            (old_source_id,),
        )
    ]
    before_old_embeddings = count_rows_matching(
        embedding_db,
        f"""
        SELECT COUNT(*)
        FROM face_embeddings
        WHERE face_observation_id IN (
            {', '.join(str(fid) for fid in before_old_face_ids)}
        )
        """ if before_old_face_ids else "SELECT 0",
    )
    before_assets = count_rows(library_db, "assets")

    add_result = add_source(workspace, FIXTURE_DIR_2)
    assert add_result.returncode == 0, add_result.stderr

    result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert result.returncode == 0, result.stderr

    latest_session = fetch_one(
        library_db,
        """
        SELECT id, total_batches, completed_batches, failed_assets
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    assert latest_session[1] == 2
    assert latest_session[2] == 2

    old_source_items = fetch_all(
        library_db,
        """
        SELECT sbi.absolute_path
        FROM scan_batch_items sbi
        INNER JOIN scan_batches sb ON sb.id = sbi.batch_id
        INNER JOIN library_sources ls ON ls.id = sbi.source_id
        WHERE sb.session_id = ? AND ls.path = ?
        """,
        (int(latest_session[0]), old_source_path),
    )
    assert len(old_source_items) == 1
    assert "pg_902_corrupt.jpg" in str(old_source_items[0][0])

    new_source_path = str(FIXTURE_DIR_2.resolve())
    new_source_items = fetch_all(
        library_db,
        """
        SELECT sbi.absolute_path
        FROM scan_batch_items sbi
        INNER JOIN scan_batches sb ON sb.id = sbi.batch_id
        INNER JOIN library_sources ls ON ls.id = sbi.source_id
        WHERE sb.session_id = ? AND ls.path = ?
        """,
        (int(latest_session[0]), new_source_path),
    )
    assert len(new_source_items) == 15

    states = {
        str(row[1]): str(row[0])
        for row in fetch_all(
            library_db,
            "SELECT scan_state, path FROM library_sources",
        )
    }
    assert states[old_source_path] == "scanned_with_retries"
    assert states[new_source_path] == "scanned_clean"

    after_old_faces = count_rows_matching(
        library_db,
        """
        SELECT COUNT(*)
        FROM face_observations
        WHERE asset_id IN (
            SELECT id FROM assets WHERE source_id = ?
        )
        """,
        params=(old_source_id,),
    )
    after_old_face_ids = [
        int(row[0])
        for row in fetch_all(
            library_db,
            """
            SELECT id FROM face_observations
            WHERE asset_id IN (
                SELECT id FROM assets WHERE source_id = ?
            )
            """,
            (old_source_id,),
        )
    ]
    after_old_embeddings = count_rows_matching(
        embedding_db,
        f"""
        SELECT COUNT(*)
        FROM face_embeddings
        WHERE face_observation_id IN (
            {', '.join(str(fid) for fid in after_old_face_ids)}
        )
        """ if after_old_face_ids else "SELECT 0",
    )
    assert after_old_faces == before_old_faces
    assert after_old_embeddings == before_old_embeddings
    assert count_rows(library_db, "assets") == before_assets + 15


def test_scan_start_retries_exhausted_after_three_failures(tmp_path: Path) -> None:
    workspace, external_root, library_db, manifest, _ = copy_scanned_workspace(tmp_path)

    corrupt_retry_count = int(
        fetch_one(
            library_db,
            "SELECT scan_retry_count FROM assets WHERE file_name = 'pg_902_corrupt.jpg'",
        )[0]
    )

    while corrupt_retry_count < 3:
        result = run_hikbox(
            "scan",
            "start",
            "--workspace",
            str(workspace),
            "--batch-size",
            "10",
        )
        assert result.returncode == 0, result.stderr
        corrupt_retry_count = int(
            fetch_one(
                library_db,
                "SELECT scan_retry_count FROM assets WHERE file_name = 'pg_902_corrupt.jpg'",
            )[0]
        )

    result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert result.returncode != 0
    assert "没有可扫描照片" in result.stderr

    source_state = fetch_one(
        library_db,
        "SELECT scan_state FROM library_sources LIMIT 1",
    )[0]
    assert source_state == "scanned_clean"


def test_scan_start_skips_scanned_clean_source_with_zero_filesystem_operations(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"

    init_result = init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    add_result = add_source(workspace, FIXTURE_DIR_2)
    assert add_result.returncode == 0

    first_result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert first_result.returncode == 0, first_result.stderr

    library_db = workspace / ".hikbox" / "library.db"
    source_state = fetch_one(
        library_db,
        "SELECT scan_state FROM library_sources LIMIT 1",
    )[0]
    assert source_state == "scanned_clean"

    counter_dir, count_file = prepare_discover_counter(tmp_path)

    second_result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
        env_updates={"HIKBOX_TEST_DISCOVER_COUNT_FILE": str(count_file)},
        pythonpath_prepend=[counter_dir],
    )
    assert second_result.returncode != 0
    assert "没有可扫描照片" in second_result.stderr

    counts = json.loads(count_file.read_text(encoding="utf-8"))
    assert counts["iterdir"] == 0, f"预期零 iterdir 调用，实际 {counts['iterdir']}"
    assert counts["scandir"] == 0, f"预期零 scandir 调用，实际 {counts['scandir']}"

    assert count_rows(library_db, "scan_sessions") == 1


def test_scan_start_refreshes_stale_running_session_when_all_batches_are_already_completed(
    tmp_path: Path,
) -> None:
    workspace, external_root, library_db, manifest, _ = copy_scanned_workspace(tmp_path)
    original_summary = fetch_one(
        library_db,
        """
        SELECT id, status, total_batches, completed_batches, failed_assets, success_faces, artifact_files
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    session_id = int(original_summary[0])
    total_batches = int(original_summary[2])
    conn = sqlite3.connect(library_db)
    try:
        with conn:
            conn.execute(
                """
                UPDATE scan_sessions
                SET status = 'running',
                    completed_batches = 0,
                    failed_assets = 0,
                    success_faces = 0,
                    artifact_files = 0,
                    completed_at = NULL
                WHERE id = ?
                """,
                (session_id,),
            )
    finally:
        conn.close()

    rerun_result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )

    assert rerun_result.returncode == 0, rerun_result.stderr

    # 旧 stale session 应被 reconcile 标记为 completed
    stale_session = fetch_one(
        library_db,
        "SELECT status FROM scan_sessions WHERE id = ?",
        (session_id,),
    )
    assert stale_session[0] == "completed"

    # 新 session 创建并完成
    # 由于旧源已是 scanned_with_retries，新 session 只包含 1 个重试候选
    new_session = fetch_one(
        library_db,
        """
        SELECT id, status, total_batches, completed_batches
        FROM scan_sessions
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    assert new_session[0] != session_id
    assert new_session[1] == "completed"
    assert new_session[2] == 1
    assert new_session[3] == 1

    assert count_rows_matching(
        library_db,
        "SELECT COUNT(*) FROM scan_sessions WHERE status = 'running'",
    ) == 0
