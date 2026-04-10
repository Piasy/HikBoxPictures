#!/usr/bin/env python3

from __future__ import annotations

import argparse
from pathlib import Path

from hikbox_pictures.deepface_engine import DeepFaceEngine
from hikbox_pictures.reference_loader import load_reference_embeddings
from hikbox_pictures.reference_template import (
    build_reference_samples_from_embeddings,
    build_reference_template,
    compute_best_face_distance_in_directory,
    scan_threshold_metrics,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="calibrate_thresholds")
    parser.add_argument("--ref-dir", required=True, type=Path)
    parser.add_argument("--positive-dir", required=True, type=Path)
    parser.add_argument("--negative-dir", required=True, type=Path)
    parser.add_argument("--model-name", default="ArcFace")
    parser.add_argument("--detector-backend", default="retinaface")
    parser.add_argument("--distance-metric", default="cosine")
    parser.add_argument("--distance-threshold", type=float)
    parser.add_argument("--align", dest="align", action=argparse.BooleanOptionalAction, default=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    engine = DeepFaceEngine.create(
        model_name=args.model_name,
        detector_backend=args.detector_backend,
        distance_metric=args.distance_metric,
        align=args.align,
        distance_threshold=args.distance_threshold,
    )
    embeddings, source_paths = load_reference_embeddings(args.ref_dir, engine)
    samples = build_reference_samples_from_embeddings(source_paths, embeddings, engine=engine)
    template = build_reference_template(
        "target",
        samples,
        engine=engine,
        default_threshold=engine.distance_threshold,
        fallback_threshold=args.distance_threshold,
    )
    positive_scores = compute_best_face_distance_in_directory(args.positive_dir, template, engine=engine)
    negative_scores = compute_best_face_distance_in_directory(args.negative_dir, template, engine=engine)
    metrics = scan_threshold_metrics(positive_scores, negative_scores)
    print(f"best_f1_threshold={metrics.best_f1_threshold:.4f}")
    print(f"best_youden_j_threshold={metrics.best_youden_j_threshold:.4f}")
    print("建议：将结果传给 --distance-threshold-a 或 --distance-threshold-b")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
