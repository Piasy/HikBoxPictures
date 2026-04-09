from __future__ import annotations

from pathlib import Path
from typing import TypeAlias

from hikbox_pictures.insightface_engine import InsightFaceEngine
from hikbox_pictures.scanner import SUPPORTED_EXTENSIONS

Embedding: TypeAlias = list[float]


class ReferenceImageError(ValueError):
    pass


_CACHED_REFERENCE_ENGINE: InsightFaceEngine | None = None


def _detect_single_face_embedding(image_path: Path, engine: InsightFaceEngine) -> Embedding:
    try:
        faces = engine.detect_faces(image_path)
    except Exception as exc:
        raise ReferenceImageError(f"Failed to detect faces in reference image {image_path}: {exc}") from exc
    if len(faces) != 1:
        raise ReferenceImageError(
            f"Reference image {image_path} must contain exactly one face; found {len(faces)}."
        )
    return faces[0].embedding


def _get_cached_reference_engine(image_path: Path) -> InsightFaceEngine:
    global _CACHED_REFERENCE_ENGINE
    if _CACHED_REFERENCE_ENGINE is None:
        try:
            _CACHED_REFERENCE_ENGINE = InsightFaceEngine.create()
        except Exception as exc:
            raise ReferenceImageError(f"Failed to initialize reference engine for {image_path}: {exc}") from exc
    return _CACHED_REFERENCE_ENGINE


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
    # 向后兼容旧调用方，内部已切换到 InsightFace，并复用懒加载引擎。
    engine = _get_cached_reference_engine(image_path)
    return load_reference_embedding(image_path, engine)
