#!/usr/bin/env python3

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterator

import face_recognition
import numpy as np
from PIL import Image

from hikbox_pictures.image_io import load_rgb_image
from hikbox_pictures.scanner import SUPPORTED_EXTENSIONS, iter_candidate_photos

DEFAULT_MARGIN_SCALE = 2.0
DEFAULT_OUTPUT_SIZE = 512


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="extract_faces",
        description="递归扫描图片目录，裁剪其中所有人脸并统一输出为 512x512 PNG。",
    )
    parser.add_argument("--input", required=True, type=Path, help="输入图片目录，或单张图片文件")
    parser.add_argument("--output", required=True, type=Path, help="输出目录")
    parser.add_argument(
        "--margin-scale",
        type=float,
        default=DEFAULT_MARGIN_SCALE,
        help="相对人脸框的外扩倍数，默认 2.0，值越大保留的四周内容越多",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=DEFAULT_OUTPUT_SIZE,
        help="输出图片边长，默认 512",
    )
    return parser


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        path.relative_to(other)
        return True
    except ValueError:
        return False


def _iter_image_paths(input_path: Path, output_path: Path) -> Iterator[Path]:
    resolved_output_path = output_path.resolve()

    if input_path.is_file():
        if input_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"不支持的图片格式: {input_path}")
        yield input_path
        return

    resolved_input_path = input_path.resolve()
    for candidate in iter_candidate_photos(input_path):
        resolved_candidate_path = candidate.path.resolve()
        if _is_relative_to(resolved_candidate_path, resolved_output_path):
            continue
        if not _is_relative_to(resolved_candidate_path, resolved_input_path):
            continue
        yield candidate.path


def _square_crop_box(
    location: tuple[int, int, int, int],
    *,
    margin_scale: float,
) -> tuple[int, int, int, int]:
    top, right, bottom, left = location
    width = max(1, right - left)
    height = max(1, bottom - top)
    crop_size = max(1, int(round(max(width, height) * margin_scale)))

    center_x = (left + right) / 2
    center_y = (top + bottom) / 2
    crop_left = int(round(center_x - crop_size / 2))
    crop_top = int(round(center_y - crop_size / 2))
    crop_right = crop_left + crop_size
    crop_bottom = crop_top + crop_size
    return crop_top, crop_right, crop_bottom, crop_left


def _crop_with_edge_padding(
    image: np.ndarray,
    box: tuple[int, int, int, int],
) -> np.ndarray:
    top, right, bottom, left = box
    height, width = image.shape[:2]

    pad_top = max(0, -top)
    pad_right = max(0, right - width)
    pad_bottom = max(0, bottom - height)
    pad_left = max(0, -left)

    if pad_top or pad_right or pad_bottom or pad_left:
        # 越界区域直接补黑，允许输出出现黑边。
        image = np.pad(
            image,
            ((pad_top, pad_bottom), (pad_left, pad_right), (0, 0)),
            mode="constant",
            constant_values=0,
        )
        top += pad_top
        bottom += pad_top
        left += pad_left
        right += pad_left

    return image[top:bottom, left:right]


def _build_output_path(
    source_path: Path,
    *,
    input_path: Path,
    output_path: Path,
    face_index: int,
) -> Path:
    if input_path.is_file():
        relative_parent = Path()
    else:
        relative_parent = source_path.relative_to(input_path).parent

    return output_path / relative_parent / f"{source_path.stem}__face_{face_index:02d}.png"


def _save_face_crop(
    image: np.ndarray,
    location: tuple[int, int, int, int],
    *,
    margin_scale: float,
    size: int,
    output_path: Path,
) -> None:
    crop_box = _square_crop_box(location, margin_scale=margin_scale)
    face_crop = _crop_with_edge_padding(image, crop_box)
    output_image = Image.fromarray(face_crop).resize((size, size), resample=Image.Resampling.LANCZOS)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_image.save(output_path, format="PNG")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.input.exists():
        print(f"输入路径不存在: {args.input}", file=sys.stderr)
        return 2
    if args.margin_scale < 1.0:
        print("--margin-scale 不能小于 1.0", file=sys.stderr)
        return 2
    if args.size <= 0:
        print("--size 必须大于 0", file=sys.stderr)
        return 2

    args.output.mkdir(parents=True, exist_ok=True)

    scanned_files = 0
    no_face_files = 0
    decode_errors = 0
    written_faces = 0

    print(f"输入路径: {args.input}")
    print(f"输出目录: {args.output}")
    print(f"外扩倍数: {args.margin_scale:.2f}")
    print(f"输出尺寸: {args.size}x{args.size}")

    try:
        image_paths = _iter_image_paths(args.input, args.output)
        for image_path in image_paths:
            scanned_files += 1
            try:
                image = load_rgb_image(image_path)
            except Exception as exc:
                decode_errors += 1
                print(f"解码失败: {image_path} ({exc})", file=sys.stderr)
                continue

            locations = face_recognition.face_locations(image)
            if not locations:
                no_face_files += 1
                continue

            for face_index, location in enumerate(locations, start=1):
                output_path = _build_output_path(
                    image_path,
                    input_path=args.input,
                    output_path=args.output,
                    face_index=face_index,
                )
                _save_face_crop(
                    image,
                    location,
                    margin_scale=args.margin_scale,
                    size=args.size,
                    output_path=output_path,
                )
                written_faces += 1
                print(f"输出人脸: {output_path}")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print()
    print(f"扫描图片数: {scanned_files}")
    print(f"输出人脸数: {written_faces}")
    print(f"无人脸图片数: {no_face_files}")
    print(f"解码失败数: {decode_errors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
