from __future__ import annotations

import argparse
import sys
from pathlib import Path

from hikbox_pictures.deepface_engine import DeepFaceEngine, DeepFaceInitError
from hikbox_pictures.exporter import export_match
from hikbox_pictures.matcher import CandidateDecodeError, evaluate_candidate_photo
from hikbox_pictures.metadata import resolve_capture_datetime
from hikbox_pictures.models import MatchBucket, RunSummary
from hikbox_pictures.reference_loader import ReferenceImageError, load_reference_embeddings
from hikbox_pictures.reference_template import build_reference_samples_from_embeddings, build_reference_template
from hikbox_pictures.scanner import iter_candidate_photos


def build_parser() -> argparse.ArgumentParser:
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


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        return 0

    args = build_parser().parse_args(argv)
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


def cli_entry() -> int:
    return main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(cli_entry())
