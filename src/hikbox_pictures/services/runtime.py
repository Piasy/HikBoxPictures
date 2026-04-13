from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.db.migrator import apply_migrations
from hikbox_pictures.workspace import WorkspacePaths, ensure_workspace_layout


def initialize_workspace(workspace: Path) -> WorkspacePaths:
    paths = ensure_workspace_layout(workspace)
    conn = connect_db(paths.db_path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    return paths


def resolve_media_allowed_roots(workspace: Path) -> list[Path]:
    paths = ensure_workspace_layout(workspace)
    roots: list[Path] = [paths.artifacts_dir.resolve()]

    conn = connect_db(paths.db_path)
    try:
        rows: list[Any] = conn.execute(
            """
            SELECT root_path
            FROM library_source
            WHERE active = 1
            """
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()

    for row in rows:
        root_path = str(row["root_path"]).strip()
        if not root_path:
            continue
        roots.append(Path(root_path).expanduser().resolve())

    dedup: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        dedup.append(root)
    return dedup
