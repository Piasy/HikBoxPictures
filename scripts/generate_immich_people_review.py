#!/usr/bin/env python3
"""扫描图片目录并导出按人物分组的原图 review 页面。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pillow_heif

from hikbox_pictures.immich_face_single_file import InsightFaceImmichBackend
from hikbox_pictures.immich_people_review import write_people_review


def _path(raw: str) -> Path:
    return Path(raw).expanduser().resolve()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="导出按人物分组的原图 review 页面")
    parser.add_argument("--input-root", required=True, help="图片输入目录")
    parser.add_argument("--output-dir", required=True, help="review 输出目录")
    parser.add_argument("--summary-json", default=None, help="可选；中间 summary.json 输出路径，默认写到 output-dir/summary.json")
    parser.add_argument("--model-root", default=".insightface", help="insightface 模型根目录")
    parser.add_argument("--model-name", default="buffalo_l", help="insightface 模型名")
    parser.add_argument("--min-score", type=float, default=0.7, help="RetinaFace 最低置信度")
    parser.add_argument("--max-distance", type=float, default=0.5, help="人脸向量最大距离")
    parser.add_argument("--min-faces", type=int, default=3, help="新建人物所需的最少近邻数")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    pillow_heif.register_heif_opener()
    backend = InsightFaceImmichBackend(
        model_root=_path(args.model_root),
        model_name=str(args.model_name),
        min_score=float(args.min_score),
    )
    output_dir = _path(args.output_dir)
    summary_json = _path(args.summary_json) if args.summary_json else output_dir / "summary.json"
    result = write_people_review(
        input_root=_path(args.input_root),
        output_dir=output_dir,
        backend=backend,
        summary_json_path=summary_json,
        min_score=float(args.min_score),
        max_distance=float(args.max_distance),
        min_faces=int(args.min_faces),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
