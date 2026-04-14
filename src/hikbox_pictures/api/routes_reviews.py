from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi import HTTPException

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.services.action_service import ActionService
from hikbox_pictures.services.web_query_service import WebQueryService

router = APIRouter()


@router.get("/reviews")
def list_reviews(request: Request) -> list[dict[str, object]]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        return WebQueryService(conn).list_reviews()
    finally:
        conn.close()


@router.post("/reviews/{review_id}/actions/dismiss")
def dismiss_review(review_id: int, request: Request) -> dict[str, object]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        return ActionService(conn).dismiss_review(review_id=review_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        conn.close()


@router.post("/reviews/{review_id}/actions/resolve")
def resolve_review(review_id: int, request: Request) -> dict[str, object]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        return ActionService(conn).resolve_review(review_id=review_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        conn.close()


@router.post("/reviews/{review_id}/actions/ignore")
def ignore_review(review_id: int, request: Request) -> dict[str, object]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        return ActionService(conn).ignore_review(review_id=review_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        conn.close()
