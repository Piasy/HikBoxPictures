from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.services.runtime import initialize_workspace

ControlHandler = Callable[[argparse.Namespace], int]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hikbox-pictures")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="初始化工作区与数据库")
    p_init.add_argument("--workspace", type=Path, required=True)
    p_init.set_defaults(handler=handle_init)

    p_source = sub.add_parser("source", help="源目录管理")
    source_sub = p_source.add_subparsers(dest="source_command", required=True)
    p_source_add = source_sub.add_parser("add", help="添加源目录")
    p_source_add.add_argument("--workspace", type=Path, required=True)
    p_source_add.add_argument("--name", required=True)
    p_source_add.add_argument("--root-path", type=Path, required=True)
    p_source_add.set_defaults(handler=handle_source_add)

    p_source_list = source_sub.add_parser("list", help="列出源目录")
    p_source_list.add_argument("--workspace", type=Path, required=True)
    p_source_list.set_defaults(handler=handle_source_list)

    p_source_remove = source_sub.add_parser("remove", help="移除源目录")
    p_source_remove.add_argument("--workspace", type=Path, required=True)
    p_source_remove.add_argument("--source-id", type=int, required=True)
    p_source_remove.set_defaults(handler=handle_source_remove)

    p_serve = sub.add_parser("serve", help="启动本地 API 服务")
    p_serve.add_argument("--workspace", type=Path, required=True)
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=7860)
    p_serve.set_defaults(handler=handle_serve)

    p_scan = sub.add_parser("scan", help="扫描控制命令")
    p_scan.add_argument("--workspace", type=Path)
    p_scan.set_defaults(handler=handle_scan, scan_command="start_or_resume")
    scan_sub = p_scan.add_subparsers(dest="scan_command")

    p_scan_status = scan_sub.add_parser("status", help="查看扫描会话状态")
    p_scan_status.add_argument("--workspace", type=Path, required=True)
    p_scan_status.set_defaults(handler=handle_scan_status)

    p_rebuild = sub.add_parser("rebuild-artifacts", help="重建可派生产物")
    p_rebuild.add_argument("--workspace", type=Path, required=True)
    p_rebuild.set_defaults(handler=handle_rebuild_artifacts)

    p_export = sub.add_parser("export", help="导出控制命令")
    export_sub = p_export.add_subparsers(dest="export_command", required=True)
    p_export_run = export_sub.add_parser("run", help="执行导出模板")
    p_export_run.add_argument("--workspace", type=Path, required=True)
    p_export_run.add_argument("--template-id", type=int, required=True)
    p_export_run.set_defaults(handler=handle_export_run)

    p_logs = sub.add_parser("logs", help="日志控制命令")
    logs_sub = p_logs.add_subparsers(dest="logs_command", required=True)
    p_logs_tail = logs_sub.add_parser("tail", help="查看日志")
    p_logs_tail.add_argument("--workspace", type=Path, required=True)
    p_logs_tail.add_argument("--run-kind")
    p_logs_tail.add_argument("--run-id")
    p_logs_tail.add_argument("--limit", type=int, default=50)
    p_logs_tail.set_defaults(handler=handle_logs_tail)

    p_logs_prune = logs_sub.add_parser("prune", help="清理旧日志")
    p_logs_prune.add_argument("--workspace", type=Path, required=True)
    p_logs_prune.add_argument("--days", type=int, default=90)
    p_logs_prune.set_defaults(handler=handle_logs_prune)

    return parser


def _run_with_control_plane(argv: list[str]) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    handler: ControlHandler = args.handler
    return handler(args)


def handle_init(args: argparse.Namespace) -> int:
    paths = initialize_workspace(args.workspace)
    print(f"Workspace initialized: {paths.root}")
    print(f"Database path: {paths.db_path}")
    return 0


def handle_source_add(args: argparse.Namespace) -> int:
    from hikbox_pictures.repositories import ScanRepo, SourceRepo

    root_path = args.root_path.expanduser().resolve()
    if not root_path.exists() or not root_path.is_dir():
        print(f"source add 失败: root-path 不是目录: {root_path}", file=sys.stderr)
        return 2

    paths = initialize_workspace(args.workspace)
    conn = connect_db(paths.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        source_repo = SourceRepo(conn)
        scan_repo = ScanRepo(conn)

        existing = conn.execute(
            """
            SELECT id, name, root_path, active
            FROM library_source
            WHERE root_path = ? AND active = 1
            ORDER BY id DESC
            LIMIT 1
            """,
            (str(root_path),),
        ).fetchone()

        if existing is not None:
            source_id = int(existing["id"])
            attach_session_id: int | None = None
            session = scan_repo.latest_resumable_session()
            if session is not None:
                attach_session_id = int(session["id"])
                scan_repo.attach_sources(attach_session_id, [source_id])
                if session["status"] == "running":
                    scan_repo.mark_session_sources_running(attach_session_id)
            conn.commit()
            print(
                "source "
                f"id={source_id} "
                "status=exists "
                f"name={existing['name']} "
                f"root_path={existing['root_path']} "
                "active=1"
            )
            if attach_session_id is not None:
                print(f"source attached_session_id={attach_session_id}")
            return 0

        source_id = source_repo.add_source(name=str(args.name), root_path=str(root_path), active=True)
        attach_session_id: int | None = None
        session = scan_repo.latest_resumable_session()
        if session is not None:
            attach_session_id = int(session["id"])
            scan_repo.attach_sources(attach_session_id, [source_id])
            if session["status"] == "running":
                scan_repo.mark_session_sources_running(attach_session_id)
        conn.commit()

        print(
            "source "
            f"id={source_id} "
            "status=added "
            f"name={args.name} "
            f"root_path={root_path} "
            "active=1"
        )
        if attach_session_id is not None:
            print(f"source attached_session_id={attach_session_id}")
        return 0
    except Exception as exc:
        conn.rollback()
        print(f"source add 失败: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def handle_source_list(args: argparse.Namespace) -> int:
    from hikbox_pictures.repositories import SourceRepo

    paths = initialize_workspace(args.workspace)
    conn = connect_db(paths.db_path)
    try:
        rows = SourceRepo(conn).list_sources(active=None)
        if not rows:
            print("source none")
            return 0
        for row in rows:
            print(
                "source "
                f"id={row['id']} "
                f"name={row['name']} "
                f"root_path={row['root_path']} "
                f"active={int(row['active'])}"
            )
        return 0
    except Exception as exc:
        print(f"source list 失败: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def handle_source_remove(args: argparse.Namespace) -> int:
    paths = initialize_workspace(args.workspace)
    conn = connect_db(paths.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT id, name, root_path, active
            FROM library_source
            WHERE id = ?
            """,
            (int(args.source_id),),
        ).fetchone()
        if row is None:
            conn.rollback()
            print(f"source remove 失败: source-id 不存在: {int(args.source_id)}", file=sys.stderr)
            return 2

        source_id = int(row["id"])
        if int(row["active"]) == 0:
            conn.commit()
            print(
                "source "
                f"id={source_id} "
                "status=already-removed "
                f"name={row['name']} "
                f"root_path={row['root_path']} "
                "active=0"
            )
            return 0

        conn.execute(
            """
            UPDATE library_source
            SET active = 0, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (source_id,),
        )
        conn.execute(
            """
            UPDATE scan_session_source
            SET status = 'abandoned',
                updated_at = CURRENT_TIMESTAMP
            WHERE library_source_id = ?
              AND status IN ('pending', 'running', 'paused', 'interrupted')
            """,
            (source_id,),
        )
        conn.commit()
        print(
            "source "
            f"id={source_id} "
            "status=removed "
            f"name={row['name']} "
            f"root_path={row['root_path']} "
            "active=0"
        )
        return 0
    except Exception as exc:
        conn.rollback()
        print(f"source remove 失败: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def handle_serve(args: argparse.Namespace) -> int:
    from hikbox_pictures.api.app import create_app
    import uvicorn

    app = create_app(workspace=args.workspace)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def handle_scan(args: argparse.Namespace) -> int:
    from hikbox_pictures.services.scan_orchestrator import ScanOrchestrator

    if args.workspace is None:
        print("scan 需要 --workspace", file=sys.stderr)
        return 2
    paths = initialize_workspace(args.workspace)
    conn = connect_db(paths.db_path)
    try:
        orchestrator = ScanOrchestrator(conn)
        session_id = orchestrator.start_or_resume()
        session = orchestrator.scan_repo.get_session(session_id)
        if session is None:
            print(f"scan session_id={session_id} status=unknown mode=unknown")
            return 0
        print(f"scan session_id={session_id} status={session['status']} mode={session['mode']}")
        return 0
    finally:
        conn.close()


def handle_scan_status(args: argparse.Namespace) -> int:
    from hikbox_pictures.services.scan_orchestrator import ScanOrchestrator

    paths = initialize_workspace(args.workspace)
    conn = connect_db(paths.db_path)
    try:
        status = ScanOrchestrator(conn).get_status()
        print(
            "scan "
            f"session_id={status['session_id']} "
            f"status={status['status']} "
            f"mode={status['mode']}"
        )
        for source in status.get("sources", []):
            if not isinstance(source, dict):
                continue
            print(
                "source "
                f"id={source.get('id')} "
                f"library_source_id={source.get('library_source_id')} "
                f"status={source.get('status')} "
                f"discovered={source.get('discovered_count')} "
                f"metadata_done={source.get('metadata_done_count')} "
                f"faces_done={source.get('faces_done_count')} "
                f"embeddings_done={source.get('embeddings_done_count')} "
                f"assignment_done={source.get('assignment_done_count')}"
            )
        return 0
    finally:
        conn.close()


def handle_rebuild_artifacts(args: argparse.Namespace) -> int:
    from hikbox_pictures.ann import AnnIndexStore
    from hikbox_pictures.repositories.person_repo import PersonRepo
    from hikbox_pictures.services.prototype_service import PrototypeService

    paths = initialize_workspace(args.workspace)
    conn = connect_db(paths.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        ann_store = AnnIndexStore(paths.artifacts_dir / "ann" / "prototype_index.npz")
        prototype_service = PrototypeService(conn, PersonRepo(conn), ann_store)
        rebuilt_count = prototype_service.rebuild_all_person_prototypes(model_key="pipeline-stub-v1")
        indexed_count = prototype_service.rebuild_ann_index_from_active_prototypes(model_key="pipeline-stub-v1")
        conn.commit()
        print(f"ANN 与人物原型重建完成: prototypes={rebuilt_count} indexed={indexed_count}")
        return 0
    except Exception as exc:
        conn.rollback()
        print(f"rebuild-artifacts 失败: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def handle_export_run(args: argparse.Namespace) -> int:
    from hikbox_pictures.services.action_service import ActionService

    paths = initialize_workspace(args.workspace)
    conn = connect_db(paths.db_path)
    try:
        result = ActionService(conn).run_export_template(template_id=int(args.template_id))
        print(
            "export "
            f"template_id={result['template_id']} "
            f"run_id={result['run_id']} "
            f"spec_hash={result['spec_hash']}"
        )
        print(
            "summary "
            f"matched_only={result['matched_only_count']} "
            f"matched_group={result['matched_group_count']} "
            f"exported={result['exported_count']} "
            f"skipped={result['skipped_count']} "
            f"failed={result['failed_count']}"
        )
        return 1 if int(result["failed_count"]) > 0 else 0
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"export run 失败: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def handle_logs_tail(args: argparse.Namespace) -> int:
    from hikbox_pictures.services.observability_service import ObservabilityService

    if int(args.limit) <= 0:
        print("logs tail 的 --limit 必须大于 0", file=sys.stderr)
        return 2

    paths = initialize_workspace(args.workspace)
    conn = connect_db(paths.db_path)
    try:
        rows = ObservabilityService(conn, workspace=paths.root).tail_run_logs(
            run_kind=args.run_kind,
            run_id=args.run_id,
            limit=int(args.limit),
        )
        for row in rows:
            print(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception as exc:
        print(f"logs tail 失败: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def handle_logs_prune(args: argparse.Namespace) -> int:
    from hikbox_pictures.services.observability_service import ObservabilityService

    if int(args.days) <= 0:
        print("logs prune 的 --days 必须大于 0", file=sys.stderr)
        return 2

    paths = initialize_workspace(args.workspace)
    conn = connect_db(paths.db_path)
    try:
        deleted = ObservabilityService(conn, workspace=paths.root).prune_ops_events(days=int(args.days))
        print(f"logs pruned={deleted} days={int(args.days)}")
        return 0
    except Exception as exc:
        print(f"logs prune 失败: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        return 0

    if len(argv) == 0:
        build_parser().print_help()
        return 0

    return _run_with_control_plane(argv)


def cli_entry() -> int:
    return main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(cli_entry())
