"""SQLite 连接封装。"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect_sqlite(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
