from __future__ import annotations

from pathlib import Path

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
