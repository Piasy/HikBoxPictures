"""页面路由。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from hikbox_pictures.product.service_registry import ServiceContainer

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


def build_page_router() -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    def people_index(request: Request) -> HTMLResponse:
        services = _services(request)
        return TEMPLATES.TemplateResponse(
            request=request,
            name="people_index.html",
            context={
                "named_people": services.read_model.list_named_people(),
                "anonymous_people": services.read_model.list_anonymous_people(),
            },
        )

    @router.get("/people/{person_id}", response_class=HTMLResponse)
    def people_detail(request: Request, person_id: int) -> HTMLResponse:
        services = _services(request)
        return TEMPLATES.TemplateResponse(
            request=request,
            name="people_detail.html",
            context=services.read_model.get_person_detail(person_id),
        )

    @router.get("/sources", response_class=HTMLResponse)
    def sources(request: Request) -> HTMLResponse:
        services = _services(request)
        return TEMPLATES.TemplateResponse(
            request=request,
            name="sources.html",
            context={"sources": services.read_model.list_sources()},
        )

    @router.get("/sources/{session_id}/audit", response_class=HTMLResponse)
    def audit(request: Request, session_id: int) -> HTMLResponse:
        services = _services(request)
        context = services.read_model.get_scan_audit_page(session_id)
        context["audit_items"] = services.read_model.list_audit_items(scan_session_id=session_id)
        return TEMPLATES.TemplateResponse(request=request, name="audit.html", context=context)

    @router.get("/exports", response_class=HTMLResponse)
    def exports(request: Request) -> HTMLResponse:
        services = _services(request)
        return TEMPLATES.TemplateResponse(
            request=request,
            name="exports.html",
            context=services.read_model.get_export_page(),
        )

    @router.get("/exports/{export_id}", response_class=HTMLResponse)
    def export_detail(request: Request, export_id: int) -> HTMLResponse:
        services = _services(request)
        context = services.read_model.get_export_page()
        context["selected_run"] = services.read_model.get_export_detail(export_id)["run"]
        return TEMPLATES.TemplateResponse(
            request=request,
            name="exports.html",
            context=context,
        )

    @router.get("/logs", response_class=HTMLResponse)
    def logs(
        request: Request,
        scan_session_id: int | None = None,
        export_run_id: int | None = None,
        severity: str | None = None,
    ) -> HTMLResponse:
        services = _services(request)
        return TEMPLATES.TemplateResponse(
            request=request,
            name="logs.html",
            context={
                "scan_session_id": scan_session_id,
                "export_run_id": export_run_id,
                "severity": severity,
                "items": services.read_model.query_logs(
                    scan_session_id=scan_session_id,
                    export_run_id=export_run_id,
                    severity=severity,
                ),
            },
        )

    return router


def _services(request: Request) -> ServiceContainer:
    return request.app.state.services
