from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from hikbox_pictures.product.audit.service import ScanAuditItem
from hikbox_pictures.product.db.connection import connect_sqlite
from hikbox_pictures.product.export import ExportRunLockError, ExportValidationError
from hikbox_pictures.product.export.run_service import assert_people_writes_allowed
from hikbox_pictures.product.people.service import MergeOperationNotFoundError
from hikbox_pictures.product.scan.errors import ScanActiveConflictError, ScanSessionNotFoundError

router = APIRouter()


class ApiContractError(RuntimeError):
    def __init__(self, *, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.code = code
        self.message = message


class ScanStartPayload(BaseModel):
    run_kind: str = "scan_full"


class ScanAbortPayload(BaseModel):
    session_id: int


class RenamePayload(BaseModel):
    display_name: str


class ExcludeAssignmentPayload(BaseModel):
    face_observation_id: int


class ExcludeAssignmentsPayload(BaseModel):
    face_observation_ids: list[int] = Field(default_factory=list)


class MergeBatchPayload(BaseModel):
    selected_person_ids: list[int] = Field(default_factory=list)


class ExportTemplateCreatePayload(BaseModel):
    name: str
    output_root: str
    person_ids: list[int] = Field(default_factory=list)


class ExportTemplateUpdatePayload(BaseModel):
    name: str | None = None
    output_root: str | None = None
    person_ids: list[int] | None = None
    enabled: bool | None = None


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _error(status_code: int, code: str, message: str) -> ApiContractError:
    return ApiContractError(status_code=status_code, code=code, message=message)


def _services(request: Request):
    return request.app.state.services


def _map_people_error(exc: ValueError) -> ApiContractError:
    return _error(400, "VALIDATION_ERROR", str(exc))


def _template_exists(db_path: Path, template_id: int) -> bool:
    with connect_sqlite(db_path) as conn:
        try:
            row = conn.execute("SELECT 1 FROM export_template WHERE id=? LIMIT 1", (int(template_id),)).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table: export_template" in str(exc):
                return False
            raise
    return row is not None


def _scan_session_exists(db_path: Path, session_id: int) -> bool:
    with connect_sqlite(db_path) as conn:
        row = conn.execute("SELECT 1 FROM scan_session WHERE id=? LIMIT 1", (int(session_id),)).fetchone()
    return row is not None


def _template_name_exists_other(db_path: Path, *, template_id: int, template_name: str) -> bool:
    with connect_sqlite(db_path) as conn:
        try:
            row = conn.execute(
                """
                SELECT 1
                FROM export_template
                WHERE name=?
                  AND id<>?
                LIMIT 1
                """,
                (template_name, int(template_id)),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table: export_template" in str(exc):
                return False
            raise
    return row is not None


def _has_active_exclusion(db_path: Path, *, person_id: int, face_observation_id: int) -> bool:
    with connect_sqlite(db_path) as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM person_face_exclusion
            WHERE person_id=?
              AND face_observation_id=?
              AND active=1
            LIMIT 1
            """,
            (int(person_id), int(face_observation_id)),
        ).fetchone()
    return row is not None


@router.post("/scan/start_or_resume")
def scan_start_or_resume(request: Request, payload: ScanStartPayload) -> dict[str, Any]:
    services = _services(request)
    try:
        session = services.scan_session_service.start_or_resume(run_kind=payload.run_kind, triggered_by="manual_webui")
    except ScanActiveConflictError as exc:
        raise _error(409, "SCAN_ACTIVE_CONFLICT", str(exc)) from exc
    except ValueError as exc:
        raise _error(400, "VALIDATION_ERROR", str(exc)) from exc
    return _ok({"session_id": session.id, "status": session.status, "resumed": bool(session.resumed)})


@router.post("/scan/start_new")
def scan_start_new(request: Request, payload: ScanStartPayload) -> dict[str, Any]:
    services = _services(request)
    try:
        session = services.scan_session_service.start_new(run_kind=payload.run_kind, triggered_by="manual_webui")
    except ScanActiveConflictError as exc:
        raise _error(409, "SCAN_ACTIVE_CONFLICT", str(exc)) from exc
    except ValueError as exc:
        raise _error(400, "VALIDATION_ERROR", str(exc)) from exc
    return _ok({"session_id": session.id, "status": session.status})


@router.post("/scan/abort")
def scan_abort(request: Request, payload: ScanAbortPayload) -> dict[str, Any]:
    services = _services(request)
    try:
        session = services.scan_session_service.abort(payload.session_id)
    except ScanSessionNotFoundError as exc:
        raise _error(404, "SCAN_SESSION_NOT_FOUND", str(exc)) from exc
    except ValueError as exc:
        raise _error(400, "VALIDATION_ERROR", str(exc)) from exc
    return _ok({"session_id": session.id, "status": session.status})


@router.post("/people/{person_id}/actions/rename")
def people_rename(request: Request, person_id: int, payload: RenamePayload) -> dict[str, Any]:
    services = _services(request)
    try:
        assert_people_writes_allowed(services.library_db_path)
        person = services.people_service.rename_person(person_id=person_id, display_name=payload.display_name)
    except ExportRunLockError as exc:
        raise _error(409, "EXPORT_RUNNING_LOCK", str(exc)) from exc
    except ValueError as exc:
        raise _map_people_error(exc) from exc
    return _ok({"person_id": person.id, "display_name": person.display_name, "is_named": person.is_named})


@router.post("/people/{person_id}/actions/exclude-assignment")
def people_exclude_assignment(request: Request, person_id: int, payload: ExcludeAssignmentPayload) -> dict[str, Any]:
    services = _services(request)
    try:
        assert_people_writes_allowed(services.library_db_path)
        result = services.people_service.exclude_assignment(person_id=person_id, face_observation_id=payload.face_observation_id)
    except ExportRunLockError as exc:
        raise _error(409, "EXPORT_RUNNING_LOCK", str(exc)) from exc
    except ValueError as exc:
        if _has_active_exclusion(
            services.library_db_path,
            person_id=person_id,
            face_observation_id=payload.face_observation_id,
        ):
            raise _error(409, "ILLEGAL_STATE", "face observation 已处于排除状态") from exc
        raise _map_people_error(exc) from exc
    return _ok(
        {
            "person_id": result.person_id,
            "face_observation_id": result.face_observation_id,
            "pending_reassign": result.pending_reassign,
        }
    )


@router.post("/people/{person_id}/actions/exclude-assignments")
def people_exclude_assignments(request: Request, person_id: int, payload: ExcludeAssignmentsPayload) -> dict[str, Any]:
    services = _services(request)
    try:
        assert_people_writes_allowed(services.library_db_path)
        result = services.people_service.exclude_assignments(person_id=person_id, face_observation_ids=payload.face_observation_ids)
    except ExportRunLockError as exc:
        raise _error(409, "EXPORT_RUNNING_LOCK", str(exc)) from exc
    except ValueError as exc:
        for face_observation_id in payload.face_observation_ids:
            if _has_active_exclusion(
                services.library_db_path,
                person_id=person_id,
                face_observation_id=face_observation_id,
            ):
                raise _error(409, "ILLEGAL_STATE", "batch 中存在已排除样本") from exc
        raise _map_people_error(exc) from exc
    return _ok({"person_id": result.person_id, "excluded_count": result.excluded_count})


@router.post("/people/actions/merge-batch")
def people_merge_batch(request: Request, payload: MergeBatchPayload) -> dict[str, Any]:
    services = _services(request)
    try:
        assert_people_writes_allowed(services.library_db_path)
        result = services.people_service.merge_people(selected_person_ids=payload.selected_person_ids)
    except ValueError as exc:
        raise _map_people_error(exc) from exc
    except ExportRunLockError as exc:
        raise _error(409, "EXPORT_RUNNING_LOCK", str(exc)) from exc
    return _ok(
        {
            "merge_operation_id": result.merge_operation_id,
            "winner_person_id": result.winner_person_id,
            "winner_person_uuid": result.winner_person_uuid,
        }
    )


@router.post("/people/actions/undo-last-merge")
def people_undo_last_merge(request: Request) -> dict[str, Any]:
    services = _services(request)
    try:
        assert_people_writes_allowed(services.library_db_path)
        result = services.people_service.undo_last_merge()
    except ExportRunLockError as exc:
        raise _error(409, "EXPORT_RUNNING_LOCK", str(exc)) from exc
    except MergeOperationNotFoundError as exc:
        raise _error(404, "MERGE_OPERATION_NOT_FOUND", str(exc)) from exc
    return _ok({"merge_operation_id": result.merge_operation_id, "status": result.status})


@router.get("/export/templates")
def export_templates_list(request: Request, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    services = _services(request)
    templates = services.export_template_service.list_templates()
    sliced = templates[offset : offset + limit]
    return _ok(
        {
            "items": [
                {
                    "template_id": item.id,
                    "name": item.name,
                    "output_root": item.output_root,
                    "enabled": item.enabled,
                    "person_ids": item.person_ids,
                }
                for item in sliced
            ]
        }
    )


@router.post("/export/templates")
def export_template_create(request: Request, payload: ExportTemplateCreatePayload) -> dict[str, Any]:
    services = _services(request)
    existing = services.export_template_service.list_templates()
    if any(item.name == payload.name.strip() for item in existing):
        raise _error(409, "EXPORT_TEMPLATE_DUPLICATE", f"模板名称重复: {payload.name}")
    try:
        created = services.export_template_service.create_template(
            name=payload.name,
            output_root=Path(payload.output_root),
            person_ids=payload.person_ids,
        )
    except ExportValidationError as exc:
        raise _error(400, "VALIDATION_ERROR", str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        raise _error(409, "EXPORT_TEMPLATE_DUPLICATE", str(exc)) from exc
    return _ok({"template_id": created.id})


@router.put("/export/templates/{template_id}")
def export_template_update(request: Request, template_id: int, payload: ExportTemplateUpdatePayload) -> dict[str, Any]:
    services = _services(request)
    if not _template_exists(services.library_db_path, template_id):
        raise _error(404, "EXPORT_TEMPLATE_NOT_FOUND", f"模板不存在: template_id={template_id}")
    if payload.name is not None:
        normalized_name = payload.name.strip()
        if normalized_name and _template_name_exists_other(
            services.library_db_path,
            template_id=template_id,
            template_name=normalized_name,
        ):
            raise _error(409, "EXPORT_TEMPLATE_DUPLICATE", f"模板名称重复: {normalized_name}")
    try:
        services.export_template_service.update_template(
            template_id=template_id,
            name=payload.name,
            output_root=None if payload.output_root is None else Path(payload.output_root),
            person_ids=payload.person_ids,
            enabled=payload.enabled,
        )
    except ExportValidationError as exc:
        raise _error(400, "VALIDATION_ERROR", str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        raise _error(409, "EXPORT_TEMPLATE_DUPLICATE", str(exc)) from exc
    return _ok({"template_id": template_id, "updated": True})


@router.post("/export/templates/{template_id}/actions/run")
def export_template_run(request: Request, template_id: int) -> dict[str, Any]:
    services = _services(request)
    if not _template_exists(services.library_db_path, template_id):
        raise _error(404, "EXPORT_TEMPLATE_NOT_FOUND", f"模板不存在: template_id={template_id}")
    try:
        run = services.export_run_service.start_export_run(template_id=template_id)
    except ExportValidationError as exc:
        raise _error(404, "EXPORT_TEMPLATE_NOT_FOUND", str(exc)) from exc
    return _ok({"export_run_id": run.id, "status": run.status})


def _audit_item_to_dict(item: ScanAuditItem) -> dict[str, Any]:
    return {
        "audit_type": item.audit_type,
        "face_observation_id": item.face_observation_id,
        "person_id": item.person_id,
        "evidence_json": item.evidence_json,
    }


@router.get("/scan/{session_id}/audit-items")
def scan_audit_items(request: Request, session_id: int, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    services = _services(request)
    if not _scan_session_exists(services.library_db_path, session_id):
        raise _error(404, "SCAN_SESSION_NOT_FOUND", f"扫描会话不存在: session_id={session_id}")
    try:
        items = services.audit_service.list_audit_items(scan_session_id=session_id, limit=limit, offset=offset)
    except ValueError as exc:
        raise _error(400, "VALIDATION_ERROR", str(exc)) from exc
    return _ok({"items": [_audit_item_to_dict(item) for item in items]})
