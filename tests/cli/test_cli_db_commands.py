from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from tests.cli.conftest import 读取_json输出


def test_db_vacuum_command_updates_database_files(
    已初始化工作区: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
) -> None:
    library_db = 已初始化工作区 / ".hikbox" / "library.db"
    embedding_db = 已初始化工作区 / ".hikbox" / "embedding.db"

    conn = sqlite3.connect(library_db)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS vacuum_probe (id INTEGER PRIMARY KEY, payload TEXT)")
        conn.executemany("INSERT INTO vacuum_probe(payload) VALUES (?)", [("x" * 4000,) for _ in range(200)])
        conn.commit()
        conn.execute("DELETE FROM vacuum_probe")
        conn.commit()
        freelist_before = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    finally:
        conn.close()

    assert freelist_before > 0
    library_mtime_before = library_db.stat().st_mtime_ns
    embedding_mtime_before = embedding_db.stat().st_mtime_ns

    result = 运行_cli(["--json", "db", "vacuum", "--library", "--embedding", "--workspace", str(已初始化工作区)])
    payload = 读取_json输出(result.stdout)

    assert result.returncode == 0
    assert payload["ok"] is True

    conn = sqlite3.connect(library_db)
    try:
        freelist_after = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
    finally:
        conn.close()

    assert freelist_after == 0
    assert library_db.stat().st_mtime_ns > library_mtime_before
    assert embedding_db.stat().st_mtime_ns > embedding_mtime_before
