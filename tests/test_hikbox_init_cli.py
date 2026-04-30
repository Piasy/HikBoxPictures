from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_hikbox(
    *args: str,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    pythonpath_parts = [str(REPO_ROOT)]
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
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


def _log_files(logs_dir: Path) -> list[Path]:
    return sorted(path for path in logs_dir.iterdir() if path.is_file())


def _assert_tree_absent(root_path: Path) -> None:
    if root_path.exists():
        descendants = [str(path) for path in sorted(root_path.rglob("*"))]
        raise AssertionError(f"发现残留目录树: root={root_path}, descendants={descendants}")


def test_init_creates_workspace_schema_artifacts_and_success_log(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"

    result = _run_hikbox(
        "init",
        "--workspace",
        str(workspace),
        "--external-root",
        str(external_root),
    )

    assert result.returncode == 0
    assert result.stderr == ""

    hikbox_dir = workspace / ".hikbox"
    config_path = hikbox_dir / "config.json"
    library_db_path = hikbox_dir / "library.db"
    embedding_db_path = hikbox_dir / "embedding.db"
    crops_dir = external_root / "artifacts" / "crops"
    context_dir = external_root / "artifacts" / "context"
    logs_dir = external_root / "logs"

    assert hikbox_dir.is_dir()
    assert config_path.is_file()
    assert library_db_path.is_file()
    assert embedding_db_path.is_file()
    assert crops_dir.is_dir()
    assert context_dir.is_dir()
    assert logs_dir.is_dir()

    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config == {
        "config_version": 1,
        "external_root": str(external_root.resolve()),
    }
    assert _read_schema_version(library_db_path) == "3"
    assert _read_schema_version(embedding_db_path) == "1"

    library_sources_sql = _read_table_sql(library_db_path, "library_sources")
    assert "path TEXT NOT NULL UNIQUE" in library_sources_sql
    assert "active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))" in library_sources_sql

    log_files = _log_files(logs_dir)
    assert len(log_files) == 1
    log_text = log_files[0].read_text(encoding="utf-8")
    assert "hikbox-pictures init" in log_text
    assert str(workspace.resolve()) in log_text
    assert str(external_root.resolve()) in log_text
    assert "success" in log_text


def test_init_resolves_relative_workspace_and_external_root_to_absolute_paths(
    tmp_path: Path,
) -> None:
    runner_dir = tmp_path / "runner"
    runner_dir.mkdir()

    result = _run_hikbox(
        "init",
        "--workspace",
        "workspace",
        "--external-root",
        "external-root",
        cwd=runner_dir,
    )

    assert result.returncode == 0

    config_path = runner_dir / "workspace" / ".hikbox" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["config_version"] == 1
    assert isinstance(config["config_version"], int)
    assert config["external_root"] == str((runner_dir / "external-root").resolve())


def test_repeated_init_fails_without_overwriting_existing_files_or_logs(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"

    first_result = _run_hikbox(
        "init",
        "--workspace",
        str(workspace),
        "--external-root",
        str(external_root),
    )
    assert first_result.returncode == 0

    hikbox_dir = workspace / ".hikbox"
    config_path = hikbox_dir / "config.json"
    library_db_path = hikbox_dir / "library.db"
    embedding_db_path = hikbox_dir / "embedding.db"
    log_path = _log_files(external_root / "logs")[0]

    original_config = config_path.read_bytes()
    original_library_db = library_db_path.read_bytes()
    original_embedding_db = embedding_db_path.read_bytes()
    original_log = log_path.read_bytes()

    second_result = _run_hikbox(
        "init",
        "--workspace",
        str(workspace),
        "--external-root",
        str(external_root),
    )

    assert second_result.returncode != 0
    assert "已存在" in second_result.stderr
    assert config_path.read_bytes() == original_config
    assert library_db_path.read_bytes() == original_library_db
    assert embedding_db_path.read_bytes() == original_embedding_db
    assert log_path.read_bytes() == original_log


def test_init_requires_workspace_and_external_root_arguments(tmp_path: Path) -> None:
    missing_workspace = _run_hikbox(
        "init",
        "--external-root",
        str(tmp_path / "external-root"),
    )
    missing_external_root = _run_hikbox(
        "init",
        "--workspace",
        str(tmp_path / "workspace"),
    )

    assert missing_workspace.returncode != 0
    assert "--workspace" in missing_workspace.stderr
    assert missing_external_root.returncode != 0
    assert "--external-root" in missing_external_root.stderr


def test_init_rolls_back_when_external_root_cannot_be_created(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root_parent = tmp_path / "occupied"
    external_root_parent.write_text("not-a-directory", encoding="utf-8")
    external_root = external_root_parent / "external-root"

    result = _run_hikbox(
        "init",
        "--workspace",
        str(workspace),
        "--external-root",
        str(external_root),
    )

    assert result.returncode != 0
    assert "external_root" in result.stderr
    _assert_tree_absent(workspace / ".hikbox")
    _assert_tree_absent(external_root)


def test_init_fails_cleanly_when_external_root_is_existing_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "existing-file"
    external_root.write_text("occupied", encoding="utf-8")

    result = _run_hikbox(
        "init",
        "--workspace",
        str(workspace),
        "--external-root",
        str(external_root),
    )

    assert result.returncode != 0
    assert "external_root" in result.stderr
    assert "Traceback" not in result.stderr
    _assert_tree_absent(workspace / ".hikbox")


def test_init_rolls_back_when_database_creation_fails_midway(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    python_source = f"""
from pathlib import Path
import runpy
import sqlite3
import sys

real_connect = sqlite3.connect
first_executescript = True


class FailingConnection:
    def __init__(self, connection, database_path: str) -> None:
        self._connection = connection
        self._db_path = Path(database_path)

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._connection.__exit__(exc_type, exc, tb)

    def executescript(self, script: str):
        global first_executescript
        if first_executescript:
            first_executescript = False
            (self._db_path.parent / "stray.tmp").write_text("leftover", encoding="utf-8")
            external_root_path = Path({str(external_root)!r})
            (external_root_path / "root.stray").write_text("leftover", encoding="utf-8")
            logs_dir = external_root_path / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            (logs_dir / "stray.log").write_text("leftover", encoding="utf-8")
            raise sqlite3.DatabaseError("boom after db create")
        return self._connection.executescript(script)

    def close(self) -> None:
        self._connection.close()

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


def failing_connect(database, *args, **kwargs):
    return FailingConnection(real_connect(database, *args, **kwargs), database)


sqlite3.connect = failing_connect
sys.argv = [
    "hikbox-pictures",
    "init",
    "--workspace",
    {str(workspace)!r},
    "--external-root",
    {str(external_root)!r},
]
runpy.run_module("hikbox_pictures", run_name="__main__")
"""

    result = _run_hikbox_with_inline_python(python_source)

    assert result.returncode != 0
    assert "boom after db create" in result.stderr
    _assert_tree_absent(workspace / ".hikbox")
    _assert_tree_absent(external_root)


def test_init_cleans_managed_external_subtrees_when_external_root_preexists(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    external_root.mkdir()
    sentinel_path = external_root / "keep.txt"
    sentinel_path.write_text("keep", encoding="utf-8")
    stray_root_path = external_root / "root.stray"
    python_source = f"""
from pathlib import Path
import runpy
import sqlite3
import sys

real_connect = sqlite3.connect
first_executescript = True


class FailingConnection:
    def __init__(self, connection, database_path: str) -> None:
        self._connection = connection
        self._db_path = Path(database_path)

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._connection.__exit__(exc_type, exc, tb)

    def executescript(self, script: str):
        global first_executescript
        if first_executescript:
            first_executescript = False
            external_root_path = Path({str(external_root)!r})
            (external_root_path / "root.stray").write_text("leftover", encoding="utf-8")
            crops_dir = external_root_path / "artifacts" / "crops"
            crops_dir.mkdir(parents=True, exist_ok=True)
            (crops_dir / "crop.tmp").write_text("leftover", encoding="utf-8")
            logs_dir = external_root_path / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            (logs_dir / "stray.log").write_text("leftover", encoding="utf-8")
            raise sqlite3.DatabaseError("boom with existing external root")
        return self._connection.executescript(script)

    def close(self) -> None:
        self._connection.close()

    def __getattr__(self, name: str):
        return getattr(self._connection, name)


def failing_connect(database, *args, **kwargs):
    return FailingConnection(real_connect(database, *args, **kwargs), database)


sqlite3.connect = failing_connect
sys.argv = [
    "hikbox-pictures",
    "init",
    "--workspace",
    {str(workspace)!r},
    "--external-root",
    {str(external_root)!r},
]
runpy.run_module("hikbox_pictures", run_name="__main__")
"""

    result = _run_hikbox_with_inline_python(python_source)

    assert result.returncode != 0
    assert "boom with existing external root" in result.stderr
    _assert_tree_absent(workspace / ".hikbox")
    assert external_root.is_dir()
    assert sentinel_path.read_text(encoding="utf-8") == "keep"
    assert not stray_root_path.exists()
    _assert_tree_absent(external_root / "artifacts")
    _assert_tree_absent(external_root / "logs")
