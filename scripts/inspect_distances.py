#!/usr/bin/env python3

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import face_recognition
from PIL import Image, ImageDraw, ImageFont

from hikbox_pictures.image_io import load_rgb_image
from hikbox_pictures.reference_loader import ReferenceImageError, load_reference_encoding
from hikbox_pictures.scanner import iter_candidate_photos


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="inspect_distances")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--ref-a", required=True, type=Path)
    parser.add_argument("--ref-b", required=True, type=Path)
    parser.add_argument("--tolerance", type=float, default=0.5)
    parser.add_argument("--annotated-dir", type=Path)
    return parser


def _load_candidate_face_encodings(path: Path) -> tuple[list[tuple[int, int, int, int]], list[list[float]]]:
    image = load_rgb_image(path)
    locations = face_recognition.face_locations(image)
    encodings = face_recognition.face_encodings(image, known_face_locations=locations)
    return locations, encodings


def _format_distance(value: float) -> str:
    return f"{value:.4f}"


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False


def _annotated_output_path(candidate_path: Path, *, input_root: Path, annotated_dir: Path) -> Path:
    relative_path = candidate_path.relative_to(input_root)
    return annotated_dir / relative_path.parent / f"{candidate_path.stem}__annotated.png"


def _should_skip_candidate(
    candidate_path: Path,
    *,
    input_root: Path,
    ref_paths: set[Path],
    annotated_dir: Path | None,
) -> bool:
    resolved_candidate_path = candidate_path.resolve()
    if resolved_candidate_path in ref_paths:
        return True
    if annotated_dir is not None and _is_relative_to(resolved_candidate_path, annotated_dir):
        return True

    try:
        relative_path = resolved_candidate_path.relative_to(input_root.resolve())
    except ValueError:
        return False

    return relative_path.parts[:1] == ("output",)


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
    padding = 4
    line_gap = 2
    label_width, label_height = _label_size(draw, lines, font)
    label_left = max(0, min(left, image_width - label_width))
    label_top = top - label_height - 6
    if label_top < 0:
        label_top = min(image_height - label_height, top + 6)
    label_top = max(0, label_top)

    draw.rectangle(
        (label_left, label_top, label_left + label_width, label_top + label_height),
        fill=(0, 0, 0),
        outline=(255, 64, 64),
        width=1,
    )

    text_top = label_top + padding
    for line in lines:
        draw.text((label_left + padding, text_top), line, fill=(255, 255, 255), font=font)
        _, _, _, bottom = draw.textbbox((0, 0), line, font=font)
        text_top += bottom + line_gap


def _write_annotated_image(
    candidate_path: Path,
    *,
    input_root: Path,
    annotated_dir: Path,
    locations: list[tuple[int, int, int, int]],
    distances_a: list[float],
    distances_b: list[float],
) -> Path:
    with Image.open(candidate_path) as source_image:
        image = source_image.convert("RGB")

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
            lines=[f"face[{index}]", f"A {_format_distance(distances_a[index])}", f"B {_format_distance(distances_b[index])}"],
            image_width=image.width,
            image_height=image.height,
        )

    output_path = _annotated_output_path(candidate_path, input_root=input_root, annotated_dir=annotated_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)
    return output_path


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    for path in (args.input, args.ref_a, args.ref_b):
        if not path.exists():
            print(f"路径不存在: {path}", file=sys.stderr)
            return 2

    try:
        ref_a_encoding = load_reference_encoding(args.ref_a)
        ref_b_encoding = load_reference_encoding(args.ref_b)
    except ReferenceImageError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(f"输入目录: {args.input}")
    print(f"参考图 A: {args.ref_a}")
    print(f"参考图 B: {args.ref_b}")
    print(f"匹配阈值: {args.tolerance:.2f}")
    if args.annotated_dir is not None:
        print(f"标注输出目录: {args.annotated_dir}")

    resolved_input_root = args.input.resolve()
    resolved_ref_paths = {args.ref_a.resolve(), args.ref_b.resolve()}
    resolved_annotated_dir = args.annotated_dir.resolve() if args.annotated_dir is not None else None

    for candidate in iter_candidate_photos(args.input):
        if _should_skip_candidate(
            candidate.path,
            input_root=resolved_input_root,
            ref_paths=resolved_ref_paths,
            annotated_dir=resolved_annotated_dir,
        ):
            continue

        print()
        print(f"文件: {candidate.path}")
        try:
            locations, encodings = _load_candidate_face_encodings(candidate.path)
        except Exception as exc:
            print(f"  解码失败: {exc}")
            continue

        print(f"  检测到人脸数: {len(encodings)}")
        if not encodings:
            continue

        distances_a = [float(value) for value in face_recognition.face_distance(encodings, ref_a_encoding)]
        distances_b = [float(value) for value in face_recognition.face_distance(encodings, ref_b_encoding)]

        for index, location in enumerate(locations):
            distance_a = distances_a[index]
            distance_b = distances_b[index]
            print(
                "  "
                f"face[{index}] location={location} "
                f"dist_a={_format_distance(distance_a)} "
                f"dist_b={_format_distance(distance_b)}"
            )

        if args.annotated_dir is not None:
            output_path = _write_annotated_image(
                candidate.path,
                input_root=args.input,
                annotated_dir=args.annotated_dir,
                locations=locations,
                distances_a=distances_a,
                distances_b=distances_b,
            )
            print(f"  标注图: {output_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
