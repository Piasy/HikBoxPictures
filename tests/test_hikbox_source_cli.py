from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
ISO_8601_UTC_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


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


def _init_workspace(workspace: Path, external_root: Path) -> subprocess.CompletedProcess[str]:
    return _run_hikbox(
        "init",
        "--workspace",
        str(workspace),
        "--external-root",
        str(external_root),
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


def _read_sources(db_path: Path) -> list[dict[str, object]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, path, label, active, created_at
            FROM library_sources
            ORDER BY id ASC
            """
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _read_log_texts(logs_dir: Path) -> list[str]:
    return [
        path.read_text(encoding="utf-8")
        for path in sorted(logs_dir.iterdir())
        if path.is_file()
    ]


def test_source_add_persists_record_and_source_list_returns_json_and_success_log(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    source_dir = tmp_path / "photos-family"
    source_dir.mkdir()

    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    add_result = _run_hikbox(
        "source",
        "add",
        "--workspace",
        str(workspace),
        str(source_dir),
        "--label",
        "family",
    )

    assert add_result.returncode == 0
    assert add_result.stdout == ""
    assert add_result.stderr == ""

    db_sources = _read_sources(workspace / ".hikbox" / "library.db")
    assert len(db_sources) == 1
    created_at = str(db_sources[0]["created_at"])
    assert db_sources[0] == {
        "id": 1,
        "path": str(source_dir.resolve()),
        "label": "family",
        "active": 1,
        "created_at": created_at,
    }
    assert ISO_8601_UTC_PATTERN.match(created_at)

    list_result = _run_hikbox(
        "source",
        "list",
        "--workspace",
        str(workspace),
    )

    assert list_result.returncode == 0
    assert list_result.stderr == ""
    assert json.loads(list_result.stdout) == {
        "sources": [
            {
                "id": 1,
                "label": "family",
                "path": str(source_dir.resolve()),
                "active": True,
                "created_at": created_at,
            }
        ]
    }

    log_texts = _read_log_texts(external_root / "logs")
    assert any("hikbox-pictures source add" in log_text for log_text in log_texts)
    assert any(str(source_dir.resolve()) in log_text for log_text in log_texts)
    assert any('"label": "family"' in log_text for log_text in log_texts)
    assert any('"result": "success"' in log_text for log_text in log_texts)


def test_source_list_returns_exact_empty_json_before_any_source_is_added(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"

    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    list_result = _run_hikbox(
        "source",
        "list",
        "--workspace",
        str(workspace),
    )

    assert list_result.returncode == 0
    assert list_result.stderr == ""
    assert list_result.stdout == '{"sources": []}'


def test_source_add_and_list_fail_without_initialized_workspace_and_do_not_create_hikbox_dir(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    source_dir = tmp_path / "photos"
    source_dir.mkdir()

    add_result = _run_hikbox(
        "source",
        "add",
        "--workspace",
        str(workspace),
        str(source_dir),
        "--label",
        "family",
    )
    list_result = _run_hikbox(
        "source",
        "list",
        "--workspace",
        str(workspace),
    )

    assert add_result.returncode != 0
    assert "工作区" in add_result.stderr
    assert "Traceback" not in add_result.stderr
    assert list_result.returncode != 0
    assert "工作区" in list_result.stderr
    assert "Traceback" not in list_result.stderr
    assert not (workspace / ".hikbox").exists()


def test_source_commands_require_workspace_and_label_arguments(tmp_path: Path) -> None:
    source_dir = tmp_path / "photos"
    source_dir.mkdir()

    add_missing_workspace_result = _run_hikbox(
        "source",
        "add",
        str(source_dir),
        "--label",
        "family",
    )
    add_missing_label_result = _run_hikbox(
        "source",
        "add",
        "--workspace",
        str(tmp_path / "workspace"),
        str(source_dir),
    )
    list_missing_workspace_result = _run_hikbox("source", "list")

    assert add_missing_workspace_result.returncode != 0
    assert "--workspace" in add_missing_workspace_result.stderr
    assert add_missing_label_result.returncode != 0
    assert "--label" in add_missing_label_result.stderr
    assert list_missing_workspace_result.returncode != 0
    assert "--workspace" in list_missing_workspace_result.stderr


def test_source_commands_fail_when_workspace_config_or_database_is_missing(tmp_path: Path) -> None:
    workspace_missing_config = tmp_path / "workspace-missing-config"
    external_root_missing_config = tmp_path / "external-root-missing-config"
    workspace_missing_db = tmp_path / "workspace-missing-db"
    external_root_missing_db = tmp_path / "external-root-missing-db"
    source_dir = tmp_path / "photos"
    source_dir.mkdir()

    init_missing_config_result = _init_workspace(
        workspace_missing_config,
        external_root_missing_config,
    )
    assert init_missing_config_result.returncode == 0

    config_path = workspace_missing_config / ".hikbox" / "config.json"
    config_path.unlink()
    add_missing_config_result = _run_hikbox(
        "source",
        "add",
        "--workspace",
        str(workspace_missing_config),
        str(source_dir),
        "--label",
        "family",
    )

    assert add_missing_config_result.returncode != 0
    assert "工作区" in add_missing_config_result.stderr

    init_missing_db_result = _init_workspace(
        workspace_missing_db,
        external_root_missing_db,
    )
    assert init_missing_db_result.returncode == 0

    library_db_path = workspace_missing_db / ".hikbox" / "library.db"
    library_db_path.unlink()
    list_missing_db_result = _run_hikbox(
        "source",
        "list",
        "--workspace",
        str(workspace_missing_db),
    )

    assert list_missing_db_result.returncode != 0
    assert "工作区" in list_missing_db_result.stderr


def test_source_list_fails_cleanly_when_workspace_config_is_invalid_utf8(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-invalid-config"
    external_root = tmp_path / "external-root-invalid-config"

    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    config_path = workspace / ".hikbox" / "config.json"
    config_path.write_bytes(b"\x80not-utf8")

    list_result = _run_hikbox(
        "source",
        "list",
        "--workspace",
        str(workspace),
    )

    assert list_result.returncode != 0
    assert "工作区配置" in list_result.stderr
    assert "Traceback" not in list_result.stderr


def test_source_list_fails_cleanly_when_sqlite_connect_raises_for_library_db(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace-list-db-connect-fail"
    external_root = tmp_path / "external-root-list-db-connect-fail"

    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    library_db_path = workspace / ".hikbox" / "library.db"
    python_source = f"""
from pathlib import Path
import runpy
import sqlite3
import sys

real_connect = sqlite3.connect
target_db_path = str(Path({str(library_db_path)!r}).resolve())


def failing_connect(database, *args, **kwargs):
    if str(Path(database).resolve()) == target_db_path:
        raise sqlite3.OperationalError("unable to open database file")
    return real_connect(database, *args, **kwargs)


sqlite3.connect = failing_connect
sys.argv = [
    "hikbox-pictures",
    "source",
    "list",
    "--workspace",
    {str(workspace)!r},
]
runpy.run_module("hikbox_pictures", run_name="__main__")
"""
    list_result = _run_hikbox_with_inline_python(python_source)

    assert list_result.returncode != 0
    assert "工作区数据库无法打开" in list_result.stderr
    assert "Traceback" not in list_result.stderr


def test_source_add_rejects_invalid_paths_and_blank_label_without_inserting_rows(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    valid_source_dir = tmp_path / "photos-valid"
    valid_source_dir.mkdir()
    missing_source_dir = tmp_path / "photos-missing"
    file_source_path = tmp_path / "photos.txt"
    file_source_path.write_text("not-a-directory", encoding="utf-8")
    unreadable_source_dir = tmp_path / "photos-unreadable"
    unreadable_source_dir.mkdir()

    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    unreadable_original_mode = unreadable_source_dir.stat().st_mode
    unreadable_source_dir.chmod(0)
    try:
        missing_result = _run_hikbox(
            "source",
            "add",
            "--workspace",
            str(workspace),
            str(missing_source_dir),
            "--label",
            "missing",
        )
        file_result = _run_hikbox(
            "source",
            "add",
            "--workspace",
            str(workspace),
            str(file_source_path),
            "--label",
            "file",
        )
        unreadable_result = _run_hikbox(
            "source",
            "add",
            "--workspace",
            str(workspace),
            str(unreadable_source_dir),
            "--label",
            "unreadable",
        )
        blank_label_result = _run_hikbox(
            "source",
            "add",
            "--workspace",
            str(workspace),
            str(valid_source_dir),
            "--label",
            "   ",
        )
    finally:
        unreadable_source_dir.chmod(unreadable_original_mode)

    assert missing_result.returncode != 0
    assert "不存在" in missing_result.stderr
    assert file_result.returncode != 0
    assert "目录" in file_result.stderr
    assert unreadable_result.returncode != 0
    assert "不可读" in unreadable_result.stderr
    assert blank_label_result.returncode != 0
    assert "label" in blank_label_result.stderr
    assert "Traceback" not in blank_label_result.stderr
    assert _read_sources(workspace / ".hikbox" / "library.db") == []


def test_source_add_fails_cleanly_when_sqlite_connect_raises_for_library_db(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace-add-db-connect-fail"
    external_root = tmp_path / "external-root-add-db-connect-fail"
    source_dir = tmp_path / "photos-connect-fail"
    source_dir.mkdir()

    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    library_db_path = workspace / ".hikbox" / "library.db"
    python_source = f"""
from pathlib import Path
import runpy
import sqlite3
import sys

real_connect = sqlite3.connect
target_db_path = str(Path({str(library_db_path)!r}).resolve())


def failing_connect(database, *args, **kwargs):
    if str(Path(database).resolve()) == target_db_path:
        raise sqlite3.OperationalError("unable to open database file")
    return real_connect(database, *args, **kwargs)


sqlite3.connect = failing_connect
sys.argv = [
    "hikbox-pictures",
    "source",
    "add",
    "--workspace",
    {str(workspace)!r},
    {str(source_dir)!r},
    "--label",
    "family",
]
runpy.run_module("hikbox_pictures", run_name="__main__")
"""
    add_result = _run_hikbox_with_inline_python(python_source)

    assert add_result.returncode != 0
    assert "工作区数据库无法打开" in add_result.stderr
    assert "Traceback" not in add_result.stderr
    assert _read_sources(workspace / ".hikbox" / "library.db") == []


def test_source_add_rejects_duplicate_path_without_changing_original_label(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    source_dir = tmp_path / "photos-family"
    source_dir.mkdir()

    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    first_add_result = _run_hikbox(
        "source",
        "add",
        "--workspace",
        str(workspace),
        str(source_dir),
        "--label",
        "family",
    )
    duplicate_add_result = _run_hikbox(
        "source",
        "add",
        "--workspace",
        str(workspace),
        str(source_dir),
        "--label",
        "renamed",
    )

    assert first_add_result.returncode == 0
    assert duplicate_add_result.returncode != 0
    assert "已存在" in duplicate_add_result.stderr
    assert _read_sources(workspace / ".hikbox" / "library.db") == [
        {
            "id": 1,
            "path": str(source_dir.resolve()),
            "label": "family",
            "active": 1,
            "created_at": _read_sources(workspace / ".hikbox" / "library.db")[0]["created_at"],
        }
    ]


def test_source_add_fails_cleanly_and_rolls_back_when_success_log_cannot_be_written(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    source_dir = tmp_path / "photos-family"
    source_dir.mkdir()

    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    logs_dir = external_root / "logs"
    for log_path in logs_dir.iterdir():
        log_path.unlink()
    logs_dir.rmdir()
    logs_dir.write_text("occupied", encoding="utf-8")

    add_result = _run_hikbox(
        "source",
        "add",
        "--workspace",
        str(workspace),
        str(source_dir),
        "--label",
        "family",
    )

    assert add_result.returncode != 0
    assert "日志" in add_result.stderr
    assert "Traceback" not in add_result.stderr
    assert _read_sources(workspace / ".hikbox" / "library.db") == []


def test_source_list_returns_multiple_sources_in_id_order(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    source_dir_a = tmp_path / "photos-a"
    source_dir_b = tmp_path / "photos-b"
    source_dir_a.mkdir()
    source_dir_b.mkdir()

    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    first_add_result = _run_hikbox(
        "source",
        "add",
        "--workspace",
        str(workspace),
        str(source_dir_a),
        "--label",
        "family",
    )
    second_add_result = _run_hikbox(
        "source",
        "add",
        "--workspace",
        str(workspace),
        str(source_dir_b),
        "--label",
        "travel",
    )
    list_result = _run_hikbox(
        "source",
        "list",
        "--workspace",
        str(workspace),
    )

    assert first_add_result.returncode == 0
    assert second_add_result.returncode == 0

    db_sources = _read_sources(workspace / ".hikbox" / "library.db")
    assert len(db_sources) == 2
    assert [source["id"] for source in db_sources] == [1, 2]
    assert [source["label"] for source in db_sources] == ["family", "travel"]

    payload = json.loads(list_result.stdout)
    assert list_result.returncode == 0
    assert [source["id"] for source in payload["sources"]] == [1, 2]
    assert [source["path"] for source in payload["sources"]] == [
        str(source_dir_a.resolve()),
        str(source_dir_b.resolve()),
    ]
