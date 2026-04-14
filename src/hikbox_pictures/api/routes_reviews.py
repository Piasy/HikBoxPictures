from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi import HTTPException
from pydantic import BaseModel

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.services.action_service import ActionService
from hikbox_pictures.services.web_query_service import WebQueryService

router = APIRouter()


class ReviewActionPayload(BaseModel):
    review_ids: list[int] | None = None


class ReviewCreatePersonPayload(ReviewActionPayload):
    display_name: str


class ReviewAssignPersonPayload(ReviewActionPayload):
    person_id: int


@router.get("/reviews")
def list_reviews(request: Request) -> list[dict[str, object]]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        return WebQueryService(conn).list_reviews()
    finally:
        conn.close()


@router.post("/reviews/{review_id}/actions/dismiss")
def dismiss_review(
    review_id: int,
    request: Request,
    payload: ReviewActionPayload | None = None,
) -> dict[str, object]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        review_ids = payload.review_ids if payload is not None else None
        return ActionService(conn).dismiss_review(review_id=review_id, review_ids=review_ids)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        conn.close()


@router.post("/reviews/{review_id}/actions/resolve")
def resolve_review(
    review_id: int,
    request: Request,
    payload: ReviewActionPayload | None = None,
) -> dict[str, object]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        review_ids = payload.review_ids if payload is not None else None
        return ActionService(conn).resolve_review(review_id=review_id, review_ids=review_ids)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        conn.close()


@router.post("/reviews/{review_id}/actions/create-person")
def create_person_from_review(
    review_id: int,
    payload: ReviewCreatePersonPayload,
    request: Request,
) -> dict[str, object]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        return ActionService(conn).create_person_from_review(
            review_id=review_id,
            review_ids=payload.review_ids,
            display_name=payload.display_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        conn.close()


@router.post("/reviews/{review_id}/actions/assign-person")
def assign_review_to_existing_person(
    review_id: int,
    payload: ReviewAssignPersonPayload,
    request: Request,
) -> dict[str, object]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        return ActionService(conn).assign_review_to_existing_person(
            review_id=review_id,
            review_ids=payload.review_ids,
            person_id=payload.person_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        conn.close()


@router.post("/reviews/{review_id}/actions/ignore")
def ignore_review(
    review_id: int,
    request: Request,
    payload: ReviewActionPayload | None = None,
) -> dict[str, object]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        review_ids = payload.review_ids if payload is not None else None
        return ActionService(conn).ignore_review(review_id=review_id, review_ids=review_ids)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        conn.close()
