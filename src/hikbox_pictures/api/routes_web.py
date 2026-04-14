from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.services.web_query_service import WebQueryService

router = APIRouter()


def _get_templates(request: Request):
    return request.app.state.templates


@router.get("/", response_class=HTMLResponse)
def people_page(request: Request) -> HTMLResponse:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        service = WebQueryService(conn)
        return _get_templates(request).TemplateResponse(
            request=request,
            name="people.html",
            context={
                "page_title": "人物库",
                "page_key": "people",
                "people": service.list_people(),
            },
        )
    finally:
        conn.close()


@router.get("/people/{person_id}", response_class=HTMLResponse)
def person_detail_page(person_id: int, request: Request) -> HTMLResponse:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        service = WebQueryService(conn)
        detail = service.get_person_detail(person_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"人物 {person_id} 不存在")
        return _get_templates(request).TemplateResponse(
            request=request,
            name="person_detail.html",
            context={
                "page_title": "人物详情",
                "page_key": "people",
                "person": detail["person"],
                "assignments": detail["assignments"],
                "viewer_items": detail["viewer_items"],
            },
        )
    finally:
        conn.close()


@router.get("/reviews", response_class=HTMLResponse)
def reviews_page(request: Request) -> HTMLResponse:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        service = WebQueryService(conn)
        review_page = service.get_review_page()
        return _get_templates(request).TemplateResponse(
            request=request,
            name="review_queue.html",
            context={
                "page_title": "待审核",
                "page_key": "reviews",
                "queues": review_page["queues"],
                "review_summary": review_page["summary"],
                "assignable_people": review_page["assignable_people"],
                "viewer_items": review_page["viewer_items"],
            },
        )
    finally:
        conn.close()


@router.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request) -> HTMLResponse:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        service = WebQueryService(conn)
        data = service.get_sources_scan_view()
        return _get_templates(request).TemplateResponse(
            request=request,
            name="sources_scan.html",
            context={
                "page_title": "源目录与扫描",
                "page_key": "sources",
                "session": data["session"],
                "session_sources": data["session_sources"],
                "sources": data["sources"],
            },
        )
    finally:
        conn.close()


@router.get("/exports", response_class=HTMLResponse)
def exports_page(request: Request) -> HTMLResponse:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        service = WebQueryService(conn)
        export_page = service.get_export_page()
        workspace_paths = request.app.state.workspace_paths
        return _get_templates(request).TemplateResponse(
            request=request,
            name="export_templates.html",
            context={
                "page_title": "导出模板",
                "page_key": "exports",
                "templates": export_page["templates"],
                "available_people": export_page["available_people"],
                "viewer_items": export_page["viewer_items"],
                "default_output_root": str(workspace_paths.exports_dir),
            },
        )
    finally:
        conn.close()


@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request) -> HTMLResponse:
    conn = connect_db(Path(request.app.state.db_path))
    try:
        service = WebQueryService(conn)
        return _get_templates(request).TemplateResponse(
            request=request,
            name="logs.html",
            context={
                "page_title": "运行日志",
                "page_key": "logs",
                "events": service.list_events(limit=100),
            },
        )
    finally:
        conn.close()
