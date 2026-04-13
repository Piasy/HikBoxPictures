from __future__ import annotations

from datetime import datetime
from hashlib import sha1
from pathlib import Path
import shutil
import subprocess

from hikbox_pictures.metadata import format_year_month, read_birthtime_datetime
from hikbox_pictures.models import MatchBucket, PhotoEvaluation


def _collision_suffix(source_path: Path) -> str:
    return sha1(str(source_path).encode("utf-8")).hexdigest()[:10]


def build_destination_path(source_path: Path, *, output_root: Path, bucket: MatchBucket, year_month: str) -> Path:
    target_dir = output_root / bucket.value / year_month
    target_dir.mkdir(parents=True, exist_ok=True)

    preferred = target_dir / source_path.name
    if not preferred.exists():
        return preferred

    suffix = _collision_suffix(source_path)
    candidate = target_dir / f"{source_path.stem}__{suffix}{source_path.suffix}"
    if not candidate.exists():
        return candidate

    index = 1
    while True:
        retry = target_dir / f"{source_path.stem}__{suffix}_{index}{source_path.suffix}"
        if not retry.exists():
            return retry
        index += 1


def build_delivery_destination_path(source_path: Path, *, output_root: Path, bucket: str, year_month: str) -> Path:
    if bucket not in {"only", "group"}:
        raise ValueError(f"不支持的导出桶: {bucket}")

    target_dir = output_root / bucket / year_month
    target_dir.mkdir(parents=True, exist_ok=True)

    preferred = target_dir / source_path.name
    if not preferred.exists():
        return preferred

    suffix = _collision_suffix(source_path)
    candidate = target_dir / f"{source_path.stem}__{suffix}{source_path.suffix}"
    if not candidate.exists():
        return candidate

    index = 1
    while True:
        retry = target_dir / f"{source_path.stem}__{suffix}_{index}{source_path.suffix}"
        if not retry.exists():
            return retry
        index += 1


def set_creation_time(source_path: Path, destination_path: Path) -> None:
    birthtime = read_birthtime_datetime(source_path)
    if birthtime is None:
        return

    formatted = birthtime.astimezone().strftime("%m/%d/%Y %H:%M:%S")
    subprocess.run(["/usr/bin/SetFile", "-d", formatted, str(destination_path)], check=False)


def copy_with_metadata(source_path: Path, destination_path: Path) -> Path:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, destination_path)
    set_creation_time(source_path, destination_path)
    return destination_path


def export_match(evaluation: PhotoEvaluation, *, output_root: Path, capture_datetime: datetime) -> list[Path]:
    if evaluation.bucket is None:
        return []

    year_month = format_year_month(capture_datetime)
    copied_paths = [
        copy_with_metadata(
            evaluation.candidate.path,
            build_destination_path(
                evaluation.candidate.path,
                output_root=output_root,
                bucket=evaluation.bucket,
                year_month=year_month,
            ),
        )
    ]

    if evaluation.candidate.live_photo_video is not None:
        try:
            copied_paths.append(
                copy_with_metadata(
                    evaluation.candidate.live_photo_video,
                    build_destination_path(
                        evaluation.candidate.live_photo_video,
                        output_root=output_root,
                        bucket=evaluation.bucket,
                        year_month=year_month,
                    ),
                )
            )
        except OSError:
            pass

    return copied_paths
