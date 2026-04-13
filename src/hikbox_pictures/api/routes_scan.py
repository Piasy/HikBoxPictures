from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.services.scan_orchestrator import ScanOrchestrator

router = APIRouter()


def _attach_source_progress(payload: dict[str, object]) -> dict[str, object]:
    sources_obj = payload.get("sources")
    if not isinstance(sources_obj, list):
        return payload
    for source in sources_obj:
        if not isinstance(source, dict):
            continue
        source["progress"] = {
            "discovered": int(source.get("discovered_count", 0)),
            "metadata_done": int(source.get("metadata_done_count", 0)),
            "faces_done": int(source.get("faces_done_count", 0)),
            "embeddings_done": int(source.get("embeddings_done_count", 0)),
            "assignment_done": int(source.get("assignment_done_count", 0)),
        }
    return payload


@router.get("/scan/status")
def scan_status(request: Request) -> dict[str, object]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        return _attach_source_progress(ScanOrchestrator(conn).get_status())
    finally:
        conn.close()


@router.post("/scan/start_or_resume")
def scan_start_or_resume(request: Request) -> dict[str, object]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        orchestrator = ScanOrchestrator(conn)
        session_id = orchestrator.start_or_resume()
        session = orchestrator.scan_repo.get_session(session_id)
        if session is None:
            return {"session_id": session_id, "status": "unknown", "mode": None}
        return {
            "session_id": session_id,
            "status": session["status"],
            "mode": session["mode"],
        }
    finally:
        conn.close()
