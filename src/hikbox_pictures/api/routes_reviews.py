from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.services.web_query_service import WebQueryService

router = APIRouter()


@router.get("/reviews")
def list_reviews(request: Request) -> list[dict[str, object]]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        return WebQueryService(conn).list_reviews()
    finally:
        conn.close()
