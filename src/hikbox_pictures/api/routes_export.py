from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.services.action_service import ActionService
from hikbox_pictures.services.web_query_service import WebQueryService

router = APIRouter()


@router.get("/export/templates")
def list_templates(request: Request) -> list[dict[str, object]]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        return WebQueryService(conn).list_export_templates()
    finally:
        conn.close()


@router.get("/export/templates/{template_id}/preview")
def preview_template(template_id: int, request: Request) -> dict[str, object]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        return ActionService(conn).preview_export_template(template_id=template_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        conn.close()


@router.post("/export/templates/{template_id}/actions/run")
def run_template(template_id: int, request: Request) -> dict[str, object]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        return ActionService(conn).run_export_template(template_id=template_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        conn.close()


@router.get("/export/templates/{template_id}/runs")
def list_template_runs(template_id: int, request: Request) -> list[dict[str, object]]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        return ActionService(conn).list_export_template_runs(template_id=template_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        conn.close()
