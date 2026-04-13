from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Query, Request

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.services.observability_service import ObservabilityService

router = APIRouter()


@router.get("/logs/events")
def list_events(
    request: Request,
    limit: int = Query(default=50, ge=1, le=1000),
    run_kind: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    run_id: str | None = Query(default=None),
    level: str | None = Query(default=None),
) -> list[dict[str, object]]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        return ObservabilityService(conn, workspace=Path(request.app.state.workspace)).list_events(
            limit=limit,
            run_kind=run_kind,
            event_type=event_type,
            run_id=run_id,
            level=level,
        )
    finally:
        conn.close()
