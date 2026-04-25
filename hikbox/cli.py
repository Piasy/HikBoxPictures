from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from hikbox_pictures.product.sources import WorkspaceAccessError
from hikbox_pictures.product.sources import add_source
from hikbox_pictures.product.sources import list_sources
from hikbox_pictures.product.sources import SourceRegistryError
from hikbox_pictures.product.workspace_init import WorkspaceInitializationError
from hikbox_pictures.product.workspace_init import initialize_workspace
import json


class HikboxArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"参数错误: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = HikboxArgumentParser(prog="hikbox")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.required = True

    init_parser = subparsers.add_parser("init", prog="hikbox init")
    init_parser.add_argument("--workspace", required=True, help="工作区目录")
    init_parser.add_argument("--external-root", required=True, help="外部产物目录")

    source_parser = subparsers.add_parser("source", prog="hikbox source")
    source_subparsers = source_parser.add_subparsers(dest="source_command")
    source_subparsers.required = True

    source_add_parser = source_subparsers.add_parser("add", prog="hikbox source add")
    source_add_parser.add_argument("--workspace", required=True, help="工作区目录")
    source_add_parser.add_argument("source_path", help="源目录")
    source_add_parser.add_argument("--label", required=True, help="源目录标签")

    source_list_parser = source_subparsers.add_parser("list", prog="hikbox source list")
    source_list_parser.add_argument("--workspace", required=True, help="工作区目录")

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

    parser.print_usage(sys.stderr)
    print("参数错误: 不支持的命令。", file=sys.stderr)
    return 2
