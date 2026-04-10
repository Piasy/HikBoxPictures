from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence, TypeAlias

import numpy as np
import numpy.typing as npt

from hikbox_pictures.deepface_engine import DeepFaceEngine
from hikbox_pictures.scanner import SUPPORTED_EXTENSIONS

Embedding: TypeAlias = npt.NDArray[np.float32]


class ReferenceImageError(ValueError):
    pass


class _FaceLike(Protocol):
    embedding: object


class ReferenceFaceEngine(Protocol):
    def detect_faces(self, image_path: Path) -> Sequence[_FaceLike]:
        ...


_CACHED_REFERENCE_ENGINE: ReferenceFaceEngine | None = None


def _to_float32_embedding(raw_embedding: object, image_path: Path) -> Embedding:
    try:
        embedding = np.asarray(raw_embedding, dtype=np.float32)
    except Exception as exc:
        raise ReferenceImageError(
            f"参考图片 {image_path} 的人脸 embedding 无法转换为 float32：{exc}"
        ) from exc

    if embedding.ndim != 1 or embedding.size == 0:
        raise ReferenceImageError(f"参考图片 {image_path} 的人脸 embedding 为空或维度非法")

    return embedding


def _normalize_detected_faces(raw_faces: object, image_path: Path) -> Sequence[_FaceLike]:
    if raw_faces is None:
        raise ReferenceImageError(f"参考图片 {image_path} 的人脸检测结果为空")
    if not isinstance(raw_faces, Sequence):
        raise ReferenceImageError(f"参考图片 {image_path} 的人脸检测结果不是序列")
    if isinstance(raw_faces, (str, bytes, bytearray)):
        raise ReferenceImageError(f"参考图片 {image_path} 的人脸检测结果类型非法")
    return raw_faces


def _get_face_embedding(face: object, image_path: Path) -> object:
    if not hasattr(face, "embedding"):
        raise ReferenceImageError(f"参考图片 {image_path} 的人脸对象缺少 embedding 字段")
    return getattr(face, "embedding")


def _detect_single_face_embedding(image_path: Path, engine: ReferenceFaceEngine) -> Embedding:
    try:
        raw_faces = engine.detect_faces(image_path)
    except Exception as exc:
        raise ReferenceImageError(f"检测参考图片人脸失败：{image_path}，错误：{exc}") from exc

    faces = _normalize_detected_faces(raw_faces, image_path)

    if len(faces) != 1:
        raise ReferenceImageError(
            f"参考图片 {image_path} 必须且仅能检测到 1 张人脸，实际检测到 {len(faces)} 张。"
        )

    raw_embedding = _get_face_embedding(faces[0], image_path)
    return _to_float32_embedding(raw_embedding, image_path)


def _get_cached_reference_engine(image_path: Path) -> ReferenceFaceEngine:
    global _CACHED_REFERENCE_ENGINE
    if _CACHED_REFERENCE_ENGINE is None:
        try:
            _CACHED_REFERENCE_ENGINE = DeepFaceEngine.create()
        except Exception as exc:
            raise ReferenceImageError(f"初始化参考图引擎失败：{image_path}，错误：{exc}") from exc
    return _CACHED_REFERENCE_ENGINE


def load_reference_embeddings(
    ref_dir: Path,
    engine: ReferenceFaceEngine,
) -> tuple[list[Embedding], list[Path]]:
    if not ref_dir.exists():
        raise ReferenceImageError(f"参考目录不存在：{ref_dir}")
    if not ref_dir.is_dir():
        raise ReferenceImageError(f"参考目录不是文件夹：{ref_dir}")

    source_paths = sorted(
        path for path in ref_dir.rglob("*") if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    if not source_paths:
        raise ReferenceImageError(f"在 {ref_dir} 中未找到支持的参考图片。")

    embeddings = [_detect_single_face_embedding(source_path, engine) for source_path in source_paths]
    return embeddings, source_paths


def load_reference_embedding(image_path: Path, engine: ReferenceFaceEngine) -> Embedding:
    return _detect_single_face_embedding(image_path, engine)


def load_reference_encoding(image_path: Path) -> Embedding:
    # 向后兼容旧调用方，内部已切换到 DeepFace，并复用懒加载引擎。
    engine = _get_cached_reference_engine(image_path)
    return load_reference_embedding(image_path, engine)
