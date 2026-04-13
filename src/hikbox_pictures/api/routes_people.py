from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.services.action_service import ActionService
from hikbox_pictures.services.web_query_service import WebQueryService

router = APIRouter()


class RenamePersonRequest(BaseModel):
    display_name: str


class MergePersonRequest(BaseModel):
    target_person_id: int


class SplitAssignmentRequest(BaseModel):
    assignment_id: int
    new_person_display_name: str


class LockAssignmentRequest(BaseModel):
    assignment_id: int


@router.get("/people")
def list_people(request: Request) -> list[dict[str, object]]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        return WebQueryService(conn).list_people()
    finally:
        conn.close()


@router.post("/people/{person_id}/actions/rename")
def rename_person(person_id: int, payload: RenamePersonRequest, request: Request) -> dict[str, object]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        return ActionService(conn).rename_person(person_id=person_id, display_name=payload.display_name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        conn.close()


@router.post("/people/{person_id}/actions/merge")
def merge_person(person_id: int, payload: MergePersonRequest, request: Request) -> dict[str, object]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        return ActionService(conn).merge_person(source_person_id=person_id, target_person_id=payload.target_person_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        conn.close()


@router.post("/people/{person_id}/actions/split")
def split_person_assignment(person_id: int, payload: SplitAssignmentRequest, request: Request) -> dict[str, object]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        return ActionService(conn).split_person_assignment(
            person_id=person_id,
            assignment_id=payload.assignment_id,
            new_person_display_name=payload.new_person_display_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        conn.close()


@router.post("/people/{person_id}/actions/lock-assignment")
def lock_person_assignment(person_id: int, payload: LockAssignmentRequest, request: Request) -> dict[str, object]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        return ActionService(conn).lock_person_assignment(
            person_id=person_id,
            assignment_id=payload.assignment_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        conn.close()
