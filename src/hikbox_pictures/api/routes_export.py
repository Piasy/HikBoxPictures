from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.services.action_service import ActionService
from hikbox_pictures.services.web_query_service import WebQueryService

router = APIRouter()


class ExportTemplatePayload(BaseModel):
    name: str
    output_root: str
    person_ids: list[int]
    include_group: bool = True
    export_live_mov: bool = False
    start_datetime: str | None = None
    end_datetime: str | None = None
    enabled: bool = True


@router.get("/export/templates")
def list_templates(request: Request) -> list[dict[str, object]]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        return WebQueryService(conn).list_export_templates()
    finally:
        conn.close()


@router.post("/export/templates")
def create_template(payload: ExportTemplatePayload, request: Request) -> dict[str, object]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        return ActionService(conn).create_export_template(
            name=payload.name,
            output_root=payload.output_root,
            person_ids=payload.person_ids,
            include_group=payload.include_group,
            export_live_mov=payload.export_live_mov,
            start_datetime=payload.start_datetime,
            end_datetime=payload.end_datetime,
            enabled=payload.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        conn.close()


@router.put("/export/templates/{template_id}")
def update_template(template_id: int, payload: ExportTemplatePayload, request: Request) -> dict[str, object]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        return ActionService(conn).update_export_template(
            template_id=template_id,
            name=payload.name,
            output_root=payload.output_root,
            person_ids=payload.person_ids,
            include_group=payload.include_group,
            export_live_mov=payload.export_live_mov,
            start_datetime=payload.start_datetime,
            end_datetime=payload.end_datetime,
            enabled=payload.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        conn.close()


@router.delete("/export/templates/{template_id}")
def delete_template(template_id: int, request: Request) -> dict[str, object]:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        return ActionService(conn).delete_export_template(template_id=template_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
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
