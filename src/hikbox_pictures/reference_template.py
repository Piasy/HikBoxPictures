from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from statistics import median
from typing import Protocol, Sequence

import numpy as np

from hikbox_pictures.deepface_engine import BBoxTLBR
from hikbox_pictures.models import ReferenceSample, ReferenceTemplate, TemplateMatchResult
from hikbox_pictures.scanner import iter_candidate_photos


class TemplateEngineProtocol(Protocol):
    def detect_faces(self, image_path: Path) -> Sequence[object]:
        ...

    def distance(self, lhs: object, rhs: object) -> float:
        ...


@dataclass(frozen=True)
class ThresholdScanMetrics:
    best_f1_threshold: float
    best_youden_j_threshold: float


class TemplateCalibrationError(RuntimeError):
    pass


def _ensure_finite_scalar(value: float, *, name: str) -> float:
    value = float(value)
    if not bool(np.isfinite(value)):
        raise ValueError(f"{name} 必须是有限值")
    return value


def _ensure_finite_array(values: np.ndarray, *, name: str) -> np.ndarray:
    if not bool(np.all(np.isfinite(values))):
        raise ValueError(f"{name} 必须全部为有限值")
    return values


def _distance(engine: TemplateEngineProtocol, lhs: object, rhs: object) -> float:
    return _ensure_finite_scalar(float(engine.distance(lhs, rhs)), name="distance")


def _sample_embedding_dimension(samples: Sequence[ReferenceSample]) -> int:
    first_embedding = np.asarray(samples[0].embedding, dtype=np.float32)
    return int(first_embedding.reshape(-1).shape[0])


def _validate_external_centroid_embedding(
    centroid_embedding: np.ndarray,
    *,
    samples: Sequence[ReferenceSample],
) -> np.ndarray:
    centroid = np.asarray(centroid_embedding, dtype=np.float32)
    if centroid.ndim != 1:
        raise ValueError("centroid_embedding 必须是 1 维向量")

    expected_dimension = _sample_embedding_dimension(samples)
    if int(centroid.shape[0]) != expected_dimension:
        raise ValueError("centroid_embedding 维度必须与样本 embedding 一致")

    return _ensure_finite_array(centroid, name="centroid_embedding")


def select_template_threshold(
    *,
    override_threshold: float | None,
    fallback_threshold: float | None,
    engine_threshold: float,
) -> float:
    if override_threshold is not None:
        return _ensure_finite_scalar(float(override_threshold), name="match_threshold")
    if fallback_threshold is not None:
        return _ensure_finite_scalar(float(fallback_threshold), name="match_threshold")
    return _ensure_finite_scalar(float(engine_threshold), name="match_threshold")


def _normalize_scores(values: Sequence[float]) -> list[float]:
    if not values:
        return []

    min_value = min(values)
    max_value = max(values)
    if max_value <= min_value:
        return [1.0] * len(values)

    span = max_value - min_value
    return [(value - min_value) / span for value in values]


def _compute_quality_scores(samples: Sequence[ReferenceSample]) -> list[float]:
    area_scores = _normalize_scores([sample.face_area_ratio for sample in samples])
    sharpness_scores = _normalize_scores([sample.sharpness_score for sample in samples])
    return [0.6 * area_score + 0.4 * sharpness_score for area_score, sharpness_score in zip(area_scores, sharpness_scores)]


def _compute_pairwise_center_distances(
    samples: Sequence[ReferenceSample],
    *,
    engine: TemplateEngineProtocol,
) -> list[float]:
    distances: list[float] = []
    for source in samples:
        source_distances = [_distance(engine, source.embedding, target.embedding) for target in samples]
        distances.append(float(median(source_distances)))
    return distances


def _select_kept_indexes(center_distances: Sequence[float]) -> set[int]:
    if len(center_distances) < 5:
        return set(range(len(center_distances)))

    center_median = median(center_distances)
    abs_deviations = [abs(distance - center_median) for distance in center_distances]
    mad = median(abs_deviations)
    threshold = center_median + 2.5 * mad

    kept_indexes = {index for index, distance in enumerate(center_distances) if distance <= threshold}
    if len(kept_indexes) >= 3:
        return kept_indexes

    sorted_indexes = sorted(range(len(center_distances)), key=lambda index: center_distances[index])
    return set(sorted_indexes[: min(3, len(sorted_indexes))])


def _build_centroid_embedding(samples: Sequence[ReferenceSample]) -> np.ndarray:
    stacked_embeddings = np.stack([np.asarray(sample.embedding, dtype=np.float32) for sample in samples], axis=0)
    weights = np.asarray([max(sample.quality_score, 0.0) for sample in samples], dtype=np.float32)
    if float(weights.sum()) <= 0.0:
        centroid = stacked_embeddings.mean(axis=0)
    else:
        centroid = np.average(stacked_embeddings, axis=0, weights=weights)

    centroid = _ensure_finite_array(np.asarray(centroid, dtype=np.float32), name="centroid_embedding")
    norm = _ensure_finite_scalar(float(np.linalg.norm(centroid)), name="centroid_embedding_norm")
    if norm <= 0.0:
        return centroid
    return _ensure_finite_array(np.asarray(centroid / norm, dtype=np.float32), name="centroid_embedding")


def build_reference_template(
    name: str,
    samples: Sequence[ReferenceSample],
    *,
    engine: TemplateEngineProtocol,
    default_threshold: float,
    override_threshold: float | None = None,
    fallback_threshold: float | None = None,
    drop_outliers: bool = False,
    centroid_embedding: np.ndarray | None = None,
) -> ReferenceTemplate:
    if not samples:
        raise ValueError("samples 不能为空")

    quality_scores = _compute_quality_scores(samples)
    center_distances = _compute_pairwise_center_distances(samples, engine=engine)
    if drop_outliers:
        kept_indexes = _select_kept_indexes(center_distances)
    else:
        kept_indexes = set(range(len(samples)))

    rebuilt_samples: list[ReferenceSample] = []
    for index, sample in enumerate(samples):
        keep = index in kept_indexes
        rebuilt_samples.append(
            replace(
                sample,
                quality_score=float(quality_scores[index]),
                center_distance=float(center_distances[index]),
                kept=keep,
                drop_reason=None if keep else "outlier",
            )
        )

    kept_samples = [sample for sample in rebuilt_samples if sample.kept]
    template_centroid = (
        _validate_external_centroid_embedding(centroid_embedding, samples=kept_samples)
        if centroid_embedding is not None
        else _build_centroid_embedding(kept_samples)
    )

    return ReferenceTemplate(
        name=name,
        samples=tuple(rebuilt_samples),
        kept_samples=tuple(kept_samples),
        centroid_embedding=template_centroid,
        match_threshold=select_template_threshold(
            override_threshold=override_threshold,
            fallback_threshold=fallback_threshold,
            engine_threshold=default_threshold,
        ),
        top_k=min(3, len(kept_samples)),
    )


def compute_template_match(
    embedding: object,
    template: ReferenceTemplate,
    *,
    engine: TemplateEngineProtocol,
) -> TemplateMatchResult:
    if not template.kept_samples:
        raise ValueError("kept_samples 不能为空")
    if template.top_k <= 0:
        raise ValueError("top_k 必须大于 0")

    distances = sorted(_distance(engine, embedding, sample.embedding) for sample in template.kept_samples)
    top_k = min(template.top_k, len(distances))
    top_k_distances = distances[:top_k]
    template_distance = _ensure_finite_scalar(float(np.mean(top_k_distances)), name="template_distance")
    centroid_distance = _distance(engine, embedding, template.centroid_embedding)

    return TemplateMatchResult(
        template_distance=template_distance,
        centroid_distance=centroid_distance,
        matched=template_distance <= template.match_threshold,
        top_k_distances=top_k_distances,
    )


def _bbox_from_face(face: object) -> BBoxTLBR:
    bbox = getattr(face, "bbox", None)
    if bbox is None:
        raise ValueError("face 缺少 bbox 字段")
    try:
        top, right, bottom, left = bbox
    except (TypeError, ValueError) as exc:
        raise ValueError("face.bbox 非法") from exc
    return int(top), int(right), int(bottom), int(left)


def build_reference_samples_from_embeddings(
    source_paths: Sequence[Path],
    embeddings: Sequence[object],
    *,
    engine: TemplateEngineProtocol,
) -> list[ReferenceSample]:
    if len(source_paths) != len(embeddings):
        raise ValueError("source_paths 与 embeddings 数量必须一致")

    samples: list[ReferenceSample] = []
    for source_path, embedding in zip(source_paths, embeddings, strict=True):
        faces = engine.detect_faces(source_path)
        if len(faces) != 1:
            raise ValueError(f"参考图片 {source_path} 必须且仅能检测到 1 张人脸")

        face = faces[0]
        bbox = _bbox_from_face(face)
        top, right, bottom, left = bbox
        width = max(0, right - left)
        height = max(0, bottom - top)
        image_size = (max(1, right), max(1, bottom))
        image_area = max(1, image_size[0] * image_size[1])
        face_area = max(0, width * height)

        samples.append(
            ReferenceSample(
                path=source_path,
                embedding=np.asarray(embedding, dtype=np.float32),
                bbox=bbox,
                image_size=image_size,
                face_area_ratio=float(face_area / image_area),
                sharpness_score=1.0,
                quality_score=0.0,
                center_distance=None,
                kept=True,
                drop_reason=None,
            )
        )
    return samples


def compute_best_face_distance_in_directory(
    directory: Path,
    template: ReferenceTemplate,
    *,
    engine,
) -> list[float]:
    scores: list[float] = []
    for candidate in iter_candidate_photos(directory):
        try:
            faces = engine.detect_faces(candidate.path)
        except Exception as exc:
            raise TemplateCalibrationError(f"Failed to decode {candidate.path}: {exc}") from exc
        if not faces:
            continue

        face_scores = [compute_template_match(face.embedding, template, engine=engine).template_distance for face in faces]
        if face_scores:
            scores.append(float(min(face_scores)))
    return scores


def _scan_threshold_candidates(positive_scores: Sequence[float], negative_scores: Sequence[float]) -> list[float]:
    candidates = sorted(set(float(score) for score in [*positive_scores, *negative_scores]))
    if not candidates:
        return [0.0]
    return candidates


def _f1_and_youden(threshold: float, positive_scores: Sequence[float], negative_scores: Sequence[float]) -> tuple[float, float]:
    tp = sum(1 for score in positive_scores if score <= threshold)
    fn = len(positive_scores) - tp
    fp = sum(1 for score in negative_scores if score <= threshold)
    tn = len(negative_scores) - fp

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    if precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)

    tpr = recall
    tnr = tn / (tn + fp) if (tn + fp) > 0 else 0.0
    youden_j = tpr + tnr - 1.0
    return f1, youden_j


def scan_threshold_metrics(
    positive_scores: Sequence[float],
    negative_scores: Sequence[float],
) -> ThresholdScanMetrics:
    candidates = _scan_threshold_candidates(positive_scores, negative_scores)

    best_f1_threshold = candidates[0]
    best_f1_value = float("-inf")
    best_youden_threshold = candidates[0]
    best_youden_value = float("-inf")

    for threshold in candidates:
        f1, youden_j = _f1_and_youden(threshold, positive_scores, negative_scores)
        if f1 > best_f1_value:
            best_f1_value = f1
            best_f1_threshold = threshold
        if youden_j > best_youden_value:
            best_youden_value = youden_j
            best_youden_threshold = threshold

    return ThresholdScanMetrics(
        best_f1_threshold=float(best_f1_threshold),
        best_youden_j_threshold=float(best_youden_threshold),
    )
