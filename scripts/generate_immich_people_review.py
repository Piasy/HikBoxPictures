#!/usr/bin/env python3
"""扫描图片目录并导出按人物分组的原图 review 页面。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from hikbox_pictures.immich_people_review import merge_people_review_summaries
from hikbox_pictures.immich_people_review import write_people_review_html_from_summary


def _path(raw: str) -> Path:
    return Path(raw).expanduser().resolve()


def _discover_batch_summary_jsons(*, merge_from_dir: Path, output_dir: Path) -> list[Path]:
    summary_json_paths: list[Path] = []
    for child in sorted(merge_from_dir.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_dir():
            continue
        if child.resolve() == output_dir.resolve():
            continue
        summary_json = child / "summary.json"
        if summary_json.exists():
            summary_json_paths.append(summary_json.resolve())
    if not summary_json_paths:
        raise ValueError(f"目录下没有可汇总的批次 summary.json: {merge_from_dir}")
    return summary_json_paths


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="导出按人物分组的原图 review 页面")
    parser.add_argument("--input-root", default=None, help="图片输入目录")
    parser.add_argument("--merge-from-dir", default=None, help="可选；从目录下各批次 summary.json 合并生成汇总 review")
    parser.add_argument("--output-dir", required=True, help="review 输出目录")
    parser.add_argument("--summary-json", default=None, help="可选；中间 summary.json 输出路径，默认写到 output-dir/summary.json")
    parser.add_argument("--db-path", default=None, help="可选；SQLite 增量库路径")
    parser.add_argument("--batch-size", type=int, default=32, help="每个子进程批次处理的图片数；<=0 时退回单进程模式")
    parser.add_argument("--model-root", default=".insightface", help="insightface 模型根目录")
    parser.add_argument("--model-name", default="buffalo_l", help="insightface 模型名")
    parser.add_argument("--min-score", type=float, default=0.7, help="RetinaFace 最低置信度")
    parser.add_argument("--max-distance", type=float, default=0.5, help="人脸向量最大距离")
    parser.add_argument("--min-faces", type=int, default=3, help="新建人物所需的最少近邻数")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if bool(args.input_root) == bool(args.merge_from_dir):
        parser.error("--input-root 与 --merge-from-dir 必须且只能提供一个")

    output_dir = _path(args.output_dir)
    summary_json = _path(args.summary_json) if args.summary_json else output_dir / "summary.json"
    if args.merge_from_dir:
        merge_from_dir = _path(args.merge_from_dir)
        summary_json_paths = _discover_batch_summary_jsons(
            merge_from_dir=merge_from_dir,
            output_dir=output_dir,
        )
        merged_summary = merge_people_review_summaries(
            summary_json_paths=summary_json_paths,
            input_root_label=f"批次汇总目录：{merge_from_dir}",
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        summary_json.write_text(json.dumps(merged_summary, ensure_ascii=False, indent=2), encoding="utf-8")
        result = write_people_review_html_from_summary(
            summary_json_path=summary_json,
            output_dir=output_dir,
        )
        print(
            json.dumps(
                {
                    "merge_from_dir": str(merge_from_dir),
                    "source_summary_count": len(summary_json_paths),
                    "source_summary_jsons": [str(path) for path in summary_json_paths],
                    "summary_json": str(summary_json),
                    **result,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    summary_command = [
        sys.executable,
        str((Path(__file__).resolve().parent / "generate_immich_people_summary.py").resolve()),
        "--input-root",
        str(_path(args.input_root)),
        "--summary-json",
        str(summary_json),
        "--batch-size",
        str(int(args.batch_size)),
        "--model-root",
        str(_path(args.model_root)),
        "--model-name",
        str(args.model_name),
        "--min-score",
        str(float(args.min_score)),
        "--max-distance",
        str(float(args.max_distance)),
        "--min-faces",
        str(int(args.min_faces)),
    ]
    if args.db_path:
        summary_command.extend(["--db-path", str(_path(args.db_path))])
    subprocess.run(summary_command, check=True, cwd=str(Path(__file__).resolve().parent.parent))
    result = write_people_review_html_from_summary(
        summary_json_path=summary_json,
        output_dir=output_dir,
    )
    print(json.dumps({"summary_json": str(summary_json), **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
