#!/usr/bin/env python3
"""根据 summary.json 生成人物原图 review 页面。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hikbox_pictures.immich_people_review import write_people_review_html_from_summary


def _path(raw: str) -> Path:
    return Path(raw).expanduser().resolve()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="根据 summary.json 生成人物原图 review 页面")
    parser.add_argument("--summary-json", required=True, help="步骤一生成的 summary.json 路径")
    parser.add_argument("--output-dir", required=True, help="review 输出目录")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    result = write_people_review_html_from_summary(
        summary_json_path=_path(args.summary_json),
        output_dir=_path(args.output_dir),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
