from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .conftest import run_cli


def test_db_vacuum_library_and_embedding(cli_bin: str, workspace: Path) -> None:
    assert run_cli(cli_bin, "init", "--workspace", str(workspace)).returncode == 0

    lib_db = workspace / ".hikbox" / "library.db"
    emb_db = workspace / ".hikbox" / "embedding.db"

    with sqlite3.connect(lib_db) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS vacuum_probe (id INTEGER PRIMARY KEY, payload TEXT)")
        conn.executemany("INSERT INTO vacuum_probe(payload) VALUES (?)", [("x" * 2000,) for _ in range(200)])
        conn.commit()
        conn.execute("DELETE FROM vacuum_probe")
        conn.commit()

    lib_mtime_before = lib_db.stat().st_mtime_ns
    emb_mtime_before = emb_db.stat().st_mtime_ns

    vacuum = run_cli(cli_bin, "--json", "db", "vacuum", "--library", "--embedding", "--workspace", str(workspace))
    assert vacuum.returncode == 0
    body = json.loads(vacuum.stdout)
    assert body["ok"] is True

    assert lib_db.stat().st_mtime_ns >= lib_mtime_before
    assert emb_db.stat().st_mtime_ns >= emb_mtime_before
