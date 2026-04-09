from __future__ import annotations

from pathlib import Path
from typing import TypeAlias

from hikbox_pictures.insightface_engine import InsightFaceEngine
from hikbox_pictures.scanner import SUPPORTED_EXTENSIONS

Embedding: TypeAlias = list[float]


class ReferenceImageError(ValueError):
    pass


def _detect_single_face_embedding(image_path: Path, engine: InsightFaceEngine) -> Embedding:
    try:
        faces = engine.detect_faces(image_path)
    except Exception as exc:  # pragma: no cover - 防御式兜底，行为由调用方测试覆盖
        raise ReferenceImageError(f"Failed to detect faces in reference image {image_path}: {exc}") from exc
    if len(faces) != 1:
        raise ReferenceImageError(
            f"Reference image {image_path} must contain exactly one face; found {len(faces)}."
        )
    return faces[0].embedding


def load_reference_embeddings(
    ref_dir: Path,
    engine: InsightFaceEngine,
) -> tuple[list[Embedding], list[Path]]:
    source_paths = sorted(
        path for path in ref_dir.rglob("*") if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    if not source_paths:
        raise ReferenceImageError(f"No supported reference images found in {ref_dir}.")

    embeddings = [_detect_single_face_embedding(source_path, engine) for source_path in source_paths]
    return embeddings, source_paths


def load_reference_embedding(image_path: Path, engine: InsightFaceEngine) -> Embedding:
    return _detect_single_face_embedding(image_path, engine)


def load_reference_encoding(image_path: Path) -> Embedding:
    # 向后兼容旧调用方，内部已切换到 InsightFace。
    engine = InsightFaceEngine.create()
    return load_reference_embedding(image_path, engine)
