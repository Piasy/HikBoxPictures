from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Query, Request

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.services.web_query_service import WebQueryService

router = APIRouter()


@router.get("/logs/events")
def list_events(request: Request, limit: int = Query(default=50, ge=1, le=1000)) -> list[dict[str, object]]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        return WebQueryService(conn).list_events(limit=limit)
    finally:
        conn.close()
