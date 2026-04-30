from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
LATEST_LIBRARY_VERSION = 3
LATEST_EMBEDDING_VERSION = 1  # No embedding_v2.sql yet; embedding stays at v1


def _run_hikbox(
    *args: str,
    cwd: Path | None = None,
    env_updates: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    pythonpath_parts = [str(REPO_ROOT)]
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    if env_updates:
        env.update(env_updates)
    return subprocess.run(
        [sys.executable, "-m", "hikbox_pictures", *args],
        cwd=cwd or REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


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


def _read_sources(db_path: Path) -> list[dict[str, object]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, path, label, active, created_at FROM library_sources ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _init_workspace(workspace: Path, external_root: Path) -> subprocess.CompletedProcess[str]:
    return _run_hikbox(
        "init",
        "--workspace",
        str(workspace),
        "--external-root",
        str(external_root),
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
    assert _index_exists(library_db, "idx_assets_source_id")
    assert _table_exists(embedding_db, "schema_meta")
    assert _table_exists(embedding_db, "face_embeddings")


def test_init_schema_version_matches_latest_even_with_v2_placeholder(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"

    result = _init_workspace(workspace, external_root)

    assert result.returncode == 0

    library_db = workspace / ".hikbox" / "library.db"
    assert _read_schema_version(library_db) == str(LATEST_LIBRARY_VERSION)
    assert _read_table_sql(library_db, "library_sources") != ""


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

    result = _run_hikbox(
        "source",
        "add",
        "--workspace",
        str(workspace),
        str(source_dir),
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

    result = _run_hikbox(
        "source",
        "list",
        "--workspace",
        str(workspace),
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

    result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
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

    # serve will fail because v1 workspace lacks WebUI tables, but migration should succeed
    result = _run_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        "18765",
    )

    # Migration should have happened even though serve fails
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

    result = _run_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        "18767",
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

    result = _run_hikbox(
        "source",
        "list",
        "--workspace",
        str(workspace),
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
            INSERT INTO schema_meta (key, value) VALUES ('schema_version', '3');
            """
        )
    finally:
        conn.close()

    # Should not raise, should be a no-op
    migrate_to_latest(db_path=db_path, db_name="library")
    assert _read_schema_version(db_path) == "3"


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
