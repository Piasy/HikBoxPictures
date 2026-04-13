from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
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

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.repositories.scan_repo import ScanRepo
from hikbox_pictures.services.scan_orchestrator import ScanOrchestrator


def test_start_or_resume_uses_latest_resumable_session(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        interrupted_id = ws.scan_repo.create_session(mode="resume", status="interrupted", started=True)
        ws.scan_repo.create_session(mode="incremental", status="abandoned", started=True)
        ws.conn.commit()

        before_count = ws.scan_repo.count()
        session_id = ScanOrchestrator(ws.conn).start_or_resume()

        assert session_id == interrupted_id
        assert ws.scan_repo.count() == before_count
        session = ws.scan_repo.get_session(session_id)
        assert session is not None
        assert session["status"] == "running"
    finally:
        ws.close()


def test_start_or_resume_creates_new_session_when_no_resumable(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        ws.conn.execute(
            """
            UPDATE scan_session
            SET status = 'completed',
                finished_at = CURRENT_TIMESTAMP
            WHERE status IN ('pending', 'running', 'paused', 'interrupted')
            """
        )
        ws.conn.commit()

        before_count = ws.scan_repo.count()
        session_id = ScanOrchestrator(ws.conn).start_or_resume()

        assert ws.scan_repo.count() == before_count + 1
        session = ws.scan_repo.get_session(session_id)
        assert session is not None
        assert session["mode"] == "incremental"
        assert session["status"] == "running"
        assert len(ws.scan_repo.list_session_sources(session_id)) == len(ws.source_repo.list_sources(active=True))
    finally:
        ws.close()


def test_write_checkpoint_persists_checkpoint_and_source_heartbeat(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        session_id = ScanOrchestrator(ws.conn).start_or_resume()
        sources = ws.scan_repo.list_session_sources(session_id)
        assert sources
        session_source_id = int(sources[0]["id"])

        ScanOrchestrator(ws.conn).write_checkpoint(
            session_source_id,
            phase="discover",
            cursor_json='{"offset": 3}',
            pending_asset_count=8,
        )

        checkpoint = ws.scan_repo.latest_checkpoint_for_source(session_source_id)
        assert checkpoint is not None
        assert checkpoint["phase"] == "discover"
        assert checkpoint["cursor_json"] == '{"offset": 3}'
        assert checkpoint["pending_asset_count"] == 8

        source_state = ws.scan_repo.get_session_source(session_source_id)
        assert source_state is not None
        assert source_state["cursor_json"] == '{"offset": 3}'
        assert source_state["last_checkpoint_at"] is not None
    finally:
        ws.close()


def test_start_or_resume_is_idempotent_under_concurrency(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        ws.conn.execute(
            """
            UPDATE scan_session
            SET status = 'completed',
                finished_at = CURRENT_TIMESTAMP
            WHERE status IN ('pending', 'running', 'paused', 'interrupted')
            """
        )
        ws.conn.commit()
        db_path = ws.paths.db_path
    finally:
        ws.close()

    def run_once() -> int:
        conn = connect_db(db_path)
        try:
            return ScanOrchestrator(conn).start_or_resume()
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(run_once) for _ in range(8)]
        session_ids = [future.result() for future in futures]

    assert len(set(session_ids)) == 1

    conn = connect_db(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) AS c FROM scan_session WHERE status = 'running'").fetchone()
        assert row is not None
        assert int(row["c"]) == 1
        running = ScanRepo(conn).latest_running_session()
        assert running is not None
        assert int(running["id"]) == session_ids[0]
    finally:
        conn.close()
