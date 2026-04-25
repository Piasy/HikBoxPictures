from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from hikbox_pictures.product.workspace_init import WorkspaceInitializationError
from hikbox_pictures.product.workspace_init import initialize_workspace


class HikboxArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"参数错误: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = HikboxArgumentParser(prog="hikbox")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", prog="hikbox init")
    init_parser.add_argument("--workspace", required=True, help="工作区目录")
    init_parser.add_argument("--external-root", required=True, help="外部产物目录")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command != "init":
        parser.print_usage(sys.stderr)
        print("参数错误: 目前只支持 hikbox init。", file=sys.stderr)
        return 2

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
