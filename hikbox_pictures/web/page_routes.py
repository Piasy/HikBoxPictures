from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from hikbox_pictures.product.db.connection import connect_sqlite
from hikbox_pictures.product.scan.detect_stage import build_scan_runtime_defaults

router = APIRouter()


@dataclass(frozen=True)
class SourceProgressRow:
    source_id: int
    processed: int
    total: int


@dataclass(frozen=True)
class PreviewSummary:
    only_count: int
    group_count: int
    samples: list[dict[str, str]]


def _render(request: Request, template: str, context: dict[str, Any]) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(request, template, context)


def _services(request: Request):
    return request.app.state.services


def _load_people_index_data(db_path: Path) -> dict[str, Any]:
    with connect_sqlite(db_path) as conn:
        named = conn.execute(
            """
            SELECT id, person_uuid, display_name
            FROM person
            WHERE status='active' AND is_named=1
            ORDER BY id
            """
        ).fetchall()
        anonymous = conn.execute(
            """
            SELECT id, person_uuid
            FROM person
            WHERE status='active' AND is_named=0
            ORDER BY id
            """
        ).fetchall()
    return {
        "named_people": [
            {"person_id": int(row[0]), "person_uuid": str(row[1]), "display_name": str(row[2])}
            for row in named
        ],
        "anonymous_people": [
            {"person_id": int(row[0]), "person_uuid": str(row[1])}
            for row in anonymous
        ],
    }


def _load_person_detail_data(db_path: Path, person_id: int) -> dict[str, Any]:
    with connect_sqlite(db_path) as conn:
        person = conn.execute(
            """
            SELECT id, person_uuid, display_name, is_named, status
            FROM person
            WHERE id=?
            """,
            (int(person_id),),
        ).fetchone()
        assignments = conn.execute(
            """
            SELECT pfa.face_observation_id
            FROM person_face_assignment pfa
            WHERE pfa.person_id=? AND pfa.active=1
            ORDER BY pfa.face_observation_id
            """,
            (int(person_id),),
        ).fetchall()
    return {
        "person": {
            "person_id": int(person[0]) if person is not None else int(person_id),
            "person_uuid": str(person[1]) if person is not None else "",
            "display_name": str(person[2]) if person is not None and person[2] is not None else "",
            "is_named": bool(int(person[3])) if person is not None else False,
            "status": str(person[4]) if person is not None else "missing",
        },
        "assignment_face_ids": [int(row[0]) for row in assignments],
    }


def _load_sources_data(db_path: Path) -> dict[str, Any]:
    with connect_sqlite(db_path) as conn:
        sources = conn.execute(
            """
            SELECT id, root_path, label, enabled, status
            FROM library_source
            ORDER BY id
            """
        ).fetchall()
        sessions = conn.execute(
            """
            SELECT id, run_kind, status, created_at
            FROM scan_session
            ORDER BY id DESC
            LIMIT 20
            """
        ).fetchall()
    return {
        "sources": [
            {
                "source_id": int(row[0]),
                "root_path": str(row[1]),
                "label": str(row[2]),
                "enabled": bool(int(row[3])),
                "status": str(row[4]),
            }
            for row in sources
        ],
        "sessions": [
            {
                "session_id": int(row[0]),
                "run_kind": str(row[1]),
                "status": str(row[2]),
                "created_at": str(row[3]),
            }
            for row in sessions
        ],
    }


def _load_scan_session_state(db_path: Path, session_id: int) -> dict[str, Any]:
    with connect_sqlite(db_path) as conn:
        row = conn.execute(
            "SELECT id, status FROM scan_session WHERE id=?",
            (int(session_id),),
        ).fetchone()
        if row is None:
            return {"session_id": int(session_id), "status": "missing", "failed_count": 0}
        failed_row = conn.execute(
            """
            SELECT COUNT(*)
            FROM scan_batch_item sbi
            JOIN scan_batch sb ON sb.id = sbi.scan_batch_id
            WHERE sb.scan_session_id=? AND sbi.status='failed'
            """,
            (int(session_id),),
        ).fetchone()
    return {
        "session_id": int(row[0]),
        "status": str(row[1]),
        "failed_count": int(failed_row[0]) if failed_row is not None else 0,
    }


def _load_source_progress_rows(db_path: Path, session_id: int) -> list[SourceProgressRow]:
    with connect_sqlite(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
              ls.id,
              SUM(CASE WHEN sbi.status IN ('done', 'failed') THEN 1 ELSE 0 END) AS processed,
              COUNT(*) AS total
            FROM scan_batch_item sbi
            JOIN scan_batch sb ON sb.id = sbi.scan_batch_id
            JOIN photo_asset pa ON pa.id = sbi.photo_asset_id
            JOIN library_source ls ON ls.id = pa.library_source_id
            WHERE sb.scan_session_id=?
            GROUP BY ls.id
            ORDER BY ls.id
            """,
            (int(session_id),),
        ).fetchall()
    return [
        SourceProgressRow(source_id=int(row[0]), processed=int(row[1]), total=int(row[2]))
        for row in rows
    ]


def _load_templates(db_path: Path) -> list[dict[str, Any]]:
    with connect_sqlite(db_path) as conn:
        try:
            template_rows = conn.execute(
                """
                SELECT id, name, output_root, enabled
                FROM export_template
                ORDER BY id
                """
            ).fetchall()
            person_rows = conn.execute(
                """
                SELECT template_id, person_id
                FROM export_template_person
                ORDER BY template_id, person_id
                """
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table: export_template" in str(exc) or "no such table: export_template_person" in str(exc):
                return []
            raise

    person_map: dict[int, list[int]] = {}
    for row in person_rows:
        person_map.setdefault(int(row[0]), []).append(int(row[1]))

    return [
        {
            "template_id": int(row[0]),
            "name": str(row[1]),
            "output_root": str(row[2]),
            "enabled": bool(int(row[3])),
            "person_ids": person_map.get(int(row[0]), []),
        }
        for row in template_rows
    ]


def _load_export_history(db_path: Path) -> list[dict[str, Any]]:
    with connect_sqlite(db_path) as conn:
        try:
            rows = conn.execute(
                """
                SELECT id, template_id, status, summary_json, started_at, finished_at
                FROM export_run
                ORDER BY id DESC
                LIMIT 20
                """
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table: export_run" in str(exc):
                return []
            raise
    history: list[dict[str, Any]] = []
    for row in rows:
        summary_raw = str(row[3]) if row[3] is not None else "{}"
        try:
            summary = json.loads(summary_raw)
        except json.JSONDecodeError:
            summary = {}
        history.append(
            {
                "export_run_id": int(row[0]),
                "template_id": int(row[1]),
                "status": str(row[2]),
                "summary": summary,
                "started_at": str(row[4]),
                "finished_at": None if row[5] is None else str(row[5]),
            }
        )
    return history


def _is_people_locked(db_path: Path) -> bool:
    with connect_sqlite(db_path) as conn:
        try:
            row = conn.execute(
                "SELECT 1 FROM export_run WHERE status='running' LIMIT 1"
            ).fetchone()
        except sqlite3.OperationalError:
            return False
    return row is not None


def _load_preview_summary(db_path: Path) -> PreviewSummary:
    with connect_sqlite(db_path) as conn:
        try:
            rows = conn.execute(
                """
                SELECT
                  pa.id,
                  pa.primary_path,
                  pfa.person_id
                FROM photo_asset pa
                LEFT JOIN face_observation fo ON fo.photo_asset_id = pa.id AND fo.active=1
                LEFT JOIN person_face_assignment pfa ON pfa.face_observation_id = fo.id AND pfa.active=1
                WHERE pa.asset_status='active'
                ORDER BY pa.id
                """
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table: photo_asset" in str(exc) or "no such table: face_observation" in str(exc):
                return PreviewSummary(only_count=0, group_count=0, samples=[])
            raise

    by_photo: dict[int, dict[str, Any]] = {}
    for row in rows:
        photo_id = int(row[0])
        entry = by_photo.setdefault(photo_id, {"path": str(row[1]), "persons": set()})
        if row[2] is not None:
            entry["persons"].add(int(row[2]))

    only_count = 0
    group_count = 0
    samples: list[dict[str, str]] = []
    for photo_id in sorted(by_photo.keys()):
        persons: set[int] = by_photo[photo_id]["persons"]
        if len(persons) == 1:
            only_count += 1
            bucket = "only"
        elif len(persons) >= 2:
            group_count += 1
            bucket = "group"
        else:
            continue
        if len(samples) < 6:
            samples.append({"photo_asset_id": str(photo_id), "bucket": bucket, "primary_path": by_photo[photo_id]["path"]})

    return PreviewSummary(only_count=only_count, group_count=group_count, samples=samples)


@router.get("/", response_class=HTMLResponse)
def people_index(request: Request) -> HTMLResponse:
    services = _services(request)
    context = _load_people_index_data(services.library_db_path)
    return _render(request, "people_index.html", context)


@router.get("/people/{person_id}", response_class=HTMLResponse)
def people_detail(request: Request, person_id: int) -> HTMLResponse:
    services = _services(request)
    context = _load_person_detail_data(services.library_db_path, person_id)
    return _render(request, "people_detail.html", context)


@router.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request) -> HTMLResponse:
    services = _services(request)
    context = _load_sources_data(services.library_db_path)
    return _render(request, "sources.html", context)


@router.get("/sources/{session_id}/audit", response_class=HTMLResponse)
def sources_audit_page(request: Request, session_id: int) -> HTMLResponse:
    services = _services(request)
    session = _load_scan_session_state(services.library_db_path, session_id)
    progress_rows = _load_source_progress_rows(services.library_db_path, session_id)
    defaults = build_scan_runtime_defaults(cpu_count=8)

    status = session["status"]
    resume_enabled = "true" if status == "interrupted" else "false"
    abort_enabled = "true" if status == "running" else "false"
    abandon_new_enabled = "true" if status in {"running", "interrupted", "aborting"} else "false"

    return _render(
        request,
        "audit.html",
        {
            "session": session,
            "progress_rows": progress_rows,
            "scan_params": {
                "det_size": defaults.det_size,
                "workers": defaults.workers,
                "batch_size": defaults.batch_size,
            },
            "action_states": {
                "resume_enabled": resume_enabled,
                "abort_enabled": abort_enabled,
                "abandon_new_enabled": abandon_new_enabled,
            },
        },
    )


@router.get("/exports", response_class=HTMLResponse)
def exports_page(request: Request) -> HTMLResponse:
    services = _services(request)
    db_path = services.library_db_path
    templates = _load_templates(db_path)
    history = _load_export_history(db_path)
    preview = _load_preview_summary(db_path)
    locked = _is_people_locked(db_path)

    return _render(
        request,
        "exports.html",
        {
            "templates": templates,
            "history": history,
            "preview": preview,
            "people_locked": locked,
        },
    )


@router.get("/exports/{template_id}", response_class=HTMLResponse)
def export_detail_page(request: Request, template_id: int) -> HTMLResponse:
    services = _services(request)
    templates = _load_templates(services.library_db_path)
    detail = next((item for item in templates if item["template_id"] == int(template_id)), None)
    return _render(request, "exports.html", {"templates": templates, "history": _load_export_history(services.library_db_path), "preview": _load_preview_summary(services.library_db_path), "people_locked": _is_people_locked(services.library_db_path), "detail": detail})


@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request) -> HTMLResponse:
    services = _services(request)
    events = services.ops_event_service.query_events(limit=50, offset=0)
    return _render(request, "logs.html", {"events": events})
