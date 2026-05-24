from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys

from tests.helpers import REPO_ROOT, run_hikbox

LATEST_LIBRARY_VERSION = 5
LATEST_EMBEDDING_VERSION = 1  # No embedding_v2.sql yet; embedding stays at v1


def _run_hikbox_with_inline_python(
    python_source: str,
    *,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    pythonpath_parts = [str(REPO_ROOT)]
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    return subprocess.run(
        [sys.executable, "-c", python_source],
        cwd=cwd or REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _read_schema_version(db_path: Path) -> str:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0])


def _table_exists(db_path: Path, table_name: str) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def _index_exists(db_path: Path, index_name: str) -> bool:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
            (index_name,),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def _read_table_sql(db_path: Path, table_name: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return " ".join(str(row[0]).split())


def _read_table_columns(db_path: Path, table_name: str) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    finally:
        conn.close()
    return [str(row[1]) for row in rows]


def _read_sources(db_path: Path) -> list[dict[str, object]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, path, label, active, scan_state, created_at FROM library_sources ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _init_workspace(workspace: Path, external_root: Path) -> subprocess.CompletedProcess[str]:
    return run_hikbox(
        "init", "--workspace", str(workspace), "--external-root", str(external_root),
        timeout=60,
    )


def _create_v1_workspace(workspace: Path, external_root: Path, source_dir: Path | None = None) -> None:
    """Create a workspace with schema_version=1 using the full v1 SQL files."""
    workspace.mkdir(parents=True, exist_ok=True)
    hikbox_dir = workspace / ".hikbox"
    hikbox_dir.mkdir(parents=True, exist_ok=True)
    (external_root / "artifacts" / "crops").mkdir(parents=True, exist_ok=True)
    (external_root / "artifacts" / "context").mkdir(parents=True, exist_ok=True)
    (external_root / "logs").mkdir(parents=True, exist_ok=True)
    (hikbox_dir / "config.json").write_text(
        json.dumps(
            {
                "config_version": 1,
                "external_root": str(external_root.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    library_sql = (REPO_ROOT / "hikbox_pictures" / "product" / "db" / "sql" / "library_v1.sql").read_text(
        encoding="utf-8"
    )
    embedding_sql = (REPO_ROOT / "hikbox_pictures" / "product" / "db" / "sql" / "embedding_v1.sql").read_text(
        encoding="utf-8"
    )

    library_db = hikbox_dir / "library.db"
    embedding_db = hikbox_dir / "embedding.db"

    library_conn = sqlite3.connect(library_db)
    try:
        with library_conn:
            library_conn.executescript(library_sql)
            if source_dir is not None:
                library_conn.execute(
                    """
                    INSERT INTO library_sources (path, label, active, created_at)
                    VALUES (?, 'test-source', 1, '2026-04-30T00:00:00Z')
                    """,
                    (str(source_dir.resolve()),),
                )
    finally:
        library_conn.close()

    embedding_conn = sqlite3.connect(embedding_db)
    try:
        with embedding_conn:
            embedding_conn.executescript(embedding_sql)
    finally:
        embedding_conn.close()


# ---------------------------------------------------------------------------
# AC-1: New workspace gets latest schema
# ---------------------------------------------------------------------------

def test_init_creates_workspace_with_latest_schema_version(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"

    result = _init_workspace(workspace, external_root)

    assert result.returncode == 0
    assert result.stderr == ""

    library_db = workspace / ".hikbox" / "library.db"
    embedding_db = workspace / ".hikbox" / "embedding.db"

    assert _read_schema_version(library_db) == str(LATEST_LIBRARY_VERSION)
    assert _read_schema_version(embedding_db) == str(LATEST_EMBEDDING_VERSION)

    # v1 tables and indexes should still exist
    assert _table_exists(library_db, "schema_meta")
    assert _table_exists(library_db, "library_sources")
    assert _table_exists(library_db, "assets")
    assert _table_exists(library_db, "export_burst_pick_run")
    assert _table_exists(library_db, "export_burst_pick_group")
    assert _table_exists(library_db, "export_burst_pick_group_asset")
    assert _table_exists(library_db, "export_burst_pick_group_edge")
    assert "algorithm_version" in _read_table_columns(library_db, "export_burst_pick_run")
    assert _index_exists(library_db, "idx_assets_source_id")
    assert "file_fingerprint" not in _read_table_columns(library_db, "assets")
    assert _table_exists(embedding_db, "schema_meta")
    assert _table_exists(embedding_db, "face_embeddings")


def test_init_schema_version_matches_latest(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"

    result = _init_workspace(workspace, external_root)

    assert result.returncode == 0

    library_db = workspace / ".hikbox" / "library.db"
    assert _read_schema_version(library_db) == str(LATEST_LIBRARY_VERSION)
    assert _read_table_sql(library_db, "library_sources") != ""
    assert "file_fingerprint" not in _read_table_columns(library_db, "assets")


def test_library_v2_migration_drops_asset_file_fingerprint_and_preserves_rows(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    source_dir = tmp_path / "photos"
    image_path = source_dir / "image.jpg"
    source_dir.mkdir()
    image_path.write_bytes(b"fake image bytes")

    _create_v1_workspace(workspace, external_root, source_dir=source_dir)
    library_db = workspace / ".hikbox" / "library.db"
    connection = sqlite3.connect(library_db)
    try:
        with connection:
            source_id = int(connection.execute("SELECT id FROM library_sources").fetchone()[0])
            connection.execute(
                """
                INSERT INTO assets (
                  source_id,
                  absolute_path,
                  file_name,
                  file_extension,
                  capture_month,
                  file_fingerprint,
                  live_photo_mov_path,
                  processing_status,
                  failure_reason,
                  scan_retry_count,
                  created_at,
                  updated_at
                )
                VALUES (?, ?, 'image.jpg', 'jpg', '2026-05', 'old-fingerprint', NULL, 'failed', 'bad image', 2, '2026-05-05T00:00:00Z', '2026-05-05T00:00:00Z')
                """,
                (source_id, str(image_path.resolve())),
            )
    finally:
        connection.close()

    result = run_hikbox("source", "list", "--workspace", str(workspace), timeout=60)

    assert result.returncode == 0
    assert _read_schema_version(library_db) == str(LATEST_LIBRARY_VERSION)
    assert "file_fingerprint" not in _read_table_columns(library_db, "assets")
    conn = sqlite3.connect(library_db)
    try:
        row = conn.execute(
            """
            SELECT absolute_path, file_name, file_extension, capture_month,
                   processing_status, failure_reason, scan_retry_count
            FROM assets
            """
        ).fetchone()
    finally:
        conn.close()
    assert row == (
        str(image_path.resolve()),
        "image.jpg",
        "jpg",
        "2026-05",
        "failed",
        "bad image",
        2,
    )


# ---------------------------------------------------------------------------
# AC-2: Old workspace auto-migrates on non-init commands
# ---------------------------------------------------------------------------

def test_source_add_auto_migrates_old_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    source_dir = tmp_path / "photos"
    source_dir.mkdir()

    _create_v1_workspace(workspace, external_root)

    assert _read_schema_version(workspace / ".hikbox" / "library.db") == "1"

    result = run_hikbox(
        "source",
        "add",
        "--workspace",
        str(workspace),
        str(source_dir),
        timeout=60,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert _read_schema_version(workspace / ".hikbox" / "library.db") == str(LATEST_LIBRARY_VERSION)
    assert _read_schema_version(workspace / ".hikbox" / "embedding.db") == str(LATEST_EMBEDDING_VERSION)
    assert len(_read_sources(workspace / ".hikbox" / "library.db")) == 1


def test_source_list_auto_migrates_old_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    source_dir = tmp_path / "photos"
    source_dir.mkdir()

    _create_v1_workspace(workspace, external_root, source_dir=source_dir)

    assert _read_schema_version(workspace / ".hikbox" / "library.db") == "1"

    result = run_hikbox(
        "source",
        "list",
        "--workspace",
        str(workspace),
        timeout=60,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert len(payload["sources"]) == 1
    assert _read_schema_version(workspace / ".hikbox" / "library.db") == str(LATEST_LIBRARY_VERSION)
    assert _read_schema_version(workspace / ".hikbox" / "embedding.db") == str(LATEST_EMBEDDING_VERSION)


def test_scan_start_auto_migrates_old_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    source_dir = tmp_path / "photos"
    source_dir.mkdir()

    _create_v1_workspace(workspace, external_root, source_dir=source_dir)

    assert _read_schema_version(workspace / ".hikbox" / "library.db") == "1"

    result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        timeout=60,
    )

    # Command may fail for other reasons (no models, etc.) but migration should succeed
    assert _read_schema_version(workspace / ".hikbox" / "library.db") == str(LATEST_LIBRARY_VERSION)
    assert _read_schema_version(workspace / ".hikbox" / "embedding.db") == str(LATEST_EMBEDDING_VERSION)


def test_serve_auto_migrates_old_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    source_dir = tmp_path / "photos"
    source_dir.mkdir()

    _create_v1_workspace(workspace, external_root, source_dir=source_dir)

    assert _read_schema_version(workspace / ".hikbox" / "library.db") == "1"

    # 预占端口，让 serve 在 migration 完成后、启动 uvicorn 前因端口冲突快速退出。
    # v1 workspace 经 migration 自动升级后已具备所有 WebUI 表，不阻断的话 serve 会成功启动并永久阻塞。
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", 18765))
    try:
        result = run_hikbox(
            "serve",
            "--workspace",
            str(workspace),
            "--port",
            "18765",
            timeout=60,
        )
    finally:
        blocker.close()

    # serve 虽然因端口占用启动失败，但 migration 应该已成功执行
    assert _read_schema_version(workspace / ".hikbox" / "library.db") == str(LATEST_LIBRARY_VERSION)
    assert _read_schema_version(workspace / ".hikbox" / "embedding.db") == str(LATEST_EMBEDDING_VERSION)


# ---------------------------------------------------------------------------
# AC-3: Repeated init on existing workspace errors without modifying DB
# ---------------------------------------------------------------------------

def test_repeated_init_fails_and_does_not_modify_schema_version(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"

    first_result = _init_workspace(workspace, external_root)
    assert first_result.returncode == 0

    library_db = workspace / ".hikbox" / "library.db"
    embedding_db = workspace / ".hikbox" / "embedding.db"
    original_library_version = _read_schema_version(library_db)
    original_embedding_version = _read_schema_version(embedding_db)
    original_library_db_bytes = library_db.read_bytes()
    original_embedding_db_bytes = embedding_db.read_bytes()

    second_result = _init_workspace(workspace, external_root)

    assert second_result.returncode != 0
    assert "已存在" in second_result.stderr
    assert _read_schema_version(library_db) == original_library_version
    assert _read_schema_version(embedding_db) == original_embedding_version
    assert library_db.read_bytes() == original_library_db_bytes
    assert embedding_db.read_bytes() == original_embedding_db_bytes


# ---------------------------------------------------------------------------
# AC-4: Migration failure causes command failure with DB rollback
# ---------------------------------------------------------------------------

def test_migration_failure_causes_command_failure_with_schema_version_unchanged(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    source_dir = tmp_path / "photos"
    source_dir.mkdir()
    broken_sql_dir = tmp_path / "broken_sql"

    _create_v1_workspace(workspace, external_root, source_dir=source_dir)

    library_version_before = _read_schema_version(workspace / ".hikbox" / "library.db")
    assert library_version_before == "1"

    python_source = f"""
from pathlib import Path
import runpy
import sys

import hikbox_pictures.product.db.migration as migration_module
migration_module.SQL_DIR = Path({str(broken_sql_dir)!r})

sys.argv = [
    "hikbox-pictures",
    "source",
    "list",
    "--workspace",
    {str(workspace)!r},
]
runpy.run_module("hikbox_pictures", run_name="__main__")
"""
    # broken_sql_dir does not exist, so migration will find no SQL files.
    # This means the library stays at v1 (no migration needed) - not a failure.
    # For a real failure test, we need a SQL file with syntax errors.

    # Create a broken SQL file
    broken_sql_dir.mkdir(parents=True, exist_ok=True)
    (broken_sql_dir / "library_v2.sql").write_text(
        "THIS IS NOT VALID SQL SYNTAX;",
        encoding="utf-8",
    )
    # Copy the real embedding SQL so embedding migration works
    real_embedding_v1 = REPO_ROOT / "hikbox_pictures" / "product" / "db" / "sql" / "embedding_v1.sql"
    (broken_sql_dir / "embedding_v1.sql").write_text(
        real_embedding_v1.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    result = _run_hikbox_with_inline_python(python_source)

    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    # schema_version should remain at 1
    assert _read_schema_version(workspace / ".hikbox" / "library.db") == "1"


def test_migration_failure_does_not_corrupt_existing_data(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    source_dir = tmp_path / "photos"
    source_dir.mkdir()
    broken_sql_dir = tmp_path / "broken_sql"
    broken_sql_dir.mkdir(parents=True, exist_ok=True)

    _create_v1_workspace(workspace, external_root, source_dir=source_dir)

    # Add a source via the real init first to have data in the DB
    library_db = workspace / ".hikbox" / "library.db"
    original_sources = _read_sources(library_db)
    assert len(original_sources) == 1

    # Create broken migration
    (broken_sql_dir / "library_v2.sql").write_text(
        "THIS IS NOT VALID SQL SYNTAX;",
        encoding="utf-8",
    )
    real_embedding_v1 = REPO_ROOT / "hikbox_pictures" / "product" / "db" / "sql" / "embedding_v1.sql"
    (broken_sql_dir / "embedding_v1.sql").write_text(
        real_embedding_v1.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    python_source = f"""
from pathlib import Path
import runpy
import sys

import hikbox_pictures.product.db.migration as migration_module
migration_module.SQL_DIR = Path({str(broken_sql_dir)!r})

sys.argv = [
    "hikbox-pictures",
    "source",
    "list",
    "--workspace",
    {str(workspace)!r},
]
runpy.run_module("hikbox_pictures", run_name="__main__")
"""
    result = _run_hikbox_with_inline_python(python_source)

    assert result.returncode != 0
    # Original data should be intact
    assert _read_sources(library_db) == original_sources
    assert _read_schema_version(library_db) == "1"


# ---------------------------------------------------------------------------
# AC-5: Already at target version -> zero overhead skip
# ---------------------------------------------------------------------------

def test_serve_on_already_latest_workspace_does_not_change_schema_version(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"

    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    library_db = workspace / ".hikbox" / "library.db"
    embedding_db = workspace / ".hikbox" / "embedding.db"
    assert _read_schema_version(library_db) == str(LATEST_LIBRARY_VERSION)
    assert _read_schema_version(embedding_db) == str(LATEST_EMBEDDING_VERSION)

    library_version_before = _read_schema_version(library_db)
    embedding_version_before = _read_schema_version(embedding_db)

    # Drop a table that ensure_webui_schema_ready checks so serve fails early
    # before starting uvicorn. This lets us verify that migration on a
    # already-latest workspace is a no-op.
    conn = sqlite3.connect(library_db)
    try:
        with conn:
            conn.execute("DROP TABLE IF EXISTS person_merge_operations")
    finally:
        conn.close()

    result = run_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        "18767",
        timeout=60,
    )

    # Serve should fail because of missing table, but schema_version unchanged
    assert result.returncode != 0
    assert _read_schema_version(library_db) == library_version_before
    assert _read_schema_version(embedding_db) == embedding_version_before


def test_source_list_on_latest_workspace_does_not_change_schema_version(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"

    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    library_db = workspace / ".hikbox" / "library.db"
    embedding_db = workspace / ".hikbox" / "embedding.db"
    assert _read_schema_version(library_db) == str(LATEST_LIBRARY_VERSION)

    result = run_hikbox(
        "source",
        "list",
        "--workspace",
        str(workspace),
        timeout=60,
    )

    assert result.returncode == 0
    assert _read_schema_version(library_db) == str(LATEST_LIBRARY_VERSION)
    assert _read_schema_version(embedding_db) == str(LATEST_EMBEDDING_VERSION)
    assert json.loads(result.stdout) == {"sources": []}


# ---------------------------------------------------------------------------
# Unit tests for migration runner
# ---------------------------------------------------------------------------

def test_migrate_to_latest_skips_when_already_at_latest(tmp_path: Path) -> None:
    from hikbox_pictures.product.db.migration import migrate_to_latest

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta (key, value) VALUES ('schema_version', '5');
            """
        )
    finally:
        conn.close()

    # Should not raise, should be a no-op
    migrate_to_latest(db_path=db_path, db_name="library")
    assert _read_schema_version(db_path) == "5"


def test_migrate_to_latest_raises_on_missing_schema_meta(tmp_path: Path) -> None:
    from hikbox_pictures.product.db.migration import migrate_to_latest, MigrationError

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript("CREATE TABLE dummy (id INTEGER PRIMARY KEY);")
    finally:
        conn.close()

    try:
        migrate_to_latest(db_path=db_path, db_name="library")
        raise AssertionError("Expected MigrationError")
    except MigrationError:
        pass


def test_migrate_to_latest_raises_on_invalid_schema_version(tmp_path: Path) -> None:
    from hikbox_pictures.product.db.migration import migrate_to_latest, MigrationError

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta (key, value) VALUES ('schema_version', 'not_a_number');
            """
        )
    finally:
        conn.close()

    try:
        migrate_to_latest(db_path=db_path, db_name="library")
        raise AssertionError("Expected MigrationError")
    except MigrationError:
        pass


# ---------------------------------------------------------------------------
# 当前 migration 文件清单
# ---------------------------------------------------------------------------

def test_migration_sql_files_match_current_versions() -> None:
    sql_dir = REPO_ROOT / "hikbox_pictures" / "product" / "db" / "sql"
    assert (sql_dir / "library_v1.sql").is_file()
    assert (sql_dir / "library_v2.sql").is_file()
    assert (sql_dir / "library_v3.sql").is_file()
    assert (sql_dir / "library_v4.sql").is_file()
    assert (sql_dir / "library_v5.sql").is_file()
    assert (sql_dir / "embedding_v1.sql").is_file()
    assert not (sql_dir / "library_v6.sql").exists()
    assert not (sql_dir / "embedding_v2.sql").exists()
