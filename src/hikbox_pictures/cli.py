from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

from hikbox_pictures.deepface_engine import DeepFaceEngine, DeepFaceInitError
from hikbox_pictures.exporter import export_match
from hikbox_pictures.matcher import CandidateDecodeError, evaluate_candidate_photo
from hikbox_pictures.metadata import resolve_capture_datetime
from hikbox_pictures.models import MatchBucket, RunSummary
from hikbox_pictures.reference_loader import ReferenceImageError, load_reference_embeddings
from hikbox_pictures.reference_template import build_reference_samples_from_embeddings, build_reference_template
from hikbox_pictures.scanner import iter_candidate_photos
from hikbox_pictures.services.runtime import initialize_workspace


ControlHandler = Callable[[argparse.Namespace], int]
CONTROL_COMMANDS = {"init", "source", "serve", "scan", "rebuild-artifacts", "export", "logs"}
LEGACY_FLAGS = {
    "--input",
    "--ref-a-dir",
    "--ref-b-dir",
    "--output",
    "--model-name",
    "--detector-backend",
    "--distance-metric",
    "--distance-threshold",
    "--distance-threshold-a",
    "--distance-threshold-b",
    "--align",
    "--no-align",
}


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
    p_scan.add_argument("--workspace", type=Path, required=True)
    p_scan.set_defaults(handler=handle_scan)

    p_rebuild = sub.add_parser("rebuild-artifacts", help="重建可派生产物")
    p_rebuild.add_argument("--workspace", type=Path, required=True)
    p_rebuild.set_defaults(handler=handle_rebuild_artifacts)

    p_export = sub.add_parser("export", help="导出控制命令")
    export_sub = p_export.add_subparsers(dest="export_command", required=True)
    p_export_run = export_sub.add_parser("run", help="执行导出模板")
    p_export_run.add_argument("--workspace", type=Path, required=True)
    p_export_run.add_argument("--template-id", type=int)
    p_export_run.set_defaults(handler=handle_export_run)

    p_logs = sub.add_parser("logs", help="日志控制命令")
    logs_sub = p_logs.add_subparsers(dest="logs_command", required=True)
    p_logs_tail = logs_sub.add_parser("tail", help="查看日志")
    p_logs_tail.add_argument("--workspace", type=Path, required=True)
    p_logs_tail.add_argument("--run-kind")
    p_logs_tail.add_argument("--run-id")
    p_logs_tail.set_defaults(handler=handle_logs_tail)

    p_logs_prune = logs_sub.add_parser("prune", help="清理旧日志")
    p_logs_prune.add_argument("--workspace", type=Path, required=True)
    p_logs_prune.add_argument("--days", type=int, default=90)
    p_logs_prune.set_defaults(handler=handle_logs_prune)

    return parser


def build_legacy_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hikbox-pictures")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--ref-a-dir", required=True, type=Path)
    parser.add_argument("--ref-b-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model-name", default="ArcFace")
    parser.add_argument("--detector-backend", default="retinaface")
    parser.add_argument("--distance-metric", default="cosine")
    parser.add_argument("--distance-threshold", type=float)
    parser.add_argument("--distance-threshold-a", type=float)
    parser.add_argument("--distance-threshold-b", type=float)
    parser.add_argument("--align", dest="align", action=argparse.BooleanOptionalAction, default=True)
    return parser


def _print_summary(summary: RunSummary) -> None:
    print(f"Scanned files: {summary.scanned_files}")
    print(f"only-two matches: {summary.only_two_matches}")
    print(f"group matches: {summary.group_matches}")
    print(f"Skipped decode errors: {summary.skipped_decode_errors}")
    print(f"Skipped no-face photos: {summary.skipped_no_faces}")
    print(f"Missing Live Photo videos: {summary.missing_live_photo_videos}")
    for warning in summary.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)


def _evaluate_with_engine(candidate, person_a_template, person_b_template, engine):
    return evaluate_candidate_photo(
        candidate,
        person_a_template,
        person_b_template,
        engine=engine,
    )


def _build_template(
    name: str,
    ref_dir: Path,
    *,
    engine: DeepFaceEngine,
    fallback_threshold: float | None,
    override_threshold: float | None,
):
    embeddings, source_paths = load_reference_embeddings(ref_dir, engine)
    samples = build_reference_samples_from_embeddings(source_paths, embeddings, engine=engine)
    default_threshold = fallback_threshold if fallback_threshold is not None else engine.distance_threshold
    return build_reference_template(
        name,
        samples,
        engine=engine,
        default_threshold=default_threshold,
        override_threshold=override_threshold,
        fallback_threshold=fallback_threshold,
    )


def _validate_reference_directory(path: Path) -> bool:
    return path.exists() and path.is_dir()


def _is_legacy_invocation(argv: list[str]) -> bool:
    return any(arg in LEGACY_FLAGS for arg in argv)


def _run_with_control_plane(argv: list[str]) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    handler: ControlHandler = args.handler
    return handler(args)


def _run_legacy_matching(argv: list[str]) -> int:
    parser = build_legacy_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    if not args.input.exists():
        print(f"Path does not exist: {args.input}", file=sys.stderr)
        return 2

    for ref_dir in (args.ref_a_dir, args.ref_b_dir):
        if not _validate_reference_directory(ref_dir):
            print(f"Reference path is not a directory: {ref_dir}", file=sys.stderr)
            return 2
    args.output.mkdir(parents=True, exist_ok=True)

    try:
        engine = DeepFaceEngine.create(
            model_name=args.model_name,
            detector_backend=args.detector_backend,
            distance_metric=args.distance_metric,
            align=args.align,
            distance_threshold=args.distance_threshold,
        )
        person_a_template = _build_template(
            "A",
            args.ref_a_dir,
            engine=engine,
            fallback_threshold=args.distance_threshold,
            override_threshold=args.distance_threshold_a,
        )
        person_b_template = _build_template(
            "B",
            args.ref_b_dir,
            engine=engine,
            fallback_threshold=args.distance_threshold,
            override_threshold=args.distance_threshold_b,
        )
    except (DeepFaceInitError, ReferenceImageError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    summary = RunSummary()
    for candidate in iter_candidate_photos(args.input):
        summary.scanned_files += 1
        try:
            evaluation = _evaluate_with_engine(candidate, person_a_template, person_b_template, engine)
        except CandidateDecodeError as exc:
            summary.skipped_decode_errors += 1
            summary.warnings.append(str(exc))
            continue

        if evaluation.detected_face_count == 0:
            summary.skipped_no_faces += 1
            continue
        if evaluation.bucket is None:
            continue

        capture_datetime = resolve_capture_datetime(candidate.path)
        export_match(evaluation, output_root=args.output, capture_datetime=capture_datetime)
        if evaluation.bucket is MatchBucket.ONLY_TWO:
            summary.only_two_matches += 1
        else:
            summary.group_matches += 1
        if candidate.path.suffix.lower() == ".heic" and candidate.live_photo_video is None:
            summary.missing_live_photo_videos += 1
            summary.warnings.append(f"Missing Live Photo MOV for {candidate.path}")

    _print_summary(summary)
    return 0


def handle_init(args: argparse.Namespace) -> int:
    paths = initialize_workspace(args.workspace)
    print(f"Workspace initialized: {paths.root}")
    print(f"Database path: {paths.db_path}")
    return 0


def _not_implemented(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


def handle_source_add(args: argparse.Namespace) -> int:
    return _not_implemented(f"source add 未实现: workspace={args.workspace} name={args.name} root_path={args.root_path}")


def handle_source_list(args: argparse.Namespace) -> int:
    return _not_implemented(f"source list 未实现: workspace={args.workspace}")


def handle_source_remove(args: argparse.Namespace) -> int:
    return _not_implemented(f"source remove 未实现: workspace={args.workspace} source_id={args.source_id}")


def handle_serve(args: argparse.Namespace) -> int:
    from hikbox_pictures.api.app import create_app
    import uvicorn

    app = create_app(workspace=args.workspace)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def handle_scan(args: argparse.Namespace) -> int:
    return _not_implemented(f"scan 未实现: workspace={args.workspace}")


def handle_rebuild_artifacts(args: argparse.Namespace) -> int:
    return _not_implemented(f"rebuild-artifacts 未实现: workspace={args.workspace}")


def handle_export_run(args: argparse.Namespace) -> int:
    return _not_implemented(f"export run 未实现: workspace={args.workspace} template_id={args.template_id}")


def handle_logs_tail(args: argparse.Namespace) -> int:
    return _not_implemented(f"logs tail 未实现: workspace={args.workspace} run_kind={args.run_kind} run_id={args.run_id}")


def handle_logs_prune(args: argparse.Namespace) -> int:
    return _not_implemented(f"logs prune 未实现: workspace={args.workspace} days={args.days}")


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        return 0

    if len(argv) == 0:
        build_parser().print_help()
        return 0

    first = argv[0]
    if first in CONTROL_COMMANDS or first in {"-h", "--help"}:
        return _run_with_control_plane(argv)
    if _is_legacy_invocation(argv):
        return _run_legacy_matching(argv)
    return _run_with_control_plane(argv)


def cli_entry() -> int:
    return main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(cli_entry())
