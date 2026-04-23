"""最小 CLI 壳子：workspace 初始化、source 管理、scan 启动。"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from hikbox_pictures.product.config import WorkspaceLayout, initialize_workspace
from hikbox_pictures.product.scan.errors import ScanActiveConflictError
from hikbox_pictures.product.scan.execution_service import (
    ScanExecutionService,
    ScanRuntimeDefaults,
    build_scan_runtime_defaults,
)
from hikbox_pictures.product.scan.session_service import ScanSessionRepository, ScanSessionService
from hikbox_pictures.product.source.repository import SourceRecord, SourceRepository
from hikbox_pictures.product.source.service import SourceRootPathConflictError, SourceService


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


def _workspace_output_root(layout: WorkspaceLayout) -> Path:
    try:
        payload = json.loads(layout.config_json.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return layout.hikbox_root / "runtime"

    external_root = payload.get("external_root")
    if isinstance(external_root, str) and external_root.strip():
        return Path(external_root).expanduser().resolve()
    return layout.hikbox_root / "runtime"


def _require_workspace_initialized(workspace: Path) -> WorkspaceLayout:
    layout = _workspace_layout(workspace)
    missing = [path for path in [layout.library_db, layout.embedding_db, layout.config_json] if not path.exists()]
    if missing:
        missing_text = ", ".join(str(path) for path in missing)
        raise ValueError(f"workspace 未初始化或文件缺失: {missing_text}")
    return layout


def _source_to_dict(source: SourceRecord) -> dict[str, Any]:
    return {
        "id": source.id,
        "root_path": source.root_path,
        "label": source.label,
        "enabled": source.enabled,
        "removed_at": source.removed_at,
        "created_at": source.created_at,
        "updated_at": source.updated_at,
    }


def _print_payload(*, args: argparse.Namespace, data: dict[str, Any]) -> None:
    if args.json:
        print(json.dumps({"ok": True, "data": data}, ensure_ascii=False))
        return
    for key, value in data.items():
        if isinstance(value, (dict, list)):
            value_text = json.dumps(value, ensure_ascii=False)
        else:
            value_text = str(value)
        print(f"{key}: {value_text}")


def _cmd_init(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    external_root = _workspace_root(args.external_root) if args.external_root else workspace / "external"
    layout = initialize_workspace(workspace_root=workspace, external_root=external_root)
    _print_payload(
        args=args,
        data={
            "workspace_root": str(layout.workspace_root),
            "hikbox_root": str(layout.hikbox_root),
            "library_db": str(layout.library_db),
            "embedding_db": str(layout.embedding_db),
            "config_json": str(layout.config_json),
            "external_root": str(external_root),
        },
    )
    return 0


def _cmd_source_add(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    service = SourceService(SourceRepository(layout.library_db))
    source = service.add_source(args.root_path, label=args.label)
    _print_payload(args=args, data={"source": _source_to_dict(source)})
    return 0


def _cmd_source_list(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    service = SourceService(SourceRepository(layout.library_db))
    sources = service.list_sources()
    _print_payload(
        args=args,
        data={
            "count": len(sources),
            "sources": [_source_to_dict(item) for item in sources],
        },
    )
    return 0


def _cmd_scan_start_or_resume(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    session_repo = ScanSessionRepository(layout.library_db)
    session_service = ScanSessionService(session_repo)
    start_result = session_service.start_or_resume(run_kind=args.run_kind, triggered_by="manual_cli")

    runtime_defaults = build_scan_runtime_defaults()
    effective_runtime = ScanRuntimeDefaults(
        det_size=args.det_size if args.det_size > 0 else runtime_defaults.det_size,
        batch_size=args.batch_size if args.batch_size > 0 else runtime_defaults.batch_size,
        workers=args.workers if args.workers > 0 else runtime_defaults.workers,
        preview_max_side=args.preview_max_side if args.preview_max_side > 0 else runtime_defaults.preview_max_side,
    )
    output_root = _workspace_root(args.output_root) if args.output_root else _workspace_output_root(layout)

    run_result = ScanExecutionService(
        db_path=layout.library_db,
        output_root=output_root,
    ).run_session(
        scan_session_id=start_result.session_id,
        runtime_defaults=effective_runtime,
    )
    session = session_repo.get_session(start_result.session_id)
    detect_progress = ScanExecutionService(
        db_path=layout.library_db,
        output_root=output_root,
    ).detect_stage_progress(scan_session_id=start_result.session_id)

    _print_payload(
        args=args,
        data={
            "session_id": start_result.session_id,
            "resumed": start_result.resumed,
            "session_status": session.status,
            "assignment_run_id": run_result.assignment_run_id,
            "detect_result": asdict(run_result.detect_result),
            "detect_progress": detect_progress,
            "output_root": str(output_root),
            "new_face_count": run_result.new_face_count,
            "anchor_candidate_face_count": run_result.anchor_candidate_face_count,
            "anchor_attached_face_count": run_result.anchor_attached_face_count,
            "anchor_missed_face_count": run_result.anchor_missed_face_count,
            "anchor_missed_by_person": run_result.anchor_missed_by_person,
            "local_rebuild_count": run_result.local_rebuild_count,
            "fallback_reason": run_result.fallback_reason,
        },
    )
    return 0


def _cmd_scan_status(args: argparse.Namespace) -> int:
    workspace = _workspace_root(args.workspace)
    layout = _require_workspace_initialized(workspace)
    repo = ScanSessionRepository(layout.library_db)
    session = repo.get_session(args.session_id)
    _print_payload(args=args, data={"session": asdict(session)})
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HikBox Pictures 最小 CLI")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="初始化 workspace")
    p_init.add_argument("--workspace", required=True, help="workspace 根目录")
    p_init.add_argument("--external-root", default="", help="外部目录根路径（默认: <workspace>/external）")
    p_init.set_defaults(func=_cmd_init)

    p_source = sub.add_parser("source", help="source 管理")
    source_sub = p_source.add_subparsers(dest="source_command", required=True)
    p_source_add = source_sub.add_parser("add", help="添加 source")
    p_source_add.add_argument("root_path", help="source 根目录（绝对路径）")
    p_source_add.add_argument("--label", default="", help="source 标签")
    p_source_add.add_argument("--workspace", required=True, help="workspace 根目录")
    p_source_add.set_defaults(func=_cmd_source_add)

    p_source_list = source_sub.add_parser("list", help="列出 source")
    p_source_list.add_argument("--workspace", required=True, help="workspace 根目录")
    p_source_list.set_defaults(func=_cmd_source_list)

    p_scan = sub.add_parser("scan", help="扫描命令")
    scan_sub = p_scan.add_subparsers(dest="scan_command", required=True)

    p_scan_start = scan_sub.add_parser("start-or-resume", help="启动或恢复扫描并执行主链路")
    p_scan_start.add_argument("--workspace", required=True, help="workspace 根目录")
    p_scan_start.add_argument(
        "--run-kind",
        default="scan_full",
        choices=["scan_full", "scan_incremental", "scan_resume"],
        help="扫描 run_kind",
    )
    p_scan_start.add_argument(
        "--output-root",
        default="",
        help="运行产物目录（默认: workspace 配置 external_root，缺失时回退到 <workspace>/.hikbox/runtime）",
    )
    p_scan_start.add_argument("--det-size", type=int, default=0, help="检测输入尺寸，<=0 表示使用默认值")
    p_scan_start.add_argument("--batch-size", type=int, default=0, help="detect 批大小，<=0 表示使用默认值")
    p_scan_start.add_argument("--workers", type=int, default=0, help="detect worker 数量，<=0 表示使用默认值")
    p_scan_start.add_argument(
        "--preview-max-side",
        type=int,
        default=0,
        help="上下文图最大边长，<=0 表示使用默认值",
    )
    p_scan_start.set_defaults(func=_cmd_scan_start_or_resume)

    p_scan_status = scan_sub.add_parser("status", help="查看扫描会话状态")
    p_scan_status.add_argument("session_id", type=int, help="会话 id")
    p_scan_status.add_argument("--workspace", required=True, help="workspace 根目录")
    p_scan_status.set_defaults(func=_cmd_scan_status)

    return parser


def cli_entry(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SourceRootPathConflictError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": "SOURCE_ROOT_PATH_CONFLICT", "message": str(exc)}, ensure_ascii=False))
        else:
            print(f"错误: {exc}")
        return 2
    except ScanActiveConflictError as exc:
        if getattr(args, "json", False):
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "SCAN_ACTIVE_CONFLICT",
                        "active_session_id": exc.active_session_id,
                        "message": str(exc),
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(f"错误: {exc}")
        return 4
    except Exception as exc:  # noqa: BLE001
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": "CLI_ERROR", "message": str(exc)}, ensure_ascii=False))
        else:
            print(f"错误: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(cli_entry())
