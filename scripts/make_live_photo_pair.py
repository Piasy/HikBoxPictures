#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid

try:
    import exiftool
except ImportError:
    exiftool = None


SWIFT_HELPER_PATH = Path(__file__).resolve().with_name("make_live_photo_mov.swift")


def default_output_paths(
    jpg_path: Path,
    mp4_path: Path,
    output_dir: Path | None,
    output_stem: str | None,
) -> tuple[Path, Path]:
    directory = output_dir if output_dir is not None else jpg_path.parent
    stem = output_stem if output_stem is not None else f"{jpg_path.stem}_live"
    return directory / f"{stem}.jpg", directory / f".{stem}.MOV"


def ensure_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"找不到依赖工具：{name}")


def run_swift_helper(
    *,
    still_path: Path,
    mp4_path: Path,
    mov_path: Path,
    asset_id: str,
) -> None:
    ensure_tool("swift")
    if not SWIFT_HELPER_PATH.is_file():
        raise FileNotFoundError(f"找不到 Swift helper：{SWIFT_HELPER_PATH}")
    subprocess.run(
        [
            "swift",
            "-suppress-warnings",
            str(SWIFT_HELPER_PATH),
            str(still_path),
            str(mp4_path),
            str(mov_path),
            asset_id,
        ],
        check=True,
    )


def build_apple_makernote_content_identifier(asset_id: str) -> bytes:
    value = asset_id.encode("ascii") + b"\0"
    return (
        b"Apple iOS\0\0\x01MM\0\x01"
        + b"\0\x11"
        + b"\0\x02"
        + len(value).to_bytes(4, "big")
        + (32).to_bytes(4, "big")
        + b"\0\0\0\0"
        + value
        + b"\0"
    )


def normalize_asset_id(asset_id: str) -> str:
    try:
        parsed = uuid.UUID(asset_id)
    except ValueError as exc:
        raise ValueError(f"Live Photo asset id 必须是 UUID：{asset_id}") from exc
    if parsed.variant != uuid.RFC_4122:
        raise ValueError(f"Live Photo asset id 必须是 RFC 4122 UUID：{asset_id}")
    return str(parsed).upper()


def write_live_photo_still_metadata(still_path: Path, asset_id: str) -> None:
    ensure_tool("exiftool")
    if exiftool is None:
        raise RuntimeError("缺少 Python 依赖 PyExifTool；请先运行 ./scripts/install.sh")

    with tempfile.NamedTemporaryFile(
        prefix="hikbox-live-photo-maker-note-",
        suffix=".bin",
    ) as maker_note:
        maker_note.write(build_apple_makernote_content_identifier(asset_id))
        maker_note.flush()

        with exiftool.ExifToolHelper() as helper:
            helper.execute(
                "-overwrite_original",
                "-P",
                f"-MakerNoteApple<={maker_note.name}",
                str(still_path),
            )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把一组干净 JPG/MP4 转成 HikBox Live Photo 实验用的 JPG/MOV。"
    )
    parser.add_argument("jpg", type=Path, help="输入 JPG 路径")
    parser.add_argument("mp4", type=Path, help="输入 MP4 路径")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="输出目录；默认与 JPG 同目录",
    )
    parser.add_argument(
        "--output-stem",
        default=None,
        help="输出文件名主干；例如 output2 会生成 output2.jpg 和 .output2.MOV",
    )
    parser.add_argument(
        "--asset-id",
        default=None,
        help="Live Photo content identifier；默认自动生成 UUID",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="允许覆盖已存在的输出 JPG/MOV",
    )
    return parser.parse_args(argv)


def validate_inputs(jpg_path: Path, mp4_path: Path) -> None:
    if not jpg_path.is_file():
        raise FileNotFoundError(f"输入 JPG 不存在：{jpg_path}")
    if not mp4_path.is_file():
        raise FileNotFoundError(f"输入 MP4 不存在：{mp4_path}")
    if jpg_path.suffix.lower() not in {".jpg", ".jpeg"}:
        raise ValueError(f"输入图片必须是 JPG/JPEG：{jpg_path}")
    if mp4_path.suffix.lower() != ".mp4":
        raise ValueError(f"输入视频必须是 MP4：{mp4_path}")


def convert_pair(args: argparse.Namespace) -> tuple[Path, Path, str]:
    jpg_path = args.jpg.expanduser().resolve()
    mp4_path = args.mp4.expanduser().resolve()
    validate_inputs(jpg_path, mp4_path)

    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else None
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
    output_stem = args.output_stem
    if output_stem == "":
        raise ValueError("--output-stem 不能为空")
    still_path, mov_path = default_output_paths(jpg_path, mp4_path, output_dir, output_stem)

    if still_path.resolve() == jpg_path or mov_path.resolve() == mp4_path:
        raise ValueError("输出路径不能覆盖输入文件")

    existing = [path for path in (still_path, mov_path) if path.exists()]
    if existing and not args.overwrite:
        paths = ", ".join(str(path) for path in existing)
        raise FileExistsError(f"输出文件已存在；加 --overwrite 才会覆盖：{paths}")
    if args.overwrite:
        for path in existing:
            path.unlink()

    asset_id = normalize_asset_id(args.asset_id) if args.asset_id else str(uuid.uuid4()).upper()
    shutil.copyfile(jpg_path, still_path)
    write_live_photo_still_metadata(still_path, asset_id)
    run_swift_helper(
        still_path=still_path,
        mp4_path=mp4_path,
        mov_path=mov_path,
        asset_id=asset_id,
    )

    os.utime(still_path, (jpg_path.stat().st_atime, jpg_path.stat().st_mtime))
    os.utime(mov_path, (mp4_path.stat().st_atime, mp4_path.stat().st_mtime))
    still_path.chmod(stat.S_IMODE(jpg_path.stat().st_mode))
    mov_path.chmod(stat.S_IMODE(mp4_path.stat().st_mode))

    return still_path, mov_path, asset_id


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
        still_path, mov_path, asset_id = convert_pair(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"asset_id: {asset_id}")
    print(f"jpg: {still_path}")
    print(f"mov: {mov_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
