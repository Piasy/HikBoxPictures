from __future__ import annotations

from pathlib import Path
from typing import Any

from hikbox_pictures.insightface_engine import InsightFaceEngine
from hikbox_pictures.scanner import SUPPORTED_EXTENSIONS


class ReferenceImageError(ValueError):
    pass


def load_reference_embeddings(
    ref_dir: Path,
    engine: InsightFaceEngine,
) -> tuple[list[Any], list[Path]]:
    source_paths = sorted(
        path for path in ref_dir.rglob("*") if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    if not source_paths:
        raise ReferenceImageError(f"No supported reference images found in {ref_dir}.")

    embeddings: list[Any] = []
    for source_path in source_paths:
        faces = engine.detect_faces(source_path)
        if len(faces) != 1:
            raise ReferenceImageError(
                f"Reference image {source_path} must contain exactly one face; found {len(faces)}."
            )
        embeddings.append(faces[0].embedding)

    return embeddings, source_paths
