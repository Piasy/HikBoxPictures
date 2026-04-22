from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hikbox_pictures.product.audit import AuditSamplingService
from hikbox_pictures.product.config import WorkspaceLayout, initialize_workspace
from hikbox_pictures.product.db.connection import connect_sqlite
from hikbox_pictures.product.export import ExportRunLockError, ExportValidationError
from hikbox_pictures.product.export.run_service import ExportRunService, assert_people_writes_allowed
from hikbox_pictures.product.export.template_service import ExportTemplateService
from hikbox_pictures.product.ops_event import OpsEventService
from hikbox_pictures.product.people.repository import SQLitePeopleRepository
from hikbox_pictures.product.people.service import MergeOperationNotFoundError, PeopleService
from hikbox_pictures.product.scan.errors import (
    ScanActiveConflictError,
    ScanSessionIllegalStatusError,
    ScanSessionNotFoundError,
    ServeBlockedByActiveScanError,
)
from hikbox_pictures.product.scan.execution_service import ScanExecutionService
from hikbox_pictures.product.scan.session_service import (
    SQLiteScanSessionRepository,
    ScanSessionService,
    assert_no_active_scan_for_serve,
)
from hikbox_pictures.product.source.repository import SQLiteSourceRepository
from hikbox_pictures.product.source.service import SourceDeletedError, SourceNotFoundError, SourceService

EXIT_OK = 0
EXIT_OTHER = 1
EXIT_VALIDATION = 2
EXIT_NOT_FOUND = 3
EXIT_SCAN_CONFLICT = 4
EXIT_EXPORT_LOCK = 5
EXIT_ILLEGAL_STATE = 6
EXIT_SERVE_BLOCKED = 7


@dataclass(frozen=True)
class CliError(Exception):
    code: str
    message: str
    exit_code: int


@dataclass(frozen=True)
class CliContext:
    workspace_root: Path
    json_output: bool
    quiet: bool


def cli_entry(argv: list[str] | None = None) -> int:
    normalized_argv = _normalize_global_options(list(sys.argv[1:] if argv is None else argv))
    parser = _build_parser()
    ctx = _context_from_argv(normalized_argv)

    try:
        args = parser.parse_args(normalized_argv)
        ctx = CliContext(
            workspace_root=Path(args.workspace).resolve(),
            json_output=bool(args.json),
            quiet=bool(args.quiet),
        )
        result = _dispatch(ctx, args)
        _print_success(ctx, result)
        return EXIT_OK
    except CliError as exc:
        _print_error(ctx, exc.code, exc.message)
        return exc.exit_code
    except KeyboardInterrupt:
        _print_error(ctx, "INTERRUPTED", "命令被中断")
        return EXIT_OTHER
    except Exception as exc:  # noqa: BLE001
        _print_error(ctx, "UNCLASSIFIED_ERROR", str(exc))
        return EXIT_OTHER


class _CliArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliError("VALIDATION_ERROR", message, EXIT_VALIDATION)


def _build_parser() -> argparse.ArgumentParser:
    parser = _CliArgumentParser(prog="hikbox-pictures", description="HikBox Pictures CLI")
    parser.add_argument("--workspace", default=".", help="工作区根目录，默认当前目录")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--quiet", action="store_true", help="仅输出错误")

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="初始化工作区")

    config = subparsers.add_parser("config", help="配置")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("show", help="显示配置")
    config_set = config_sub.add_parser("set-external-root", help="设置外部目录")
    config_set.add_argument("abs_path")

    source = subparsers.add_parser("source", help="源管理")
    source_sub = source.add_subparsers(dest="source_command", required=True)
    source_sub.add_parser("list", help="列出 source")
    source_add = source_sub.add_parser("add", help="添加 source")
    source_add.add_argument("abs_path")
    source_add.add_argument("--label")
    source_remove = source_sub.add_parser("remove", help="删除 source")
    source_remove.add_argument("source_id", type=int)
    source_enable = source_sub.add_parser("enable", help="启用 source")
    source_enable.add_argument("source_id", type=int)
    source_disable = source_sub.add_parser("disable", help="禁用 source")
    source_disable.add_argument("source_id", type=int)
    source_relabel = source_sub.add_parser("relabel", help="修改标签")
    source_relabel.add_argument("source_id", type=int)
    source_relabel.add_argument("label")

    scan = subparsers.add_parser("scan", help="扫描")
    scan_sub = scan.add_subparsers(dest="scan_command", required=True)
    scan_sub.add_parser("start-or-resume", help="启动或恢复")
    scan_sub.add_parser("start-new", help="启动新会话")
    scan_abort = scan_sub.add_parser("abort", help="中止")
    scan_abort.add_argument("session_id", type=int)
    scan_status = scan_sub.add_parser("status", help="会话状态")
    status_group = scan_status.add_mutually_exclusive_group()
    status_group.add_argument("--session-id", type=int)
    status_group.add_argument("--latest", action="store_true")
    scan_list = scan_sub.add_parser("list", help="会话列表")
    scan_list.add_argument("--limit", type=int, default=20)
    scan_run_session = scan_sub.add_parser("_run-session", help=argparse.SUPPRESS)
    scan_run_session.add_argument("--session-id", type=int, required=True)

    serve = subparsers.add_parser("serve", help="启动 Web 服务")
    serve_sub = serve.add_subparsers(dest="serve_command", required=True)
    serve_start = serve_sub.add_parser("start", help="启动")
    serve_start.add_argument("--host", default="127.0.0.1")
    serve_start.add_argument("--port", type=int, default=8000)

    people = subparsers.add_parser("people", help="人物")
    people_sub = people.add_subparsers(dest="people_command", required=True)
    people_list = people_sub.add_parser("list", help="列出人物")
    people_list_filter = people_list.add_mutually_exclusive_group()
    people_list_filter.add_argument("--named", action="store_true")
    people_list_filter.add_argument("--anonymous", action="store_true")
    people_show = people_sub.add_parser("show", help="人物详情")
    people_show.add_argument("person_id", type=int)
    people_rename = people_sub.add_parser("rename", help="重命名")
    people_rename.add_argument("person_id", type=int)
    people_rename.add_argument("display_name")
    people_exclude = people_sub.add_parser("exclude", help="排除样本")
    people_exclude.add_argument("person_id", type=int)
    people_exclude.add_argument("--face-observation-id", type=int, required=True)
    people_exclude_batch = people_sub.add_parser("exclude-batch", help="批量排除")
    people_exclude_batch.add_argument("person_id", type=int)
    people_exclude_batch.add_argument("--face-observation-ids", required=True)
    people_merge = people_sub.add_parser("merge", help="合并")
    people_merge.add_argument("--selected-person-ids", required=True)
    people_sub.add_parser("undo-last-merge", help="撤销最近合并")

    export = subparsers.add_parser("export", help="导出")
    export_sub = export.add_subparsers(dest="export_command", required=True)
    export_template = export_sub.add_parser("template", help="模板")
    export_template_sub = export_template.add_subparsers(dest="export_template_command", required=True)
    export_template_sub.add_parser("list", help="模板列表")
    export_template_create = export_template_sub.add_parser("create", help="创建模板")
    export_template_create.add_argument("--name", required=True)
    export_template_create.add_argument("--output-root", required=True)
    export_template_create.add_argument("--person-ids")
    export_template_update = export_template_sub.add_parser("update", help="更新模板")
    export_template_update.add_argument("template_id", type=int)
    export_template_update.add_argument("--name")
    export_template_update.add_argument("--output-root")
    export_template_update.add_argument("--person-ids")
    export_run = export_sub.add_parser("run", help="启动导出")
    export_run.add_argument("template_id", type=int)
    export_run_status = export_sub.add_parser("run-status", help="导出状态")
    export_run_status.add_argument("export_run_id", type=int)
    export_run_list = export_sub.add_parser("run-list", help="导出列表")
    export_run_list.add_argument("--template-id", type=int)
    export_run_list.add_argument("--limit", type=int, default=20)

    logs = subparsers.add_parser("logs", help="日志")
    logs_sub = logs.add_subparsers(dest="logs_command", required=True)
    logs_list = logs_sub.add_parser("list", help="日志列表")
    logs_list.add_argument("--scan-session-id", type=int)
    logs_list.add_argument("--export-run-id", type=int)
    logs_list.add_argument("--severity", choices=["info", "warning", "error"])
    logs_list.add_argument("--limit", type=int, default=50)

    audit = subparsers.add_parser("audit", help="审计")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    audit_list = audit_sub.add_parser("list", help="审计列表")
    audit_list.add_argument("--scan-session-id", type=int, required=True)

    db = subparsers.add_parser("db", help="数据库维护")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    db_vacuum = db_sub.add_parser("vacuum", help="vacuum")
    db_vacuum.add_argument("--library", action="store_true")
    db_vacuum.add_argument("--embedding", action="store_true")

    return parser


def _dispatch(ctx: CliContext, args: argparse.Namespace) -> dict[str, Any]:
    command = str(args.command)
    if command == "init":
        return _cmd_init(ctx)
    if command == "config":
        return _cmd_config(ctx, args)
    if command == "source":
        return _cmd_source(ctx, args)
    if command == "scan":
        return _cmd_scan(ctx, args)
    if command == "serve":
        return _cmd_serve(ctx, args)
    if command == "people":
        return _cmd_people(ctx, args)
    if command == "export":
        return _cmd_export(ctx, args)
    if command == "logs":
        return _cmd_logs(ctx, args)
    if command == "audit":
        return _cmd_audit(ctx, args)
    if command == "db":
        return _cmd_db(ctx, args)
    raise CliError("VALIDATION_ERROR", f"未知命令: {command}", EXIT_VALIDATION)


def _cmd_init(ctx: CliContext) -> dict[str, Any]:
    external_root = (ctx.workspace_root / "external").resolve()
    try:
        layout = initialize_workspace(ctx.workspace_root, external_root)
    except ValueError as exc:
        raise CliError("VALIDATION_ERROR", str(exc), EXIT_VALIDATION) from exc
    return {
        "workspace": str(layout.workspace_root),
        "library_db": str(layout.library_db_path),
        "embedding_db": str(layout.embedding_db_path),
        "external_root": str(layout.external_root),
    }


def _cmd_config(ctx: CliContext, args: argparse.Namespace) -> dict[str, Any]:
    command = str(args.config_command)
    layout = _load_workspace(ctx.workspace_root)
    if command == "show":
        return {
            "workspace": str(layout.workspace_root),
            "external_root": str(layout.external_root),
            "library_db": str(layout.library_db_path),
            "embedding_db": str(layout.embedding_db_path),
        }
    if command == "set-external-root":
        raw = Path(str(args.abs_path))
        if not raw.is_absolute():
            raise CliError("VALIDATION_ERROR", "external_root 必须是绝对路径", EXIT_VALIDATION)
        external_root = raw.resolve()
        _write_workspace_config(layout, external_root)
        return {
            "workspace": str(layout.workspace_root),
            "external_root": str(external_root),
            "updated": True,
        }
    raise CliError("VALIDATION_ERROR", f"未知 config 子命令: {command}", EXIT_VALIDATION)


def _cmd_source(ctx: CliContext, args: argparse.Namespace) -> dict[str, Any]:
    layout = _load_workspace(ctx.workspace_root)
    service = SourceService(SQLiteSourceRepository(layout.library_db_path))
    command = str(args.source_command)
    try:
        if command == "list":
            items = service.list_sources(include_deleted=True)
            return {
                "items": [
                    {
                        "source_id": item.id,
                        "root_path": item.root_path,
                        "label": item.label,
                        "enabled": bool(item.enabled),
                        "status": item.status,
                    }
                    for item in items
                ]
            }
        if command == "add":
            label = str(args.label) if args.label is not None else _default_source_label(Path(args.abs_path))
            created = service.add_source(root_path=Path(args.abs_path), label=label)
            return {
                "source_id": created.id,
                "root_path": created.root_path,
                "label": created.label,
                "enabled": bool(created.enabled),
                "status": created.status,
            }
        if command == "remove":
            updated = service.remove_source(int(args.source_id))
            return {"source_id": updated.id, "status": updated.status, "enabled": bool(updated.enabled)}
        if command == "enable":
            updated = service.enable_source(int(args.source_id))
            return {"source_id": updated.id, "enabled": bool(updated.enabled), "status": updated.status}
        if command == "disable":
            updated = service.disable_source(int(args.source_id))
            return {"source_id": updated.id, "enabled": bool(updated.enabled), "status": updated.status}
        if command == "relabel":
            updated = service.relabel_source(int(args.source_id), str(args.label))
            return {"source_id": updated.id, "label": updated.label, "status": updated.status}
    except SourceNotFoundError as exc:
        raise CliError("NOT_FOUND", str(exc), EXIT_NOT_FOUND) from exc
    except SourceDeletedError as exc:
        raise CliError("ILLEGAL_STATE", str(exc), EXIT_ILLEGAL_STATE) from exc
    except ValueError as exc:
        raise CliError("VALIDATION_ERROR", str(exc), EXIT_VALIDATION) from exc

    raise CliError("VALIDATION_ERROR", f"未知 source 子命令: {command}", EXIT_VALIDATION)


def _cmd_scan(ctx: CliContext, args: argparse.Namespace) -> dict[str, Any]:
    layout = _load_workspace(ctx.workspace_root)
    service = ScanSessionService(SQLiteScanSessionRepository(layout.library_db_path))
    command = str(args.scan_command)

    try:
        if command == "_run-session":
            session_id = int(args.session_id)
            lock_path = _scan_runner_lock_path(layout=layout, session_id=session_id)
            with _scan_runner_lock(lock_path) as locked:
                if not locked:
                    return {"session_id": session_id, "status": "skipped_locked"}
                execution = ScanExecutionService(
                    library_db_path=layout.library_db_path,
                    embedding_db_path=layout.embedding_db_path,
                )
                status = execution.run_session(session_id=session_id)
                return {"session_id": session_id, "status": status}
        if command == "start-or-resume":
            session = service.start_or_resume(run_kind="scan_full", triggered_by="manual_cli")
            terminal_status = _run_scan_session_blocking(layout=layout, session_id=session.id)
            return {"session_id": session.id, "status": terminal_status, "resumed": bool(session.resumed)}
        if command == "start-new":
            session = service.start_new(run_kind="scan_full", triggered_by="manual_cli")
            terminal_status = _run_scan_session_blocking(layout=layout, session_id=session.id)
            return {"session_id": session.id, "status": terminal_status, "resumed": False}
        if command == "abort":
            session = service.abort(int(args.session_id))
            return {"session_id": session.id, "status": session.status}
        if command == "status":
            return _scan_status(layout.library_db_path, session_id=args.session_id, latest=bool(args.latest))
        if command == "list":
            limit = int(args.limit)
            if limit <= 0:
                raise CliError("VALIDATION_ERROR", "limit 必须 > 0", EXIT_VALIDATION)
            return _scan_list(layout.library_db_path, limit=limit)
    except ScanActiveConflictError as exc:
        raise CliError("SCAN_ACTIVE_CONFLICT", str(exc), EXIT_SCAN_CONFLICT) from exc
    except ScanSessionNotFoundError as exc:
        raise CliError("NOT_FOUND", str(exc), EXIT_NOT_FOUND) from exc
    except ScanSessionIllegalStatusError as exc:
        raise CliError("ILLEGAL_STATE", str(exc), EXIT_ILLEGAL_STATE) from exc
    except ValueError as exc:
        raise CliError("VALIDATION_ERROR", str(exc), EXIT_VALIDATION) from exc

    raise CliError("VALIDATION_ERROR", f"未知 scan 子命令: {command}", EXIT_VALIDATION)


def _cmd_serve(ctx: CliContext, args: argparse.Namespace) -> dict[str, Any]:
    import uvicorn
    from hikbox_pictures.web.app import ServiceContainer, create_app

    layout = _load_workspace(ctx.workspace_root)
    command = str(args.serve_command)
    if command != "start":
        raise CliError("VALIDATION_ERROR", f"未知 serve 子命令: {command}", EXIT_VALIDATION)

    repo = SQLiteScanSessionRepository(layout.library_db_path)
    try:
        assert_no_active_scan_for_serve(repo)
    except ServeBlockedByActiveScanError as exc:
        raise CliError("SERVE_BLOCKED_BY_ACTIVE_SCAN", str(exc), EXIT_SERVE_BLOCKED) from exc

    app = create_app(ServiceContainer.from_library_db(layout.library_db_path))
    uvicorn.run(app, host=str(args.host), port=int(args.port), log_level="info")
    return {"started": True, "host": str(args.host), "port": int(args.port)}


def _cmd_people(ctx: CliContext, args: argparse.Namespace) -> dict[str, Any]:
    layout = _load_workspace(ctx.workspace_root)
    service = PeopleService(SQLitePeopleRepository(layout.library_db_path))
    command = str(args.people_command)

    try:
        if command == "list":
            return _people_list(layout.library_db_path, named=bool(args.named), anonymous=bool(args.anonymous))
        if command == "show":
            return _people_show(layout.library_db_path, int(args.person_id))
        if command == "rename":
            assert_people_writes_allowed(layout.library_db_path)
            person = service.rename_person(person_id=int(args.person_id), display_name=str(args.display_name))
            return {
                "person_id": person.id,
                "person_uuid": person.person_uuid,
                "display_name": person.display_name,
                "is_named": bool(person.is_named),
                "status": person.status,
            }
        if command == "exclude":
            assert_people_writes_allowed(layout.library_db_path)
            result = service.exclude_assignment(person_id=int(args.person_id), face_observation_id=int(args.face_observation_id))
            return {
                "person_id": result.person_id,
                "face_observation_id": result.face_observation_id,
                "pending_reassign": result.pending_reassign,
            }
        if command == "exclude-batch":
            assert_people_writes_allowed(layout.library_db_path)
            result = service.exclude_assignments(
                person_id=int(args.person_id),
                face_observation_ids=_parse_int_csv(str(args.face_observation_ids), field_name="face_observation_ids"),
            )
            return {"person_id": result.person_id, "excluded_count": result.excluded_count}
        if command == "merge":
            assert_people_writes_allowed(layout.library_db_path)
            result = service.merge_people(
                selected_person_ids=_parse_int_csv(str(args.selected_person_ids), field_name="selected_person_ids"),
            )
            return {
                "merge_operation_id": result.merge_operation_id,
                "winner_person_id": result.winner_person_id,
                "winner_person_uuid": result.winner_person_uuid,
            }
        if command == "undo-last-merge":
            assert_people_writes_allowed(layout.library_db_path)
            result = service.undo_last_merge()
            return {"merge_operation_id": result.merge_operation_id, "status": result.status}
    except ExportRunLockError as exc:
        raise CliError("EXPORT_RUNNING_LOCK", str(exc), EXIT_EXPORT_LOCK) from exc
    except MergeOperationNotFoundError as exc:
        raise CliError("NOT_FOUND", str(exc), EXIT_NOT_FOUND) from exc
    except ValueError as exc:
        message = str(exc)
        if "已排除" in message or "不允许" in message:
            raise CliError("ILLEGAL_STATE", message, EXIT_ILLEGAL_STATE) from exc
        if "不存在" in message:
            raise CliError("NOT_FOUND", message, EXIT_NOT_FOUND) from exc
        raise CliError("VALIDATION_ERROR", message, EXIT_VALIDATION) from exc

    raise CliError("VALIDATION_ERROR", f"未知 people 子命令: {command}", EXIT_VALIDATION)


def _cmd_export(ctx: CliContext, args: argparse.Namespace) -> dict[str, Any]:
    layout = _load_workspace(ctx.workspace_root)
    template_service = ExportTemplateService(layout.library_db_path)
    run_service = ExportRunService(layout.library_db_path)
    command = str(args.export_command)

    try:
        if command == "template":
            return _cmd_export_template(layout, template_service, args)
        if command == "run":
            run = run_service.execute_export(template_id=int(args.template_id))
            return {"export_run_id": run.id, "status": run.status}
        if command == "run-status":
            return _export_run_status(layout.library_db_path, int(args.export_run_id))
        if command == "run-list":
            limit = int(args.limit)
            if limit <= 0:
                raise CliError("VALIDATION_ERROR", "limit 必须 > 0", EXIT_VALIDATION)
            return _export_run_list(layout.library_db_path, template_id=args.template_id, limit=limit)
    except ExportValidationError as exc:
        message = str(exc)
        if "不存在" in message:
            raise CliError("NOT_FOUND", message, EXIT_NOT_FOUND) from exc
        raise CliError("VALIDATION_ERROR", message, EXIT_VALIDATION) from exc
    except sqlite3.IntegrityError as exc:
        raise CliError("ILLEGAL_STATE", str(exc), EXIT_ILLEGAL_STATE) from exc

    raise CliError("VALIDATION_ERROR", f"未知 export 子命令: {command}", EXIT_VALIDATION)


def _cmd_export_template(
    layout: WorkspaceLayout,
    template_service: ExportTemplateService,
    args: argparse.Namespace,
) -> dict[str, Any]:
    command = str(args.export_template_command)
    if command == "list":
        templates = template_service.list_templates()
        return {
            "items": [
                {
                    "template_id": item.id,
                    "name": item.name,
                    "output_root": item.output_root,
                    "enabled": bool(item.enabled),
                    "person_ids": item.person_ids,
                }
                for item in templates
            ]
        }

    if command == "create":
        person_ids = _parse_int_csv(str(args.person_ids), field_name="person_ids") if args.person_ids else _default_named_person_ids(
            layout.library_db_path
        )
        created = template_service.create_template(
            name=str(args.name),
            output_root=Path(args.output_root),
            person_ids=person_ids,
        )
        return {"template_id": created.id}

    if command == "update":
        person_ids: list[int] | None
        if args.person_ids is None:
            person_ids = None
        else:
            person_ids = _parse_int_csv(str(args.person_ids), field_name="person_ids")
        updated = template_service.update_template(
            template_id=int(args.template_id),
            name=args.name,
            output_root=Path(args.output_root) if args.output_root is not None else None,
            person_ids=person_ids,
        )
        return {
            "template_id": updated.id,
            "updated": True,
        }

    raise CliError("VALIDATION_ERROR", f"未知 export template 子命令: {command}", EXIT_VALIDATION)


def _cmd_logs(ctx: CliContext, args: argparse.Namespace) -> dict[str, Any]:
    layout = _load_workspace(ctx.workspace_root)
    if str(args.logs_command) != "list":
        raise CliError("VALIDATION_ERROR", f"未知 logs 子命令: {args.logs_command}", EXIT_VALIDATION)
    service = OpsEventService(layout.library_db_path)
    try:
        events = service.query_events(
            scan_session_id=args.scan_session_id,
            export_run_id=args.export_run_id,
            severity=args.severity,
            limit=int(args.limit),
            offset=0,
        )
    except ValueError as exc:
        raise CliError("VALIDATION_ERROR", str(exc), EXIT_VALIDATION) from exc

    return {
        "items": [
            {
                "event_id": item.id,
                "event_type": item.event_type,
                "severity": item.severity,
                "scan_session_id": item.scan_session_id,
                "export_run_id": item.export_run_id,
                "payload_json": item.payload_json,
                "created_at": item.created_at,
            }
            for item in events
        ]
    }


def _cmd_audit(ctx: CliContext, args: argparse.Namespace) -> dict[str, Any]:
    layout = _load_workspace(ctx.workspace_root)
    if str(args.audit_command) != "list":
        raise CliError("VALIDATION_ERROR", f"未知 audit 子命令: {args.audit_command}", EXIT_VALIDATION)

    scan_session_id = int(args.scan_session_id)
    with connect_sqlite(layout.library_db_path) as conn:
        row = conn.execute("SELECT 1 FROM scan_session WHERE id=? LIMIT 1", (scan_session_id,)).fetchone()
    if row is None:
        raise CliError("NOT_FOUND", f"扫描会话不存在: session_id={scan_session_id}", EXIT_NOT_FOUND)

    service = AuditSamplingService(layout.library_db_path)
    items = service.list_audit_items(scan_session_id=scan_session_id, limit=500, offset=0)
    return {
        "items": [
            {
                "audit_type": item.audit_type,
                "face_observation_id": item.face_observation_id,
                "person_id": item.person_id,
                "evidence_json": item.evidence_json,
            }
            for item in items
        ]
    }


def _cmd_db(ctx: CliContext, args: argparse.Namespace) -> dict[str, Any]:
    layout = _load_workspace(ctx.workspace_root)
    if str(args.db_command) != "vacuum":
        raise CliError("VALIDATION_ERROR", f"未知 db 子命令: {args.db_command}", EXIT_VALIDATION)

    targets: list[tuple[str, Path]] = []
    if bool(args.library) or (not bool(args.library) and not bool(args.embedding)):
        targets.append(("library", layout.library_db_path))
    if bool(args.embedding) or (not bool(args.library) and not bool(args.embedding)):
        targets.append(("embedding", layout.embedding_db_path))

    for _name, path in targets:
        with connect_sqlite(path) as conn:
            conn.execute("VACUUM")
            conn.commit()

    return {
        "vacuumed": [name for name, _ in targets],
    }


def _load_workspace(workspace_root: Path) -> WorkspaceLayout:
    workspace = Path(workspace_root).resolve()
    hikbox_root = workspace / ".hikbox"
    config_path = hikbox_root / "config.json"
    library_db = hikbox_root / "library.db"
    embedding_db = hikbox_root / "embedding.db"
    if not config_path.exists() or not library_db.exists() or not embedding_db.exists():
        raise CliError("NOT_FOUND", f"工作区未初始化: {workspace}", EXIT_NOT_FOUND)

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CliError("VALIDATION_ERROR", f"配置文件非法 JSON: {config_path}", EXIT_VALIDATION) from exc
    if not isinstance(raw, dict):
        raise CliError("VALIDATION_ERROR", "配置文件格式非法：根节点必须是对象", EXIT_VALIDATION)
    raw_external_root = raw.get("external_root")
    if not isinstance(raw_external_root, str) or not raw_external_root.strip():
        raise CliError("VALIDATION_ERROR", "external_root 缺失或为空", EXIT_VALIDATION)
    raw_external_root_path = Path(raw_external_root)
    if not raw_external_root_path.is_absolute():
        raise CliError("VALIDATION_ERROR", "external_root 必须是绝对路径", EXIT_VALIDATION)
    external_root = raw_external_root_path.resolve()

    return WorkspaceLayout(
        workspace_root=workspace,
        hikbox_root=hikbox_root,
        config_path=config_path,
        library_db_path=library_db,
        embedding_db_path=embedding_db,
        external_root=external_root,
        artifacts_root=external_root / "artifacts",
        crops_root=external_root / "artifacts" / "crops",
        aligned_root=external_root / "artifacts" / "aligned",
        context_root=external_root / "artifacts" / "context",
        logs_root=external_root / "logs",
    )


def _write_workspace_config(layout: WorkspaceLayout, external_root: Path) -> None:
    cfg = {
        "version": 1,
        "external_root": str(external_root),
    }
    layout.config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    (external_root / "artifacts" / "crops").mkdir(parents=True, exist_ok=True)
    (external_root / "artifacts" / "aligned").mkdir(parents=True, exist_ok=True)
    (external_root / "artifacts" / "context").mkdir(parents=True, exist_ok=True)
    (external_root / "logs").mkdir(parents=True, exist_ok=True)


def _run_scan_session_blocking(layout: WorkspaceLayout, *, session_id: int) -> str:
    lock_path = _scan_runner_lock_path(layout=layout, session_id=session_id)
    with _scan_runner_lock(lock_path) as locked:
        if locked:
            execution = ScanExecutionService(
                library_db_path=layout.library_db_path,
                embedding_db_path=layout.embedding_db_path,
            )
            return execution.run_session(session_id=int(session_id))

    # 其他进程已接管同一会话时，前台阻塞等待其收敛到终态。
    return _wait_scan_terminal_status(layout.library_db_path, session_id=int(session_id))


def _scan_runner_lock_path(*, layout: WorkspaceLayout, session_id: int) -> Path:
    return layout.hikbox_root / "runner_locks" / f"scan_session_{int(session_id)}.lock"


@contextlib.contextmanager
def _scan_runner_lock(lock_path: Path):
    try:
        import fcntl
    except ImportError:
        yield True
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        yield False
        return
    try:
        yield True
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _wait_scan_terminal_status(library_db_path: Path, *, session_id: int) -> str:
    while True:
        with connect_sqlite(library_db_path) as conn:
            row = conn.execute("SELECT status FROM scan_session WHERE id=?", (int(session_id),)).fetchone()
        if row is None:
            raise CliError("NOT_FOUND", f"扫描会话不存在: session_id={session_id}", EXIT_NOT_FOUND)
        status = str(row[0])
        if status not in {"running", "aborting"}:
            return status
        time.sleep(0.2)


def _scan_status(library_db_path: Path, *, session_id: int | None, latest: bool) -> dict[str, Any]:
    with connect_sqlite(library_db_path) as conn:
        if session_id is not None:
            row = conn.execute(
                "SELECT id, run_kind, status, triggered_by, created_at, updated_at FROM scan_session WHERE id=?",
                (int(session_id),),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, run_kind, status, triggered_by, created_at, updated_at FROM scan_session ORDER BY id DESC LIMIT 1"
            ).fetchone()
    if row is None:
        if latest:
            raise CliError("NOT_FOUND", "不存在扫描会话", EXIT_NOT_FOUND)
        raise CliError("NOT_FOUND", f"扫描会话不存在: session_id={session_id}", EXIT_NOT_FOUND)

    return {
        "session_id": int(row[0]),
        "run_kind": str(row[1]),
        "status": str(row[2]),
        "triggered_by": str(row[3]),
        "created_at": str(row[4]),
        "updated_at": str(row[5]),
    }


def _scan_list(library_db_path: Path, *, limit: int) -> dict[str, Any]:
    with connect_sqlite(library_db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, run_kind, status, triggered_by, created_at, updated_at
            FROM scan_session
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return {
        "items": [
            {
                "session_id": int(row[0]),
                "run_kind": str(row[1]),
                "status": str(row[2]),
                "triggered_by": str(row[3]),
                "created_at": str(row[4]),
                "updated_at": str(row[5]),
            }
            for row in rows
        ]
    }


def _people_list(library_db_path: Path, *, named: bool, anonymous: bool) -> dict[str, Any]:
    sql = "SELECT id, person_uuid, display_name, is_named, status, merged_into_person_id, created_at, updated_at FROM person WHERE status='active'"
    params: list[object] = []
    if named:
        sql += " AND is_named=1"
    if anonymous:
        sql += " AND is_named=0"
    sql += " ORDER BY id"

    with connect_sqlite(library_db_path) as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()

    items = [
        {
            "person_id": int(row[0]),
            "person_uuid": str(row[1]),
            "display_name": None if row[2] is None else str(row[2]),
            "is_named": bool(int(row[3])),
            "status": str(row[4]),
            "merged_into_person_id": None if row[5] is None else int(row[5]),
            "created_at": str(row[6]),
            "updated_at": str(row[7]),
        }
        for row in rows
    ]
    return {"total": len(items), "items": items}


def _people_show(library_db_path: Path, person_id: int) -> dict[str, Any]:
    with connect_sqlite(library_db_path) as conn:
        row = conn.execute(
            "SELECT id, person_uuid, display_name, is_named, status, merged_into_person_id, created_at, updated_at FROM person WHERE id=?",
            (int(person_id),),
        ).fetchone()
        if row is None:
            raise CliError("NOT_FOUND", f"person 不存在: id={person_id}", EXIT_NOT_FOUND)
        face_rows = conn.execute(
            """
            SELECT face_observation_id
            FROM person_face_assignment
            WHERE person_id=? AND active=1
            ORDER BY face_observation_id
            """,
            (int(person_id),),
        ).fetchall()

    return {
        "person_id": int(row[0]),
        "person_uuid": str(row[1]),
        "display_name": None if row[2] is None else str(row[2]),
        "is_named": bool(int(row[3])),
        "status": str(row[4]),
        "merged_into_person_id": None if row[5] is None else int(row[5]),
        "created_at": str(row[6]),
        "updated_at": str(row[7]),
        "assignment_face_ids": [int(r[0]) for r in face_rows],
    }


def _export_run_status(library_db_path: Path, export_run_id: int) -> dict[str, Any]:
    with connect_sqlite(library_db_path) as conn:
        try:
            row = conn.execute(
                "SELECT id, template_id, status, summary_json, started_at, finished_at FROM export_run WHERE id=?",
                (int(export_run_id),),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table: export_run" in str(exc):
                raise CliError("NOT_FOUND", "不存在导出运行记录", EXIT_NOT_FOUND) from exc
            raise
    if row is None:
        raise CliError("NOT_FOUND", f"导出运行不存在: export_run_id={export_run_id}", EXIT_NOT_FOUND)

    return {
        "export_run_id": int(row[0]),
        "template_id": int(row[1]),
        "status": str(row[2]),
        "summary_json": _safe_load_json(str(row[3]) if row[3] is not None else "{}"),
        "started_at": str(row[4]),
        "finished_at": None if row[5] is None else str(row[5]),
    }


def _export_run_list(library_db_path: Path, *, template_id: int | None, limit: int) -> dict[str, Any]:
    sql = "SELECT id, template_id, status, summary_json, started_at, finished_at FROM export_run"
    params: list[object] = []
    if template_id is not None:
        sql += " WHERE template_id=?"
        params.append(int(template_id))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))

    with connect_sqlite(library_db_path) as conn:
        try:
            rows = conn.execute(sql, tuple(params)).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table: export_run" in str(exc):
                return {"items": []}
            raise

    return {
        "items": [
            {
                "export_run_id": int(row[0]),
                "template_id": int(row[1]),
                "status": str(row[2]),
                "summary_json": _safe_load_json(str(row[3]) if row[3] is not None else "{}"),
                "started_at": str(row[4]),
                "finished_at": None if row[5] is None else str(row[5]),
            }
            for row in rows
        ]
    }


def _default_named_person_ids(library_db_path: Path) -> list[int]:
    with connect_sqlite(library_db_path) as conn:
        rows = conn.execute("SELECT id FROM person WHERE status='active' AND is_named=1 ORDER BY id").fetchall()
    person_ids = [int(row[0]) for row in rows]
    if not person_ids:
        raise CliError("VALIDATION_ERROR", "当前工作区没有可用的已命名人物", EXIT_VALIDATION)
    return person_ids


def _default_source_label(source_path: Path) -> str:
    name = source_path.name.strip()
    if name:
        return name
    text = str(source_path).rstrip("/").strip()
    if text:
        return text.split("/")[-1] or text
    raise CliError("VALIDATION_ERROR", "无法推导 source label，请显式传 --label", EXIT_VALIDATION)


def _parse_int_csv(raw: str, *, field_name: str) -> list[int]:
    values: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            value = int(token)
        except ValueError as exc:
            raise CliError("VALIDATION_ERROR", f"{field_name} 包含非整数值: {token}", EXIT_VALIDATION) from exc
        values.append(value)
    if not values:
        raise CliError("VALIDATION_ERROR", f"{field_name} 不能为空", EXIT_VALIDATION)
    return values


def _safe_load_json(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if isinstance(data, dict):
        return data
    return {}


def _print_success(ctx: CliContext, data: dict[str, Any]) -> None:
    if ctx.quiet:
        return
    if ctx.json_output:
        sys.stdout.write(json.dumps({"ok": True, "data": data}, ensure_ascii=False) + "\n")
        return
    if not data:
        return
    if "items" in data and isinstance(data["items"], list):
        for item in data["items"]:
            sys.stdout.write(json.dumps(item, ensure_ascii=False) + "\n")
        return
    sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")


def _print_error(ctx: CliContext, code: str, message: str) -> None:
    if ctx.json_output:
        sys.stderr.write(json.dumps({"ok": False, "error": {"code": code, "message": message}}, ensure_ascii=False) + "\n")
        return
    sys.stderr.write(f"{code}: {message}\n")


def _normalize_global_options(argv: list[str]) -> list[str]:
    global_tokens: list[str] = []
    rest_tokens: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in {"--json", "--quiet"}:
            global_tokens.append(token)
            index += 1
            continue
        if token == "--workspace":
            if index + 1 >= len(argv):
                rest_tokens.append(token)
                index += 1
                continue
            global_tokens.extend([token, argv[index + 1]])
            index += 2
            continue
        rest_tokens.append(token)
        index += 1
    return [*global_tokens, *rest_tokens]


def _context_from_argv(argv: list[str]) -> CliContext:
    workspace_root = Path(".")
    json_output = False
    quiet = False
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--json":
            json_output = True
            index += 1
            continue
        if token == "--quiet":
            quiet = True
            index += 1
            continue
        if token == "--workspace":
            if index + 1 < len(argv):
                workspace_root = Path(argv[index + 1])
            index += 2
            continue
        index += 1
    return CliContext(workspace_root=workspace_root.resolve(), json_output=json_output, quiet=quiet)


if __name__ == "__main__":
    raise SystemExit(cli_entry())
