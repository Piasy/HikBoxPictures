from __future__ import annotations

import argparse
import sys
from pathlib import Path

from hikbox_pictures.exporter import export_match
from hikbox_pictures.insightface_engine import InsightFaceEngine, InsightFaceInitError
from hikbox_pictures.matcher import CandidateDecodeError, evaluate_candidate_photo
from hikbox_pictures.metadata import resolve_capture_datetime
from hikbox_pictures.models import MatchBucket, RunSummary
from hikbox_pictures.reference_loader import ReferenceImageError, load_reference_embeddings
from hikbox_pictures.scanner import iter_candidate_photos


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hikbox-pictures")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--ref-a-dir", required=True, type=Path)
    parser.add_argument("--ref-b-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
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


def _evaluate_with_engine(candidate, person_a_embeddings, person_b_embeddings, engine):
    try:
        return evaluate_candidate_photo(
            candidate,
            person_a_embeddings,
            person_b_embeddings,
            engine=engine,
        )
    except TypeError:
        return evaluate_candidate_photo(candidate, person_a_embeddings, person_b_embeddings)


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
        engine = InsightFaceEngine.create()
        person_a_embeddings, _ = load_reference_embeddings(args.ref_a_dir, engine)
        person_b_embeddings, _ = load_reference_embeddings(args.ref_b_dir, engine)
    except (InsightFaceInitError, ReferenceImageError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    summary = RunSummary()
    for candidate in iter_candidate_photos(args.input):
        summary.scanned_files += 1
        try:
            evaluation = _evaluate_with_engine(candidate, person_a_embeddings, person_b_embeddings, engine)
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
