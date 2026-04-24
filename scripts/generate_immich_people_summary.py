#!/usr/bin/env python3
"""扫描图片目录并导出人物识别 summary.json。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

import pillow_heif

from hikbox_pictures.immich_face_single_file import InsightFaceImmichBackend
from hikbox_pictures.immich_people_review import run_people_summary_batch
from hikbox_pictures.immich_people_review import write_people_summary_batched
from hikbox_pictures.immich_people_review import write_people_summary


def _path(raw: str) -> Path:
    return Path(raw).expanduser().resolve()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="导出人物识别 summary.json")
    parser.add_argument("--input-root", required=True, help="图片输入目录")
    parser.add_argument("--summary-json", required=True, help="summary.json 输出路径")
    parser.add_argument("--db-path", default=None, help="可选；SQLite 增量库路径")
    parser.add_argument("--batch-size", type=int, default=32, help="每个子进程批次处理的图片数；<=0 时退回单进程模式")
    parser.add_argument("--model-root", default=".insightface", help="insightface 模型根目录")
    parser.add_argument("--model-name", default="buffalo_l", help="insightface 模型名")
    parser.add_argument("--min-score", type=float, default=0.7, help="RetinaFace 最低置信度")
    parser.add_argument("--max-distance", type=float, default=0.5, help="人脸向量最大距离")
    parser.add_argument("--min-faces", type=int, default=3, help="新建人物所需的最少近邻数")
    parser.add_argument("--worker-batch-json", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--worker-result-json", default=None, help=argparse.SUPPRESS)
    return parser


def _worker_backend(args: argparse.Namespace) -> InsightFaceImmichBackend:
    pillow_heif.register_heif_opener()
    return InsightFaceImmichBackend(
        model_root=_path(args.model_root),
        model_name=str(args.model_name),
        min_score=float(args.min_score),
    )


def _run_worker_batch(args: argparse.Namespace) -> int:
    if not args.worker_batch_json:
        raise ValueError("worker 模式缺少 batch manifest")
    if not args.worker_result_json:
        raise ValueError("worker 模式缺少 result json")
    batch_json = _path(args.worker_batch_json)
    result_json = _path(args.worker_result_json)
    payload = json.loads(batch_json.read_text(encoding="utf-8"))
    image_paths = [_path(str(path)) for path in payload.get("image_paths", [])]
    backend = _worker_backend(args)
    result = run_people_summary_batch(
        input_root=_path(args.input_root),
        image_paths=image_paths,
        backend=backend,
        db_path=_path(args.db_path) if args.db_path else (_path(args.summary_json).parent / "people.sqlite3"),
        summary_json_path=_path(args.summary_json),
        min_score=float(args.min_score),
        max_distance=float(args.max_distance),
        min_faces=int(args.min_faces),
    )
    result_json.parent.mkdir(parents=True, exist_ok=True)
    result_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def _run_batch_subprocess(
    *,
    batch_index: int,
    batch_image_paths: list[Path],
    args: argparse.Namespace,
) -> dict[str, object]:
    summary_json = _path(args.summary_json)
    worker_dir = summary_json.parent / "worker-batches"
    worker_dir.mkdir(parents=True, exist_ok=True)
    batch_manifest = worker_dir / f"batch-{batch_index:04d}.json"
    batch_result = worker_dir / f"batch-{batch_index:04d}-result.json"
    batch_manifest.write_text(
        json.dumps(
            {
                "image_paths": [str(path.expanduser().resolve()) for path in batch_image_paths],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--input-root",
        str(_path(args.input_root)),
        "--summary-json",
        str(summary_json),
        "--db-path",
        str(_path(args.db_path) if args.db_path else (summary_json.parent / "people.sqlite3")),
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
        "--worker-batch-json",
        str(batch_manifest),
        "--worker-result-json",
        str(batch_result),
    ]
    subprocess.run(
        command,
        check=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    return json.loads(batch_result.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.worker_batch_json or args.worker_result_json:
        return _run_worker_batch(args)

    summary_json = _path(args.summary_json)
    effective_db_path = _path(args.db_path) if args.db_path else (summary_json.parent / "people.sqlite3")
    if int(args.batch_size) > 0:
        batch_counter = {"value": 0}

        def batch_runner(batch_image_paths: list[Path]) -> dict[str, object]:
            batch_counter["value"] += 1
            return _run_batch_subprocess(
                batch_index=int(batch_counter["value"]),
                batch_image_paths=batch_image_paths,
                args=args,
            )

        result = write_people_summary_batched(
            input_root=_path(args.input_root),
            summary_json_path=summary_json,
            batch_runner=batch_runner,
            db_path=effective_db_path,
            batch_size=int(args.batch_size),
            min_score=float(args.min_score),
            max_distance=float(args.max_distance),
            min_faces=int(args.min_faces),
        )
    else:
        backend = _worker_backend(args)
        result = write_people_summary(
            input_root=_path(args.input_root),
            summary_json_path=summary_json,
            backend=backend,
            db_path=effective_db_path,
            min_score=float(args.min_score),
            max_distance=float(args.max_distance),
            min_faces=int(args.min_faces),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
