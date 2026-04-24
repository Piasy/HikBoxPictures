"""HikBox CLI 入口。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from hikbox_pictures.product.config import WorkspaceLayout, initialize_workspace
from hikbox_pictures.product.export import (
    ExportRunNotFoundError,
    ExportTemplateDuplicateError,
    ExportTemplateNotFoundError,
    ExportValidationError,
)
from hikbox_pictures.product.export.run_service import ExportRunningLockError
from hikbox_pictures.product.people.service import (
    PeopleExcludeConflictError,
    PeopleMergeError,
    PeopleNotFoundError,
    PeopleUndoMergeConflictError,
    PeopleUndoMergeError,
)
from hikbox_pictures.product.scan.errors import (
    InvalidRunKindError,
    InvalidTriggeredByError,
    ScanActiveConflictError,
    ServeBlockedByActiveScanError,
    SessionNotFoundError,
)
from hikbox_pictures.product.scan.session_service import (
    ScanSessionRepository,
    assert_no_active_scan_for_serve,
)
from hikbox_pictures.product.service_registry import build_service_container
from hikbox_pictures.product.source.service import SourceNotFoundError, SourceRootPathConflictError
EXIT_CODE_SUCCESS = 0
EXIT_CODE_UNCLASSIFIED = 1
EXIT_CODE_VALIDATION = 2
EXIT_CODE_NOT_FOUND = 3
EXIT_CODE_SCAN_ACTIVE_CONFLICT = 4
EXIT_CODE_EXPORT_RUNNING_LOCK = 5
EXIT_CODE_ILLEGAL_STATE = 6
EXIT_CODE_SERVE_BLOCKED = 7


def _workspace_root(raw: str) -> Path:
    return Path(raw).expanduser().resolve()


def _workspace_layout(workspace: Path) -> WorkspaceLayout:
    hikbox_root = workspace / ".hikbox"
    return WorkspaceLayout(
        workspace_root=workspace,
        hikbox_root=hikbox_root,
        library_db=hikbox_root / "library.db",
        embedding_db=hikbox_root / "embedding.db",
        config_json=hikbox_root / "config.json",
    )


def _load_config(layout: WorkspaceLayout) -> dict[str, Any]:
    try:
        return json.loads(layout.config_json.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"workspace 未初始化或文件缺失: {layout.config_json}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"config.json 非法: {exc}") from exc


def _require_workspace_initialized(workspace: Path) -> WorkspaceLayout:
    layout = _workspace_layout(workspace)
    missing = [path for path in (layout.library_db, layout.embedding_db, layout.config_json) if not path.exists()]
    if missing:
        missing_text = ", ".join(str(path) for path in missing)
        raise ValueError(f"workspace 未初始化或文件缺失: {missing_text}")
    return layout


def _print_success(args: argparse.Namespace, data: dict[str, Any]) -> None:
    if getattr(args, "quiet", False):
        return
    if getattr(args, "json", False):
        print(json.dumps({"ok": True, "data": data}, ensure_ascii=False))
        return
    for key, value in data.items():
        if isinstance(value, (dict, list)):
            value_text = json.dumps(value, ensure_ascii=False)
        else:
            value_text = str(value)
        print(f"{key}: {value_text}")


def _print_scan_runtime_stats(args: argparse.Namespace, run_result: Any | None) -> None:
    if run_result is None or getattr(args, "quiet", False) or getattr(args, "json", False):
        return
    fields = [
        ("new_face_count", getattr(run_result, "new_face_count", None)),
        ("anchor_candidate_face_count", getattr(run_result, "anchor_candidate_face_count", None)),
        ("anchor_attached_face_count", getattr(run_result, "anchor_attached_face_count", None)),
        ("anchor_missed_face_count", getattr(run_result, "anchor_missed_face_count", None)),
        ("anchor_missed_by_person", getattr(run_result, "anchor_missed_by_person", None)),
        ("local_rebuild_count", getattr(run_result, "local_rebuild_count", None)),
        ("fallback_reason", getattr(run_result, "fallback_reason", None)),
    ]
    if not any(value is not None for _, value in fields):
        return
    for key, value in fields:
        if value is None:
            continue
        if isinstance(value, (dict, list)):
            value_text = json.dumps(value, ensure_ascii=False)
        else:
            value_text = str(value)
        print(f"{key}: {value_text}")


def _print_error(args: argparse.Namespace | None, *, code: str, message: str, extra: dict[str, Any] | None = None) -> None:
    payload = {"ok": False, "error": {"code": code, "message": message}}
    if extra:
        payload["error"].update(extra)
    if args is not None and getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        return
    print(f"{code}: {message}", file=sys.stderr)


def _cmd_init(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    external_root = _workspace_root(args.external_root) if args.external_root else workspace / "external"
    layout = initialize_workspace(workspace_root=workspace, external_root=external_root)
    _print_success(
        args,
        {
            "workspace_root": str(layout.workspace_root),
            "hikbox_root": str(layout.hikbox_root),
            "library_db": str(layout.library_db),
            "embedding_db": str(layout.embedding_db),
            "config_json": str(layout.config_json),
            "external_root": str(external_root),
        },
    )
    return EXIT_CODE_SUCCESS


def _cmd_config_show(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    _print_success(args, _load_config(layout))
    return EXIT_CODE_SUCCESS


def _cmd_config_set_external_root(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    external_root = _workspace_root(args.external_root)
    config = _load_config(layout)
    config["external_root"] = str(external_root)
    layout.config_json.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _print_success(args, config)
    return EXIT_CODE_SUCCESS


def _cmd_scan_start_or_resume(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    services = build_service_container(layout)
    result = services.scan_sessions.start_or_resume(
        run_kind=args.run_kind,
        triggered_by="manual_cli",
    )
    session, run_result = _run_scan_session_until_terminal(
        services,
        session_id=result.session_id,
        should_execute=result.should_execute,
    )
    _print_success(
        args,
        {
            "session_id": result.session_id,
            "resumed": result.resumed,
            "status": session.status,
        },
    )
    _print_scan_runtime_stats(args, run_result)
    return EXIT_CODE_SUCCESS


def _cmd_scan_start_new(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    services = build_service_container(layout)
    result = services.scan_sessions.start_new(
        run_kind=args.run_kind,
        triggered_by="manual_cli",
    )
    session, run_result = _run_scan_session_until_terminal(
        services,
        session_id=result.session_id,
        should_execute=result.should_execute,
    )
    _print_success(
        args,
        {
            "session_id": result.session_id,
            "resumed": result.resumed,
            "status": session.status,
        },
    )
    _print_scan_runtime_stats(args, run_result)
    return EXIT_CODE_SUCCESS


def _cmd_scan_abort(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    services = build_service_container(layout)
    session = services.scan_sessions.abort(args.session_id)
    _print_success(
        args,
        {
            "session_id": session.id,
            "status": session.status,
        },
    )
    return EXIT_CODE_SUCCESS


def _cmd_scan_status(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    session = _get_scan_session_status(
        layout.library_db,
        latest=bool(args.latest),
        session_id=args.session_id,
    )
    _print_success(args, session)
    return EXIT_CODE_SUCCESS


def _cmd_scan_list(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    if args.limit <= 0:
        raise ValueError("limit 必须是正整数")
    items = _list_scan_sessions(layout.library_db, limit=args.limit)
    _print_success(args, {"items": items})
    return EXIT_CODE_SUCCESS


def _cmd_serve_start(args: argparse.Namespace) -> int:
    from hikbox_pictures.web.app import create_app
    import uvicorn

    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    repo = ScanSessionRepository(layout.library_db)
    assert_no_active_scan_for_serve(repo)

    app = create_app(build_service_container(layout))
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        access_log=False,
        log_level="warning",
    )
    return EXIT_CODE_SUCCESS


def _cmd_people_list(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    services = build_service_container(layout)
    if args.named:
        rows = services.read_model.list_named_people()
    elif args.anonymous:
        rows = services.read_model.list_anonymous_people()
    else:
        rows = _list_all_people(layout.library_db)
    items = [_serialize_list_person_item(row) for row in rows]
    _print_success(args, {"total": len(items), "items": items})
    return EXIT_CODE_SUCCESS


def _cmd_people_show(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    services = build_service_container(layout)
    detail = services.read_model.get_person_detail(args.person_id)
    if detail["person"] is None:
        raise PeopleNotFoundError(f"人物不存在，id={args.person_id}")
    _print_success(args, detail)
    return EXIT_CODE_SUCCESS


def _cmd_people_rename(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    services = build_service_container(layout)
    person = services.people.rename_person(args.person_id, args.display_name)
    _print_success(
        args,
        {
            "person_id": person.id,
            "display_name": person.display_name,
            "is_named": bool(person.is_named),
        },
    )
    return EXIT_CODE_SUCCESS


def _cmd_people_exclude(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    services = build_service_container(layout)
    result = services.people.exclude_face(args.person_id, args.face_observation_id)
    pending_reassign = _query_face_pending_reassign(layout.library_db, args.face_observation_id)
    _print_success(
        args,
        {
            "person_id": result.person_id,
            "face_observation_id": result.face_observation_ids[0],
            "pending_reassign": pending_reassign,
        },
    )
    return EXIT_CODE_SUCCESS


def _cmd_people_exclude_batch(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    services = build_service_container(layout)
    face_observation_ids = _parse_csv_ints(args.face_observation_ids, field_name="face_observation_ids")
    result = services.people.exclude_faces(args.person_id, face_observation_ids)
    _print_success(
        args,
        {
            "person_id": result.person_id,
            "excluded_count": len(result.face_observation_ids),
        },
    )
    return EXIT_CODE_SUCCESS


def _cmd_people_merge(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    services = build_service_container(layout)
    selected_person_ids = _parse_csv_ints(args.selected_person_ids, field_name="selected_person_ids")
    result = services.people.merge_people(selected_person_ids)
    detail = services.read_model.get_person_detail(result.winner_person_id)
    person = detail["person"]
    winner_person_uuid = "" if person is None else str(person["person_uuid"])
    _print_success(
        args,
        {
            "merge_operation_id": result.merge_operation_id,
            "winner_person_id": result.winner_person_id,
            "winner_person_uuid": winner_person_uuid,
        },
    )
    return EXIT_CODE_SUCCESS


def _cmd_people_undo_last_merge(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    services = build_service_container(layout)
    result = services.people.undo_last_merge()
    _print_success(args, {"merge_operation_id": result.merge_operation_id, "status": "undone"})
    return EXIT_CODE_SUCCESS


def _cmd_audit_list(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    services = build_service_container(layout)
    items = services.read_model.list_audit_items(scan_session_id=args.scan_session_id)
    _print_success(args, {"items": items})
    return EXIT_CODE_SUCCESS


def _cmd_source_list(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    services = build_service_container(layout)
    items = [
        {
            "source_id": source.id,
            "root_path": source.root_path,
            "label": source.label,
            "enabled": bool(source.enabled),
            "removed_at": source.removed_at,
            "created_at": source.created_at,
            "updated_at": source.updated_at,
        }
        for source in services.sources.list_sources()
    ]
    _print_success(args, {"total": len(items), "items": items})
    return EXIT_CODE_SUCCESS


def _cmd_source_add(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    services = build_service_container(layout)
    source = services.sources.add_source(args.root_path, label=args.label)
    _print_success(
        args,
        {
            "source_id": source.id,
            "root_path": source.root_path,
            "label": source.label,
            "enabled": bool(source.enabled),
        },
    )
    return EXIT_CODE_SUCCESS


def _cmd_source_disable(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    services = build_service_container(layout)
    source = services.sources.disable_source(args.source_id)
    _print_success(
        args,
        {
            "source_id": source.id,
            "root_path": source.root_path,
            "label": source.label,
            "enabled": bool(source.enabled),
        },
    )
    return EXIT_CODE_SUCCESS


def _cmd_source_enable(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    services = build_service_container(layout)
    source = services.sources.enable_source(args.source_id)
    _print_success(
        args,
        {
            "source_id": source.id,
            "root_path": source.root_path,
            "label": source.label,
            "enabled": bool(source.enabled),
        },
    )
    return EXIT_CODE_SUCCESS


def _cmd_source_relabel(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    services = build_service_container(layout)
    source = services.sources.relabel_source(args.source_id, args.label)
    _print_success(
        args,
        {
            "source_id": source.id,
            "root_path": source.root_path,
            "label": source.label,
            "enabled": bool(source.enabled),
        },
    )
    return EXIT_CODE_SUCCESS


def _cmd_source_remove(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    services = build_service_container(layout)
    source = services.sources.remove_source(args.source_id)
    _print_success(
        args,
        {
            "source_id": source.id,
            "root_path": source.root_path,
            "label": source.label,
            "enabled": bool(source.enabled),
        },
    )
    return EXIT_CODE_SUCCESS


def _cmd_export_template_list(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    services = build_service_container(layout)
    items = [
        {
            "template_id": template.id,
            "name": template.name,
            "output_root": template.output_root,
            "enabled": bool(template.enabled),
            "person_ids": list(template.person_ids),
        }
        for template in services.export_templates.list_templates()
    ]
    _print_success(args, {"items": items})
    return EXIT_CODE_SUCCESS


def _cmd_export_template_create(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    services = build_service_container(layout)
    template = services.export_templates.create_template(
        name=args.name,
        output_root=args.output_root,
        person_ids=_parse_csv_ints(args.person_ids, field_name="person_ids"),
        enabled=True if args.enabled is None else _parse_bool(args.enabled, field_name="enabled"),
    )
    _print_success(args, {"template_id": template.id})
    return EXIT_CODE_SUCCESS


def _cmd_export_template_update(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    services = build_service_container(layout)
    person_ids: list[int] | object
    if args.person_ids is None:
        person_ids = _EXPORT_UNCHANGED
    else:
        person_ids = _parse_csv_ints(args.person_ids, field_name="person_ids")
    enabled: bool | object
    if args.enabled is None:
        enabled = _EXPORT_UNCHANGED
    else:
        enabled = _parse_bool(args.enabled, field_name="enabled")
    services.export_templates.update_template(
        args.template_id,
        name=args.name if args.name is not None else _EXPORT_UNCHANGED,
        output_root=args.output_root if args.output_root is not None else _EXPORT_UNCHANGED,
        enabled=enabled,
        person_ids=person_ids,
    )
    _print_success(args, {"template_id": args.template_id, "updated": True})
    return EXIT_CODE_SUCCESS


def _cmd_export_run(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    services = build_service_container(layout)
    result = services.export_runs.start_run(args.template_id)
    _print_success(args, {"export_run_id": result.export_run_id, "status": result.status})
    return EXIT_CODE_SUCCESS


def _cmd_export_run_status(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    run = _get_export_run(layout.library_db, export_run_id=args.export_run_id)
    _print_success(args, run)
    return EXIT_CODE_SUCCESS


def _cmd_export_execute(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    services = build_service_container(layout)
    result = services.export_runs.execute_run(args.export_run_id)
    _print_success(
        args,
        {
            "export_run_id": result.export_run_id,
            "status": result.status,
            "exported_count": result.exported_count,
            "skipped_exists_count": result.skipped_exists_count,
            "failed_count": result.failed_count,
        },
    )
    return EXIT_CODE_SUCCESS


def _cmd_export_run_list(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    if args.limit <= 0:
        raise ValueError("limit 必须是正整数")
    items = _list_export_runs(layout.library_db, template_id=args.template_id, limit=args.limit)
    _print_success(args, {"items": items})
    return EXIT_CODE_SUCCESS


def _cmd_logs_list(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    services = build_service_container(layout)
    page = services.ops_events.query_events(
        scan_session_id=args.scan_session_id,
        export_run_id=args.export_run_id,
        severity=args.severity,
        limit=args.limit,
    )
    items = [
        {
            "id": item.id,
            "event_type": item.event_type,
            "severity": item.severity,
            "scan_session_id": item.scan_session_id,
            "export_run_id": item.export_run_id,
            "payload": item.payload,
            "created_at": item.created_at,
        }
        for item in page.items
    ]
    _print_success(
        args,
        {
            "items": items,
            "limit": page.limit,
            "before_id": page.before_id,
            "next_before_id": page.next_before_id,
        },
    )
    return EXIT_CODE_SUCCESS


def _cmd_db_vacuum(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    targets: list[tuple[str, Path]] = []
    if args.library or (not args.library and not args.embedding):
        targets.append(("library", layout.library_db))
    if args.embedding or (not args.library and not args.embedding):
        targets.append(("embedding", layout.embedding_db))
    for _, db_path in targets:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()
    _print_success(args, {"targets": [name for name, _ in targets]})
    return EXIT_CODE_SUCCESS


def _list_all_people(library_db: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(library_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, person_uuid, display_name, is_named, status
            FROM person
            WHERE status='active'
            ORDER BY id ASC
            """
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _serialize_list_person_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "person_id": int(row["id"]),
        "person_uuid": str(row["person_uuid"]),
        "display_name": str(row["display_name"]),
        "is_named": bool(row["is_named"]),
        "status": str(row["status"]),
    }


def _query_face_pending_reassign(library_db: Path, face_observation_id: int) -> int:
    conn = sqlite3.connect(library_db)
    try:
        row = conn.execute(
            "SELECT pending_reassign FROM face_observation WHERE id=?",
            (int(face_observation_id),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise PeopleNotFoundError(f"face_observation 不存在，id={face_observation_id}")
    return int(row[0])


def _parse_csv_ints(raw_value: str, *, field_name: str) -> list[int]:
    items = [item.strip() for item in str(raw_value).split(",")]
    values = [int(item) for item in items if item]
    if not values:
        raise ValueError(f"{field_name} 不能为空")
    return values


def _parse_bool(raw_value: str, *, field_name: str) -> bool:
    normalized = str(raw_value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{field_name} 必须是 true/false")


_EXPORT_UNCHANGED = object()


def _serialize_scan_session_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "session_id": int(row["id"]),
        "run_kind": str(row["run_kind"]),
        "status": str(row["status"]),
        "triggered_by": str(row["triggered_by"]),
        "resume_from_session_id": None if row["resume_from_session_id"] is None else int(row["resume_from_session_id"]),
        "started_at": None if row["started_at"] is None else str(row["started_at"]),
        "finished_at": None if row["finished_at"] is None else str(row["finished_at"]),
        "last_error": None if row["last_error"] is None else str(row["last_error"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _execute_scan_session(services: Any, *, session_id: int):
    return services.scan_execution.run_session(scan_session_id=session_id)


def _run_scan_session_until_terminal(services: Any, *, session_id: int, should_execute: bool):
    session = services.scan_session_repo.get_session(session_id)
    if not should_execute:
        return session, None
    if session.status == "pending":
        session = services.scan_session_repo.update_status(session_id, status="running")
    run_result = _execute_scan_session(services, session_id=session_id)
    return services.scan_session_repo.get_session(session_id), run_result


def _get_scan_session_status(library_db: Path, *, latest: bool, session_id: int | None) -> dict[str, Any]:
    conn = sqlite3.connect(library_db)
    conn.row_factory = sqlite3.Row
    try:
        if latest:
            row = conn.execute(
                """
                SELECT id, run_kind, status, triggered_by, resume_from_session_id, started_at, finished_at, last_error, created_at, updated_at
                FROM scan_session
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT id, run_kind, status, triggered_by, resume_from_session_id, started_at, finished_at, last_error, created_at, updated_at
                FROM scan_session
                WHERE id=?
                """,
                (int(session_id or 0),),
            ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise SessionNotFoundError(0 if latest else int(session_id or 0))
    return _serialize_scan_session_row(row)


def _list_scan_sessions(library_db: Path, *, limit: int) -> list[dict[str, Any]]:
    conn = sqlite3.connect(library_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, run_kind, status, triggered_by, resume_from_session_id, started_at, finished_at, last_error, created_at, updated_at
            FROM scan_session
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    finally:
        conn.close()
    return [_serialize_scan_session_row(row) for row in rows]


def _serialize_export_run_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "export_run_id": int(row["id"]),
        "template_id": int(row["template_id"]),
        "status": str(row["status"]),
        "summary": json.loads(str(row["summary_json"])),
        "started_at": str(row["started_at"]),
        "finished_at": None if row["finished_at"] is None else str(row["finished_at"]),
    }


def _get_export_run(library_db: Path, *, export_run_id: int) -> dict[str, Any]:
    conn = sqlite3.connect(library_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT id, template_id, status, summary_json, started_at, finished_at
            FROM export_run
            WHERE id=?
            """,
            (int(export_run_id),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ExportRunNotFoundError(f"导出运行不存在: {export_run_id}")
    return _serialize_export_run_row(row)


def _list_export_runs(library_db: Path, *, template_id: int | None, limit: int) -> list[dict[str, Any]]:
    conn = sqlite3.connect(library_db)
    conn.row_factory = sqlite3.Row
    try:
        if template_id is None:
            rows = conn.execute(
                """
                SELECT id, template_id, status, summary_json, started_at, finished_at
                FROM export_run
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, template_id, status, summary_json, started_at, finished_at
                FROM export_run
                WHERE template_id=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(template_id), int(limit)),
            ).fetchall()
    finally:
        conn.close()
    return [_serialize_export_run_row(row) for row in rows]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HikBox Pictures CLI")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    parser.add_argument("--quiet", action="store_true", help="成功时不输出内容")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="初始化 workspace")
    p_init.add_argument("--workspace", required=True, help="workspace 根目录")
    p_init.add_argument("--external-root", default="", help="外部目录根路径（默认: <workspace>/external）")
    p_init.set_defaults(func=_cmd_init)

    p_config = sub.add_parser("config", help="配置命令")
    config_sub = p_config.add_subparsers(dest="config_command", required=True)

    p_config_show = config_sub.add_parser("show", help="查看当前配置")
    p_config_show.add_argument("--workspace", required=True, help="workspace 根目录")
    p_config_show.set_defaults(func=_cmd_config_show)

    p_config_set_external_root = config_sub.add_parser("set-external-root", help="设置 external_root")
    p_config_set_external_root.add_argument("external_root", help="外部目录根路径")
    p_config_set_external_root.add_argument("--workspace", required=True, help="workspace 根目录")
    p_config_set_external_root.set_defaults(func=_cmd_config_set_external_root)

    p_scan = sub.add_parser("scan", help="扫描会话管理")
    scan_sub = p_scan.add_subparsers(dest="scan_command", required=True)

    p_scan_start = scan_sub.add_parser("start-or-resume", help="启动或恢复扫描会话")
    p_scan_start.add_argument("--workspace", required=True, help="workspace 根目录")
    p_scan_start.add_argument(
        "--run-kind",
        default="scan_full",
        choices=["scan_full", "scan_incremental", "scan_resume"],
        help="扫描 run_kind",
    )
    p_scan_start.set_defaults(func=_cmd_scan_start_or_resume)

    p_scan_start_new = scan_sub.add_parser("start-new", help="放弃最近 interrupted 并创建新会话")
    p_scan_start_new.add_argument("--workspace", required=True, help="workspace 根目录")
    p_scan_start_new.add_argument(
        "--run-kind",
        default="scan_full",
        choices=["scan_full", "scan_incremental", "scan_resume"],
        help="扫描 run_kind",
    )
    p_scan_start_new.set_defaults(func=_cmd_scan_start_new)

    p_scan_abort = scan_sub.add_parser("abort", help="将活动会话标记为 aborting")
    p_scan_abort.add_argument("session_id", type=int, help="会话 id")
    p_scan_abort.add_argument("--workspace", required=True, help="workspace 根目录")
    p_scan_abort.set_defaults(func=_cmd_scan_abort)

    p_scan_status = scan_sub.add_parser("status", help="查看扫描会话状态")
    status_group = p_scan_status.add_mutually_exclusive_group(required=True)
    status_group.add_argument("--latest", action="store_true", help="查看最近一条会话")
    status_group.add_argument("--session-id", type=int, help="查看指定会话")
    p_scan_status.add_argument("--workspace", required=True, help="workspace 根目录")
    p_scan_status.set_defaults(func=_cmd_scan_status)

    p_scan_list = scan_sub.add_parser("list", help="列出扫描会话")
    p_scan_list.add_argument("--limit", type=int, default=20, help="返回条数")
    p_scan_list.add_argument("--workspace", required=True, help="workspace 根目录")
    p_scan_list.set_defaults(func=_cmd_scan_list)

    p_serve = sub.add_parser("serve", help="启动 Web 服务")
    serve_sub = p_serve.add_subparsers(dest="serve_command", required=True)
    p_serve_start = serve_sub.add_parser("start", help="启动 HTTP 服务")
    p_serve_start.add_argument("--workspace", required=True, help="workspace 根目录")
    p_serve_start.add_argument("--host", default="127.0.0.1", help="监听地址")
    p_serve_start.add_argument("--port", type=int, default=8000, help="监听端口")
    p_serve_start.set_defaults(func=_cmd_serve_start)

    p_people = sub.add_parser("people", help="人物维护命令")
    people_sub = p_people.add_subparsers(dest="people_command", required=True)

    p_people_list = people_sub.add_parser("list", help="列出人物")
    p_people_list.add_argument("--workspace", required=True, help="workspace 根目录")
    list_group = p_people_list.add_mutually_exclusive_group()
    list_group.add_argument("--named", action="store_true", help="仅列出已命名人物")
    list_group.add_argument("--anonymous", action="store_true", help="仅列出匿名人物")
    p_people_list.set_defaults(func=_cmd_people_list)

    p_people_show = people_sub.add_parser("show", help="查看人物详情")
    p_people_show.add_argument("person_id", type=int, help="人物 id")
    p_people_show.add_argument("--workspace", required=True, help="workspace 根目录")
    p_people_show.set_defaults(func=_cmd_people_show)

    p_people_rename = people_sub.add_parser("rename", help="重命名人物")
    p_people_rename.add_argument("person_id", type=int, help="人物 id")
    p_people_rename.add_argument("display_name", help="新的展示名称")
    p_people_rename.add_argument("--workspace", required=True, help="workspace 根目录")
    p_people_rename.set_defaults(func=_cmd_people_rename)

    p_people_exclude = people_sub.add_parser("exclude", help="排除单个人脸样本")
    p_people_exclude.add_argument("person_id", type=int, help="人物 id")
    p_people_exclude.add_argument("--face-observation-id", required=True, type=int, help="人脸 observation id")
    p_people_exclude.add_argument("--workspace", required=True, help="workspace 根目录")
    p_people_exclude.set_defaults(func=_cmd_people_exclude)

    p_people_exclude_batch = people_sub.add_parser("exclude-batch", help="批量排除人脸样本")
    p_people_exclude_batch.add_argument("person_id", type=int, help="人物 id")
    p_people_exclude_batch.add_argument(
        "--face-observation-ids",
        required=True,
        help="逗号分隔的人脸 observation id 列表",
    )
    p_people_exclude_batch.add_argument("--workspace", required=True, help="workspace 根目录")
    p_people_exclude_batch.set_defaults(func=_cmd_people_exclude_batch)

    p_people_merge = people_sub.add_parser("merge", help="批量合并人物")
    p_people_merge.add_argument(
        "--selected-person-ids",
        required=True,
        help="逗号分隔的人物 id 列表",
    )
    p_people_merge.add_argument("--workspace", required=True, help="workspace 根目录")
    p_people_merge.set_defaults(func=_cmd_people_merge)

    p_people_undo = people_sub.add_parser("undo-last-merge", help="撤销最近一次合并")
    p_people_undo.add_argument("--workspace", required=True, help="workspace 根目录")
    p_people_undo.set_defaults(func=_cmd_people_undo_last_merge)

    p_audit = sub.add_parser("audit", help="审计命令")
    audit_sub = p_audit.add_subparsers(dest="audit_command", required=True)
    p_audit_list = audit_sub.add_parser("list", help="列出审计项")
    p_audit_list.add_argument("--scan-session-id", required=True, type=int, help="扫描会话 id")
    p_audit_list.add_argument("--workspace", required=True, help="workspace 根目录")
    p_audit_list.set_defaults(func=_cmd_audit_list)

    p_source = sub.add_parser("source", help="source 命令")
    source_sub = p_source.add_subparsers(dest="source_command", required=True)

    p_source_add = source_sub.add_parser("add", help="添加 source")
    p_source_add.add_argument("root_path", help="source 根目录")
    p_source_add.add_argument("--label", help="source 标签")
    p_source_add.add_argument("--workspace", required=True, help="workspace 根目录")
    p_source_add.set_defaults(func=_cmd_source_add)

    p_source_list = source_sub.add_parser("list", help="列出 source")
    p_source_list.add_argument("--workspace", required=True, help="workspace 根目录")
    p_source_list.set_defaults(func=_cmd_source_list)

    p_source_disable = source_sub.add_parser("disable", help="禁用 source")
    p_source_disable.add_argument("source_id", type=int, help="source id")
    p_source_disable.add_argument("--workspace", required=True, help="workspace 根目录")
    p_source_disable.set_defaults(func=_cmd_source_disable)

    p_source_enable = source_sub.add_parser("enable", help="启用 source")
    p_source_enable.add_argument("source_id", type=int, help="source id")
    p_source_enable.add_argument("--workspace", required=True, help="workspace 根目录")
    p_source_enable.set_defaults(func=_cmd_source_enable)

    p_source_relabel = source_sub.add_parser("relabel", help="重命名 source 标签")
    p_source_relabel.add_argument("source_id", type=int, help="source id")
    p_source_relabel.add_argument("label", help="新标签")
    p_source_relabel.add_argument("--workspace", required=True, help="workspace 根目录")
    p_source_relabel.set_defaults(func=_cmd_source_relabel)

    p_source_remove = source_sub.add_parser("remove", help="删除 source")
    p_source_remove.add_argument("source_id", type=int, help="source id")
    p_source_remove.add_argument("--workspace", required=True, help="workspace 根目录")
    p_source_remove.set_defaults(func=_cmd_source_remove)

    p_export = sub.add_parser("export", help="导出命令")
    export_sub = p_export.add_subparsers(dest="export_command", required=True)

    p_export_template = export_sub.add_parser("template", help="导出模板命令")
    export_template_sub = p_export_template.add_subparsers(dest="export_template_command", required=True)

    p_export_template_list = export_template_sub.add_parser("list", help="列出导出模板")
    p_export_template_list.add_argument("--workspace", required=True, help="workspace 根目录")
    p_export_template_list.set_defaults(func=_cmd_export_template_list)

    p_export_template_create = export_template_sub.add_parser("create", help="创建导出模板")
    p_export_template_create.add_argument("--name", required=True, help="模板名称")
    p_export_template_create.add_argument("--output-root", required=True, help="导出根目录")
    p_export_template_create.add_argument("--person-ids", required=True, help="逗号分隔的人物 id 列表")
    p_export_template_create.add_argument("--enabled", help="是否启用，true/false")
    p_export_template_create.add_argument("--workspace", required=True, help="workspace 根目录")
    p_export_template_create.set_defaults(func=_cmd_export_template_create)

    p_export_template_update = export_template_sub.add_parser("update", help="更新导出模板")
    p_export_template_update.add_argument("template_id", type=int, help="模板 id")
    p_export_template_update.add_argument("--name", help="模板名称")
    p_export_template_update.add_argument("--output-root", help="导出根目录")
    p_export_template_update.add_argument("--person-ids", help="逗号分隔的人物 id 列表")
    p_export_template_update.add_argument("--enabled", help="是否启用，true/false")
    p_export_template_update.add_argument("--workspace", required=True, help="workspace 根目录")
    p_export_template_update.set_defaults(func=_cmd_export_template_update)

    p_export_run = export_sub.add_parser("run", help="启动导出运行")
    p_export_run.add_argument("template_id", type=int, help="模板 id")
    p_export_run.add_argument("--workspace", required=True, help="workspace 根目录")
    p_export_run.set_defaults(func=_cmd_export_run)

    p_export_run_status = export_sub.add_parser("run-status", help="查看导出运行状态")
    p_export_run_status.add_argument("export_run_id", type=int, help="导出运行 id")
    p_export_run_status.add_argument("--workspace", required=True, help="workspace 根目录")
    p_export_run_status.set_defaults(func=_cmd_export_run_status)

    p_export_execute = export_sub.add_parser("execute", help="执行导出运行")
    p_export_execute.add_argument("export_run_id", type=int, help="导出运行 id")
    p_export_execute.add_argument("--workspace", required=True, help="workspace 根目录")
    p_export_execute.set_defaults(func=_cmd_export_execute)

    p_export_run_list = export_sub.add_parser("run-list", help="列出导出运行")
    p_export_run_list.add_argument("--template-id", type=int, help="模板 id")
    p_export_run_list.add_argument("--limit", type=int, default=20, help="返回条数")
    p_export_run_list.add_argument("--workspace", required=True, help="workspace 根目录")
    p_export_run_list.set_defaults(func=_cmd_export_run_list)

    p_logs = sub.add_parser("logs", help="运行日志命令")
    logs_sub = p_logs.add_subparsers(dest="logs_command", required=True)
    p_logs_list = logs_sub.add_parser("list", help="列出运行日志")
    p_logs_list.add_argument("--scan-session-id", type=int, help="扫描会话 id")
    p_logs_list.add_argument("--export-run-id", type=int, help="导出运行 id")
    p_logs_list.add_argument("--severity", help="日志等级")
    p_logs_list.add_argument("--limit", type=int, default=50, help="返回条数")
    p_logs_list.add_argument("--workspace", required=True, help="workspace 根目录")
    p_logs_list.set_defaults(func=_cmd_logs_list)

    p_db = sub.add_parser("db", help="数据库命令")
    db_sub = p_db.add_subparsers(dest="db_command", required=True)
    p_db_vacuum = db_sub.add_parser("vacuum", help="执行 VACUUM")
    p_db_vacuum.add_argument("--library", action="store_true", help="压缩 library.db")
    p_db_vacuum.add_argument("--embedding", action="store_true", help="压缩 embedding.db")
    p_db_vacuum.add_argument("--workspace", required=True, help="workspace 根目录")
    p_db_vacuum.set_defaults(func=_cmd_db_vacuum)

    return parser


def cli_entry(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        return int(args.func(args))
    except SystemExit:
        raise
    except SourceRootPathConflictError as exc:
        _print_error(locals().get("args"), code="VALIDATION_ERROR", message=str(exc))
        return EXIT_CODE_VALIDATION
    except (ValueError, InvalidRunKindError, InvalidTriggeredByError) as exc:
        _print_error(locals().get("args"), code="VALIDATION_ERROR", message=str(exc))
        return EXIT_CODE_VALIDATION
    except PeopleMergeError as exc:
        _print_error(locals().get("args"), code="VALIDATION_ERROR", message=str(exc))
        return EXIT_CODE_VALIDATION
    except ExportValidationError as exc:
        _print_error(locals().get("args"), code="VALIDATION_ERROR", message=str(exc))
        return EXIT_CODE_VALIDATION
    except ExportTemplateDuplicateError as exc:
        _print_error(locals().get("args"), code="EXPORT_TEMPLATE_DUPLICATE", message=str(exc))
        return EXIT_CODE_VALIDATION
    except SessionNotFoundError as exc:
        _print_error(locals().get("args"), code="NOT_FOUND", message=str(exc))
        return EXIT_CODE_NOT_FOUND
    except (PeopleExcludeConflictError, PeopleUndoMergeConflictError) as exc:
        _print_error(locals().get("args"), code="ILLEGAL_STATE", message=str(exc))
        return EXIT_CODE_ILLEGAL_STATE
    except (PeopleNotFoundError, PeopleUndoMergeError, ExportTemplateNotFoundError, ExportRunNotFoundError, SourceNotFoundError) as exc:
        _print_error(locals().get("args"), code="NOT_FOUND", message=str(exc))
        return EXIT_CODE_NOT_FOUND
    except ScanActiveConflictError as exc:
        _print_error(
            locals().get("args"),
            code="SCAN_ACTIVE_CONFLICT",
            message=str(exc),
            extra={"active_session_id": exc.active_session_id},
        )
        return EXIT_CODE_SCAN_ACTIVE_CONFLICT
    except ExportRunningLockError as exc:
        _print_error(locals().get("args"), code=exc.error_code, message=str(exc))
        return EXIT_CODE_EXPORT_RUNNING_LOCK
    except ServeBlockedByActiveScanError as exc:
        _print_error(
            locals().get("args"),
            code="SERVE_BLOCKED_BY_ACTIVE_SCAN",
            message=str(exc),
            extra={"active_session_id": exc.active_session_id},
        )
        return EXIT_CODE_SERVE_BLOCKED
    except Exception as exc:  # noqa: BLE001
        _print_error(locals().get("args"), code="UNCLASSIFIED_ERROR", message=str(exc))
        return EXIT_CODE_UNCLASSIFIED


if __name__ == "__main__":
    raise SystemExit(cli_entry())
