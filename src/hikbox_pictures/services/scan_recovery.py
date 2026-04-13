from __future__ import annotations

from pathlib import Path

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.repositories import ScanRepo
from hikbox_pictures.services.runtime import initialize_workspace


def mark_stale_running_sessions(workspace: Path, stale_after_seconds: int = 900) -> int:
    paths = initialize_workspace(workspace)
    conn = connect_db(paths.db_path)
    try:
        changed = ScanRepo(conn).mark_stale_running_as_interrupted(stale_after_seconds=stale_after_seconds)
        conn.commit()
        return changed
    finally:
        conn.close()
