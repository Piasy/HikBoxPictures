"""API 路由与错误包裹。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from hikbox_pictures.product.export.run_service import ExportRunningLockError
from hikbox_pictures.product.export.template_service import _UNCHANGED
from hikbox_pictures.product.people.service import (
    PeopleExcludeConflictError,
    PeopleMergeError,
    PeopleNotFoundError,
    PeopleUndoMergeError,
    PeopleUndoMergeConflictError,
)
from hikbox_pictures.product.scan.errors import (
    InvalidRunKindError,
    InvalidTriggeredByError,
    ScanActiveConflictError,
    SessionNotFoundError,
)
from hikbox_pictures.product.service_registry import ServiceContainer


def build_api_router() -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.post("/scan/start_or_resume")
    async def scan_start_or_resume(request: Request) -> JSONResponse:
        services = _services(request)
        payload = await request.json()
        try:
            result = services.scan_sessions.start_or_resume(
                run_kind=str(payload.get("run_kind", "")),
                triggered_by=str(payload.get("triggered_by", "")),
            )
            session = _run_scan_session_until_terminal(
                services,
                session_id=result.session_id,
                should_execute=result.should_execute,
            )
            return _ok(
                {
                    "session_id": result.session_id,
                    "status": session.status,
                    "resumed": result.resumed,
                }
            )
        except (InvalidRunKindError, InvalidTriggeredByError) as exc:
            return _error("VALIDATION_ERROR", str(exc), status_code=422)

    @router.post("/scan/start_new")
    async def scan_start_new(request: Request) -> JSONResponse:
        services = _services(request)
        payload = await request.json()
        try:
            result = services.scan_sessions.start_new(
                run_kind=str(payload.get("run_kind", "")),
                triggered_by=str(payload.get("triggered_by", "")),
            )
            session = _run_scan_session_until_terminal(
                services,
                session_id=result.session_id,
                should_execute=result.should_execute,
            )
            return _ok({"session_id": result.session_id, "status": session.status})
        except ScanActiveConflictError as exc:
            return _error("SCAN_ACTIVE_CONFLICT", str(exc), status_code=409)
        except (InvalidRunKindError, InvalidTriggeredByError) as exc:
            return _error("VALIDATION_ERROR", str(exc), status_code=422)

    @router.post("/scan/abort")
    async def scan_abort(request: Request) -> JSONResponse:
        services = _services(request)
        payload = await request.json()
        session_id = int(payload.get("session_id", 0))
        try:
            result = services.scan_sessions.abort(session_id)
            return _ok({"session_id": result.id, "status": result.status})
        except SessionNotFoundError as exc:
            return _error("SCAN_SESSION_NOT_FOUND", str(exc), status_code=404)

    @router.get("/scan/{session_id}/audit-items")
    def scan_audit_items(request: Request, session_id: int) -> JSONResponse:
        services = _services(request)
        try:
            services.scan_session_repo.get_session(session_id)
        except SessionNotFoundError as exc:
            return _error("SCAN_SESSION_NOT_FOUND", str(exc), status_code=404)
        items = services.read_model.list_audit_items(scan_session_id=session_id)
        return _ok({"items": items})

    @router.post("/people/{person_id}/actions/rename")
    async def people_rename(request: Request, person_id: int) -> JSONResponse:
        services = _services(request)
        payload = await request.json()
        try:
            person = services.people.rename_person(person_id, str(payload.get("display_name", "")))
            return _ok({"person_id": person.id, "display_name": person.display_name, "is_named": person.is_named})
        except ValueError as exc:
            return _error("VALIDATION_ERROR", str(exc), status_code=422)
        except PeopleNotFoundError as exc:
            return _error("NOT_FOUND", str(exc), status_code=404)

    @router.post("/people/{person_id}/actions/exclude-assignment")
    async def people_exclude_assignment(request: Request, person_id: int) -> JSONResponse:
        services = _services(request)
        payload = await request.json()
        try:
            face_observation_id = int(payload.get("face_observation_id", 0))
            result = services.people.exclude_face(person_id=person_id, face_observation_id=face_observation_id)
            return _ok(
                {
                    "person_id": result.person_id,
                    "face_observation_id": face_observation_id,
                    "pending_reassign": 1,
                }
            )
        except ValueError as exc:
            return _error("VALIDATION_ERROR", str(exc), status_code=422)
        except PeopleExcludeConflictError as exc:
            return _error("ILLEGAL_STATE", str(exc), status_code=409)
        except PeopleNotFoundError as exc:
            return _error("NOT_FOUND", str(exc), status_code=404)
        except ExportRunningLockError as exc:
            return _error(exc.error_code, str(exc), status_code=409)

    @router.post("/people/{person_id}/actions/exclude-assignments")
    async def people_exclude_assignments(request: Request, person_id: int) -> JSONResponse:
        services = _services(request)
        payload = await request.json()
        try:
            face_observation_ids = list(payload.get("face_observation_ids", []))
            result = services.people.exclude_faces(person_id=person_id, face_observation_ids=face_observation_ids)
            return _ok({"person_id": result.person_id, "excluded_count": len(result.face_observation_ids)})
        except ValueError as exc:
            return _error("VALIDATION_ERROR", str(exc), status_code=422)
        except PeopleExcludeConflictError as exc:
            return _error("ILLEGAL_STATE", str(exc), status_code=409)
        except PeopleNotFoundError as exc:
            return _error("NOT_FOUND", str(exc), status_code=404)
        except ExportRunningLockError as exc:
            return _error(exc.error_code, str(exc), status_code=409)

    @router.post("/people/actions/merge-batch")
    async def people_merge_batch(request: Request) -> JSONResponse:
        services = _services(request)
        payload = await request.json()
        try:
            result = services.people.merge_people(list(payload.get("selected_person_ids", [])))
            person = services.read_model.get_person_detail(result.winner_person_id)["person"]
            winner_person_uuid = "" if person is None else str(person["person_uuid"])
            return _ok(
                {
                    "merge_operation_id": result.merge_operation_id,
                    "winner_person_id": result.winner_person_id,
                    "winner_person_uuid": winner_person_uuid,
                }
            )
        except PeopleMergeError as exc:
            return _error("VALIDATION_ERROR", str(exc), status_code=422)
        except PeopleNotFoundError as exc:
            return _error("NOT_FOUND", str(exc), status_code=404)
        except ExportRunningLockError as exc:
            return _error(exc.error_code, str(exc), status_code=409)

    @router.post("/people/actions/undo-last-merge")
    def people_undo_last_merge(request: Request) -> JSONResponse:
        services = _services(request)
        try:
            result = services.people.undo_last_merge()
            return _ok({"merge_operation_id": result.merge_operation_id, "status": "undone"})
        except PeopleUndoMergeConflictError as exc:
            return _error("ILLEGAL_STATE", str(exc), status_code=409)
        except PeopleUndoMergeError as exc:
            return _error("MERGE_OPERATION_NOT_FOUND", str(exc), status_code=404)
        except ExportRunningLockError as exc:
            return _error(exc.error_code, str(exc), status_code=409)

    @router.get("/export/templates")
    def export_templates_list(request: Request) -> JSONResponse:
        services = _services(request)
        raw_limit = request.query_params.get("limit")
        if raw_limit is not None:
            try:
                limit = int(raw_limit)
            except ValueError:
                return _error("VALIDATION_ERROR", "limit 必须是正整数", status_code=422)
            if limit <= 0:
                return _error("VALIDATION_ERROR", "limit 必须是正整数", status_code=422)
        else:
            limit = None
        items = services.export_templates.list_templates()
        if limit is not None:
            items = items[:limit]
        return _ok({"items": [item.__dict__ for item in items]})

    @router.post("/export/templates")
    async def export_templates_create(request: Request) -> JSONResponse:
        services = _services(request)
        payload = await request.json()
        try:
            result = services.export_templates.create_template(
                name=str(payload.get("name", "")),
                output_root=str(payload.get("output_root", "")),
                person_ids=list(payload.get("person_ids", [])),
                enabled=bool(payload.get("enabled", True)),
            )
            return _ok({"template_id": result.id})
        except ValueError as exc:
            return _error("VALIDATION_ERROR", str(exc), status_code=422)
        except Exception as exc:
            code = _export_error_code(exc)
            return _error(code, str(exc), status_code=_export_status_code(code))

    @router.put("/export/templates/{template_id}")
    async def export_templates_update(request: Request, template_id: int) -> JSONResponse:
        services = _services(request)
        payload = await request.json()
        try:
            result = services.export_templates.update_template(
                template_id,
                name=payload.get("name", _UNCHANGED),
                output_root=payload.get("output_root", _UNCHANGED),
                enabled=payload.get("enabled", _UNCHANGED),
                person_ids=payload.get("person_ids", _UNCHANGED),
            )
            return _ok({"template_id": result.id, "updated": True})
        except Exception as exc:
            code = _export_error_code(exc)
            return _error(code, str(exc), status_code=_export_status_code(code))

    @router.post("/export/templates/{template_id}/actions/run")
    def export_template_run(request: Request, template_id: int) -> JSONResponse:
        services = _services(request)
        try:
            result = services.export_runs.start_run(template_id)
            return _ok({"export_run_id": result.export_run_id, "status": result.status})
        except Exception as exc:
            code = _export_error_code(exc)
            return _error(code, str(exc), status_code=_export_status_code(code))

    @router.post("/export/runs/{export_run_id}/actions/execute")
    def export_run_execute(request: Request, export_run_id: int) -> JSONResponse:
        services = _services(request)
        try:
            result = services.export_runs.execute_run(export_run_id)
            return _ok(
                {
                    "export_run_id": result.export_run_id,
                    "status": result.status,
                    "exported_count": result.exported_count,
                    "skipped_exists_count": result.skipped_exists_count,
                    "failed_count": result.failed_count,
                }
            )
        except Exception as exc:
            code = _export_error_code(exc)
            return _error(code, str(exc), status_code=_export_status_code(code))

    return router


def _services(request: Request) -> ServiceContainer:
    return request.app.state.services


def _run_scan_session_until_terminal(services: ServiceContainer, *, session_id: int, should_execute: bool):
    session = services.scan_session_repo.get_session(session_id)
    if not should_execute:
        return session
    if session.status == "pending":
        session = services.scan_session_repo.update_status(session_id, status="running")
    services.scan_execution.run_session(scan_session_id=session_id)
    return services.scan_session_repo.get_session(session_id)


def _ok(data: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"ok": True, "data": data})


def _error(code: str, message: str, *, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"ok": False, "error": {"code": code, "message": message}},
    )


def _export_error_code(exc: Exception) -> str:
    name = exc.__class__.__name__
    if name == "ExportTemplateNotFoundError":
        return "EXPORT_TEMPLATE_NOT_FOUND"
    if name == "ExportRunNotFoundError":
        return "EXPORT_RUN_NOT_FOUND"
    if name == "ExportTemplateDuplicateError":
        return "EXPORT_TEMPLATE_DUPLICATE"
    if name == "ExportValidationError":
        return "VALIDATION_ERROR"
    return "ILLEGAL_STATE"


def _export_status_code(code: str) -> int:
    if code == "VALIDATION_ERROR":
        return 422
    if code in {"EXPORT_TEMPLATE_NOT_FOUND", "EXPORT_RUN_NOT_FOUND"}:
        return 404
    return 409
