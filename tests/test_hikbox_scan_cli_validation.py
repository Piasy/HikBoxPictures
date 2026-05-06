from __future__ import annotations

import json
from pathlib import Path
import shutil
import stat
import sqlite3

import pytest

from tests.conftest import copy_scanned_workspace
from tests.helpers import (
    FIXTURE_DIR,
    add_source,
    find_free_port,
    init_workspace,
    load_manifest,
    run_hikbox,
    spawn_hikbox,
    terminate_process,
    wait_for_http_ready,
)
from tests.scan_cli_helpers import (
    count_rows,
    count_rows_matching,
    create_slice_a_only_workspace,
    fetch_one,
    normalized_stderr,
)


def test_scan_start_fails_without_initialized_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"

    result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
    )

    assert result.returncode != 0
    assert "工作区" in result.stderr
    assert not (workspace / ".hikbox").exists()


def test_scan_start_rejects_invalid_batch_size(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    init_result = init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    invalid_results = [
        run_hikbox("scan", "start", "--workspace", str(workspace), "--batch-size", "0"),
        run_hikbox("scan", "start", "--workspace", str(workspace), "--batch-size", "-1"),
        run_hikbox("scan", "start", "--workspace", str(workspace), "--batch-size", "abc"),
    ]

    for result in invalid_results:
        assert result.returncode != 0
        assert "batch-size" in result.stderr
        assert "正整数" in result.stderr


def test_scan_start_fails_when_no_active_source_exists(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    init_result = init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
    )

    assert result.returncode != 0
    assert "active source" in result.stderr or "没有可用 source" in result.stderr
    assert count_rows(workspace / ".hikbox" / "library.db", "library_sources") == 0


def test_scan_start_fails_when_serve_is_running(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-serve-running"
    external_root = tmp_path / "external-root-serve-running"
    init_result = init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    add_result = add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0

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
        result = run_hikbox(
            "scan",
            "start",
            "--workspace",
            str(workspace),
            "--batch-size",
            "10",
        )
    finally:
        terminate_process(process)

    assert result.returncode != 0
    normalized = normalized_stderr(result.stderr)
    assert normalized.startswith("scan start 失败:")
    assert "serve" in normalized
    assert "互斥" in normalized or "运行中" in normalized
    library_db = workspace / ".hikbox" / "library.db"
    assert count_rows(library_db, "scan_sessions") == 0
    assert count_rows(library_db, "assets") == 0
    assert count_rows(library_db, "face_observations") == 0


def test_scan_start_fails_cleanly_for_slice_a_only_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-slice-a-only"
    external_root = tmp_path / "external-root-slice-a-only"
    source_dir = tmp_path / "source-slice-a-only"
    source_dir.mkdir()
    (source_dir / "sample.jpg").write_bytes((FIXTURE_DIR / "pg_001_single_alex_01.jpg").read_bytes())
    create_slice_a_only_workspace(workspace, external_root, source_dir)

    result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
    )

    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    normalized = normalized_stderr(result.stderr)
    assert normalized.startswith("scan start 失败:")
    assert "缺少扫描表" in normalized
    assert "不支持自动升级" in normalized
    assert "hikbox-pictures init" in normalized
    assert "source add" in normalized
    assert "scan start" in normalized
    assert "no such table" not in normalized
    assert "scan session 初始化失败" not in normalized


def test_scan_start_fails_early_when_workspace_lacks_person_face_exclusions_table(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-missing-exclusions-table"
    external_root = tmp_path / "external-root-missing-exclusions-table"
    source_dir = tmp_path / "source-missing-exclusions-table"
    source_dir.mkdir()
    (source_dir / "sample.jpg").write_bytes((FIXTURE_DIR / "pg_001_single_alex_01.jpg").read_bytes())

    init_result = init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    add_result = add_source(workspace, source_dir)
    assert add_result.returncode == 0

    library_db = workspace / ".hikbox" / "library.db"
    connection = sqlite3.connect(library_db)
    try:
        with connection:
            connection.execute("DROP TABLE person_face_exclusions")
    finally:
        connection.close()

    result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )

    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    normalized = normalized_stderr(result.stderr)
    assert normalized.startswith("scan start 失败:")
    assert "缺少扫描表" in normalized
    assert "person_face_exclusions" in normalized
    assert "不支持自动升级" in normalized
    assert "assignment 输入读取失败" not in normalized
    assert "no such table" not in normalized
    assert count_rows(library_db, "scan_sessions") == 0
    assert count_rows(library_db, "assets") == 0
    assert count_rows(library_db, "face_observations") == 0


def test_scan_start_reports_log_path_io_failure_without_traceback(tmp_path: Path) -> None:
    workspace, external_root, library_db, manifest, _ = copy_scanned_workspace(tmp_path)

    logs_dir = external_root / "logs"
    shutil.rmtree(logs_dir)
    logs_dir.write_text("occupied-by-file", encoding="utf-8")

    result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )

    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    normalized = normalized_stderr(result.stderr)
    assert normalized.startswith("scan start 失败:")
    assert "日志" in normalized
    assert "logs" in normalized or "scan.log.jsonl" in normalized


@pytest.mark.parametrize(
    ("source_state", "expected_message"),
    [
        ("missing", "source 路径不存在"),
        ("file", "source 不是目录"),
        ("unreadable", "source 不可读"),
    ],
)
def test_scan_start_fails_when_source_becomes_invalid(
    tmp_path: Path,
    source_state: str,
    expected_message: str,
) -> None:
    workspace = tmp_path / f"workspace-{source_state}"
    external_root = tmp_path / f"external-root-{source_state}"
    source_dir = tmp_path / f"source-{source_state}"
    source_dir.mkdir()
    (source_dir / "sample.jpg").write_bytes((FIXTURE_DIR / "pg_001_single_alex_01.jpg").read_bytes())

    init_result = init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    add_result = add_source(workspace, source_dir)
    assert add_result.returncode == 0

    original_mode = None
    if source_state == "missing":
        shutil.rmtree(source_dir)
    elif source_state == "file":
        shutil.rmtree(source_dir)
        source_dir.write_text("not-a-directory", encoding="utf-8")
    elif source_state == "unreadable":
        original_mode = stat.S_IMODE(source_dir.stat().st_mode)
        source_dir.chmod(0)

    try:
        result = run_hikbox(
            "scan",
            "start",
            "--workspace",
            str(workspace),
        )
    finally:
        if source_state == "unreadable" and source_dir.exists():
            source_dir.chmod(original_mode if original_mode is not None else 0o755)

    assert result.returncode != 0
    assert expected_message in result.stderr
    library_db = workspace / ".hikbox" / "library.db"
    assert count_rows_matching(library_db, "SELECT COUNT(*) FROM scan_batches WHERE status = 'completed'") == 0
