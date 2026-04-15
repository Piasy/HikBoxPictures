from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_seed_workspace = _MODULE.build_seed_workspace
build_empty_workspace = _MODULE.build_empty_workspace

from hikbox_pictures.services.scan_recovery import mark_stale_running_sessions


def test_stale_running_session_marked_interrupted(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        running_id = ws.scan_repo.create_session(mode="incremental", status="running", started=True)
        sources = ws.source_repo.list_sources(active=True)
        source_a = int(sources[0]["id"])
        source_b = int(sources[1]["id"])
        source_c = ws.source_repo.add_source("USB", "/data/c", root_fingerprint="fp-c", active=True)

        session_source_running = ws.scan_repo.create_session_source(running_id, source_a, status="running")
        session_source_paused = ws.scan_repo.create_session_source(running_id, source_b, status="paused")
        session_source_pending = ws.scan_repo.create_session_source(running_id, source_c, status="pending")
        ws.conn.execute(
            "UPDATE scan_session SET started_at = datetime('now', '-2 hours') WHERE id = ?",
            (running_id,),
        )
        ws.conn.execute(
            """
            UPDATE scan_session_source
            SET last_checkpoint_at = datetime('now', '-2 hours'),
                updated_at = datetime('now', '-2 hours')
            WHERE scan_session_id = ?
            """,
            (running_id,),
        )
        ws.conn.commit()

        changed = mark_stale_running_sessions(ws.root, stale_after_seconds=60)

        assert changed == 1
        session = ws.scan_repo.get_session(running_id)
        assert session is not None
        assert session["status"] == "interrupted"
        assert session["stopped_at"] is not None
        state_running = ws.scan_repo.get_session_source(session_source_running)
        state_paused = ws.scan_repo.get_session_source(session_source_paused)
        state_pending = ws.scan_repo.get_session_source(session_source_pending)
        assert state_running is not None
        assert state_paused is not None
        assert state_pending is not None
        assert state_running["status"] == "interrupted"
        assert state_paused["status"] == "interrupted"
        assert state_pending["status"] == "interrupted"
    finally:
        ws.close()


def test_stale_running_session_not_marked_when_fresh(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        running_id = ws.scan_repo.create_session(mode="incremental", status="running", started=True)
        source_id = int(ws.source_repo.list_sources(active=True)[0]["id"])
        session_source_id = ws.scan_repo.create_session_source(running_id, source_id, status="running")
        ws.conn.execute(
            "UPDATE scan_session_source SET last_checkpoint_at = CURRENT_TIMESTAMP WHERE id = ?",
            (session_source_id,),
        )
        ws.conn.commit()

        changed = mark_stale_running_sessions(ws.root, stale_after_seconds=3600)

        assert changed == 0
        session = ws.scan_repo.get_session(running_id)
        assert session is not None
        assert session["status"] == "running"
    finally:
        ws.close()


def test_mark_stale_running_sessions_reads_initialized_empty_workspace(tmp_path) -> None:
    workspace = build_empty_workspace(tmp_path)
    changed = mark_stale_running_sessions(workspace, stale_after_seconds=60)
    assert changed == 0
    assert (workspace / ".hikbox" / "library.db").exists()
