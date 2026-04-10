#!/usr/bin/env python3

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageDraw, ImageFont

from hikbox_pictures.deepface_engine import DeepFaceEngine, DeepFaceInitError
from hikbox_pictures.image_io import load_rgb_image
from hikbox_pictures.reference_loader import ReferenceImageError, load_reference_embeddings
from hikbox_pictures.reference_template import (
    build_reference_samples_from_embeddings,
    build_reference_template,
    compute_template_match,
)
from hikbox_pictures.scanner import iter_candidate_photos

ANNOTATION_TEXT_COLOR = (64, 128, 255)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="inspect_distances")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--ref-a-dir", required=True, type=Path)
    parser.add_argument("--ref-b-dir", required=True, type=Path)
    parser.add_argument("--model-name", default="ArcFace")
    parser.add_argument("--detector-backend", default="retinaface")
    parser.add_argument("--distance-metric", default="cosine")
    parser.add_argument("--distance-threshold", type=float)
    parser.add_argument("--distance-threshold-a", type=float)
    parser.add_argument("--distance-threshold-b", type=float)
    parser.add_argument("--align", dest="align", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--annotated-dir", type=Path)
    return parser


def _load_candidate_face_encodings(
    path: Path,
    engine: DeepFaceEngine,
) -> tuple[list[tuple[int, int, int, int]], list[Sequence[float]]]:
    faces = engine.detect_faces(path)
    locations = [face.bbox for face in faces]
    encodings = [face.embedding for face in faces]
    return locations, encodings


def _format_distance(value: float) -> str:
    return f"{value:.4f}"


def _best_joint_distance(matches_a, matches_b):
    joint_distances = [
        max(match_a.template_distance, match_b.template_distance)
        for index_a, match_a in enumerate(matches_a)
        for index_b, match_b in enumerate(matches_b)
        if index_a != index_b and match_a.matched and match_b.matched
    ]
    return min(joint_distances) if joint_distances else None


def _annotated_output_path(candidate_path: Path, *, input_root: Path, annotated_dir: Path) -> Path:
    relative_path = candidate_path.relative_to(input_root)
    return annotated_dir / relative_path.parent / f"{candidate_path.stem}__annotated.png"


def _load_annotation_font(*, size: int) -> Any:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _label_size(draw: ImageDraw.ImageDraw, lines: list[str], font: ImageFont.ImageFont) -> tuple[int, int]:
    widths: list[int] = []
    heights: list[int] = []
    for line in lines:
        left, top, right, bottom = draw.textbbox((0, 0), line, font=font)
        widths.append(right - left)
        heights.append(bottom - top)

    padding = 4
    line_gap = 2
    width = max(widths, default=0) + padding * 2
    height = sum(heights) + line_gap * max(0, len(lines) - 1) + padding * 2
    return width, height


def _draw_label(
    draw: ImageDraw.ImageDraw,
    *,
    font: ImageFont.ImageFont,
    left: int,
    top: int,
    lines: list[str],
    image_width: int,
    image_height: int,
) -> None:
    line_gap = 2
    label_width, label_height = _label_size(draw, lines, font)
    label_left = max(0, min(left, image_width - label_width))
    label_top = top - label_height - 6
    if label_top < 0:
        label_top = min(image_height - label_height, top + 6)
    label_top = max(0, label_top)

    text_top = label_top
    for line in lines:
        draw.text((label_left, text_top), line, fill=ANNOTATION_TEXT_COLOR, font=font)
        _, _, _, bottom = draw.textbbox((0, 0), line, font=font)
        text_top += bottom + line_gap


def _write_annotated_image(
    candidate_path: Path,
    *,
    input_root: Path,
    annotated_dir: Path,
    locations: list[tuple[int, int, int, int]],
    template_distances_a: list[float],
    template_distances_b: list[float],
    centroid_distances_a: list[float],
    centroid_distances_b: list[float],
) -> Path:
    image_array = load_rgb_image(candidate_path)
    image = Image.fromarray(image_array)

    draw = ImageDraw.Draw(image)
    font = _load_annotation_font(size=30)
    line_width = max(2, min(image.size) // 300)

    for index, location in enumerate(locations):
        top, right, bottom, left = location
        draw.rectangle((left, top, right, bottom), outline=(255, 64, 64), width=line_width)
        _draw_label(
            draw,
            font=font,
            left=left,
            top=top,
            lines=[
                f"face[{index}]",
                f"A {_format_distance(template_distances_a[index])}/{_format_distance(centroid_distances_a[index])}",
                f"B {_format_distance(template_distances_b[index])}/{_format_distance(centroid_distances_b[index])}",
            ],
            image_width=image.width,
            image_height=image.height,
        )

    output_path = _annotated_output_path(candidate_path, input_root=input_root, annotated_dir=annotated_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)
    return output_path


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.input.exists():
        print(f"路径不存在: {args.input}", file=sys.stderr)
        return 2
    if not args.input.is_dir():
        print(f"路径不是目录: {args.input}", file=sys.stderr)
        return 2

    for path in (args.ref_a_dir, args.ref_b_dir):
        if not path.exists():
            print(f"路径不存在: {path}", file=sys.stderr)
            return 2
        if not path.is_dir():
            print(f"路径不是目录: {path}", file=sys.stderr)
            return 2
    if args.annotated_dir is not None and args.annotated_dir.exists() and not args.annotated_dir.is_dir():
        print(f"路径不是目录: {args.annotated_dir}", file=sys.stderr)
        return 2

    try:
        engine = DeepFaceEngine.create(
            model_name=args.model_name,
            detector_backend=args.detector_backend,
            distance_metric=args.distance_metric,
            align=args.align,
            distance_threshold=args.distance_threshold,
        )
        ref_a_embeddings, ref_a_paths = load_reference_embeddings(args.ref_a_dir, engine)
        ref_b_embeddings, ref_b_paths = load_reference_embeddings(args.ref_b_dir, engine)
        ref_a_samples = build_reference_samples_from_embeddings(ref_a_paths, ref_a_embeddings, engine=engine)
        ref_b_samples = build_reference_samples_from_embeddings(ref_b_paths, ref_b_embeddings, engine=engine)
        template_a = build_reference_template(
            "A",
            ref_a_samples,
            engine=engine,
            default_threshold=engine.distance_threshold,
            override_threshold=args.distance_threshold_a,
            fallback_threshold=args.distance_threshold,
        )
        template_b = build_reference_template(
            "B",
            ref_b_samples,
            engine=engine,
            default_threshold=engine.distance_threshold,
            override_threshold=args.distance_threshold_b,
            fallback_threshold=args.distance_threshold,
        )
    except (DeepFaceInitError, ReferenceImageError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(f"输入目录: {args.input}")
    print(f"参考图目录 A: {args.ref_a_dir}")
    print(f"参考图目录 B: {args.ref_b_dir}")
    print(
        "运行配置: "
        f"model_name={engine.model_name} "
        f"detector_backend={engine.detector_backend} "
        f"distance_metric={engine.distance_metric} "
        f"align={engine.align} "
        f"distance_threshold={_format_distance(engine.distance_threshold)} "
        f"threshold_source={engine.threshold_source}"
    )
    print(
        "模板配置: "
        f"template_threshold_a={_format_distance(template_a.match_threshold)} "
        f"template_threshold_b={_format_distance(template_b.match_threshold)} "
        f"top_k_a={template_a.top_k} "
        f"top_k_b={template_b.top_k}"
    )
    if args.annotated_dir is not None:
        print(f"标注输出目录: {args.annotated_dir}")

    for candidate in iter_candidate_photos(args.input):
        print()
        print(f"文件: {candidate.path}")
        try:
            locations, encodings = _load_candidate_face_encodings(candidate.path, engine)
        except Exception as exc:
            print(f"  解码失败: {exc}")
            continue

        print(f"  检测到人脸数: {len(encodings)}")
        if not encodings:
            continue

        matches_a = [compute_template_match(encoding, template_a, engine=engine) for encoding in encodings]
        matches_b = [compute_template_match(encoding, template_b, engine=engine) for encoding in encodings]
        joint_distance = _best_joint_distance(matches_a, matches_b)

        template_distances_a = [match.template_distance for match in matches_a]
        template_distances_b = [match.template_distance for match in matches_b]
        centroid_distances_a = [match.centroid_distance for match in matches_a]
        centroid_distances_b = [match.centroid_distance for match in matches_b]

        for index, location in enumerate(locations):
            match_a = matches_a[index]
            match_b = matches_b[index]
            print(
                "  "
                f"face[{index}] location={location} "
                f"template_dist_a={_format_distance(match_a.template_distance)} "
                f"template_dist_b={_format_distance(match_b.template_distance)} "
                f"centroid_dist_a={_format_distance(match_a.centroid_distance)} "
                f"centroid_dist_b={_format_distance(match_b.centroid_distance)} "
                f"match_a={'Y' if match_a.matched else 'N'} "
                f"match_b={'Y' if match_b.matched else 'N'}"
            )

        if joint_distance is not None:
            print(f"  joint_distance={_format_distance(joint_distance)}")

        if args.annotated_dir is not None:
            try:
                output_path = _write_annotated_image(
                    candidate.path,
                    input_root=args.input,
                    annotated_dir=args.annotated_dir,
                    locations=locations,
                    template_distances_a=template_distances_a,
                    template_distances_b=template_distances_b,
                    centroid_distances_a=centroid_distances_a,
                    centroid_distances_b=centroid_distances_b,
                )
                print(f"  标注图: {output_path}")
            except Exception as exc:
                print(f"标注失败: {candidate.path} -> {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
