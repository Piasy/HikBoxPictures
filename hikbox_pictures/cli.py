from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from hikbox_pictures.product.serve import ServeStartError
from hikbox_pictures.product.serve import serve_workspace
from hikbox_pictures.product.scan import ScanStartError
from hikbox_pictures.product.scan import start_scan
from hikbox_pictures.product.sources import SourceRegistryError
from hikbox_pictures.product.sources import WorkspaceAccessError
from hikbox_pictures.product.sources import add_source
from hikbox_pictures.product.sources import list_sources
from hikbox_pictures.product.workspace_init import WorkspaceInitializationError
from hikbox_pictures.product.workspace_init import initialize_workspace


CLI_PROGRAM = "hikbox-pictures"


class HikboxPicturesArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"参数错误: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = HikboxPicturesArgumentParser(prog=CLI_PROGRAM)
    subparsers = parser.add_subparsers(dest="command")
    subparsers.required = True

    init_parser = subparsers.add_parser("init", prog=f"{CLI_PROGRAM} init")
    init_parser.add_argument("--workspace", required=True, help="工作区目录")
    init_parser.add_argument("--external-root", required=True, help="外部产物目录")

    source_parser = subparsers.add_parser("source", prog=f"{CLI_PROGRAM} source")
    source_subparsers = source_parser.add_subparsers(dest="source_command")
    source_subparsers.required = True

    source_add_parser = source_subparsers.add_parser("add", prog=f"{CLI_PROGRAM} source add")
    source_add_parser.add_argument("--workspace", required=True, help="工作区目录")
    source_add_parser.add_argument("source_path", help="源目录")
    source_add_parser.add_argument("--label", required=True, help="源目录标签")

    source_list_parser = source_subparsers.add_parser("list", prog=f"{CLI_PROGRAM} source list")
    source_list_parser.add_argument("--workspace", required=True, help="工作区目录")

    scan_parser = subparsers.add_parser("scan", prog=f"{CLI_PROGRAM} scan")
    scan_subparsers = scan_parser.add_subparsers(dest="scan_command")
    scan_subparsers.required = True

    scan_start_parser = scan_subparsers.add_parser("start", prog=f"{CLI_PROGRAM} scan start")
    scan_start_parser.add_argument("--workspace", required=True, help="工作区目录")
    scan_start_parser.add_argument(
        "--batch-size",
        default=200,
        type=_positive_int,
        help="每批处理的照片数量，必须为正整数，默认 200。",
    )

    serve_parser = subparsers.add_parser("serve", prog=f"{CLI_PROGRAM} serve")
    serve_parser.add_argument("--workspace", required=True, help="工作区目录")
    serve_parser.add_argument(
        "--port",
        default=8000,
        type=_tcp_port,
        help="监听端口，默认 8000。",
    )
    serve_parser.add_argument(
        "--person-detail-page-size",
        default=200,
        type=_positive_person_detail_page_size,
        help="人物详情页分页大小，必须为正整数，默认 200。",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "init":
        try:
            initialize_workspace(
                workspace=Path(args.workspace),
                external_root=Path(args.external_root),
                command_args=list(argv) if argv is not None else sys.argv[1:],
            )
        except WorkspaceInitializationError as exc:
            print(f"初始化失败: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.command == "source" and args.source_command == "add":
        try:
            add_source(
                workspace=Path(args.workspace),
                source_path=Path(args.source_path),
                label=args.label,
                command_args=list(argv) if argv is not None else sys.argv[1:],
            )
        except (WorkspaceAccessError, SourceRegistryError) as exc:
            print(f"source add 失败: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.command == "source" and args.source_command == "list":
        try:
            payload = {"sources": list_sources(workspace=Path(args.workspace))}
        except (WorkspaceAccessError, SourceRegistryError) as exc:
            print(f"source list 失败: {exc}", file=sys.stderr)
            return 1
        sys.stdout.write(json.dumps(payload, ensure_ascii=False))
        return 0

    if args.command == "scan" and args.scan_command == "start":
        try:
            start_scan(
                workspace=Path(args.workspace),
                batch_size=int(args.batch_size),
                command_args=list(argv) if argv is not None else sys.argv[1:],
            )
        except (WorkspaceAccessError, ScanStartError) as exc:
            print(f"scan start 失败: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.command == "serve":
        try:
            serve_workspace(
                workspace=Path(args.workspace),
                port=int(args.port),
                person_detail_page_size=int(args.person_detail_page_size),
            )
        except (WorkspaceAccessError, ServeStartError) as exc:
            print(f"serve 失败: {exc}", file=sys.stderr)
            return 1
        return 0

    parser.print_usage(sys.stderr)
    print("参数错误: 不支持的命令。", file=sys.stderr)
    return 2


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--batch-size 必须是正整数。") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("--batch-size 必须是正整数。")
    return value


def _positive_person_detail_page_size(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--person-detail-page-size 必须是正整数。") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("--person-detail-page-size 必须是正整数。")
    return value


def _tcp_port(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--port 必须是 1-65535 之间的整数。") from exc
    if value < 1 or value > 65535:
        raise argparse.ArgumentTypeError("--port 必须是 1-65535 之间的整数。")
    return value
