from __future__ import annotations

from dataclasses import replace
from statistics import median
from typing import Protocol, Sequence

import numpy as np

from hikbox_pictures.models import ReferenceSample, ReferenceTemplate, TemplateMatchResult


class TemplateEngineProtocol(Protocol):
    def distance(self, lhs: object, rhs: object) -> float:
        ...


def _distance(engine: TemplateEngineProtocol, lhs: object, rhs: object) -> float:
    return float(engine.distance(lhs, rhs))


def select_template_threshold(
    *,
    override_threshold: float | None,
    fallback_threshold: float | None,
    engine_threshold: float,
) -> float:
    if override_threshold is not None:
        return float(override_threshold)
    if fallback_threshold is not None:
        return float(fallback_threshold)
    return float(engine_threshold)


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
    return np.asarray(centroid, dtype=np.float32)


def build_reference_template(
    name: str,
    samples: Sequence[ReferenceSample],
    *,
    engine: TemplateEngineProtocol,
    default_threshold: float,
    centroid_embedding: np.ndarray | None = None,
) -> ReferenceTemplate:
    if not samples:
        raise ValueError("samples 不能为空")

    quality_scores = _compute_quality_scores(samples)
    center_distances = _compute_pairwise_center_distances(samples, engine=engine)
    kept_indexes = _select_kept_indexes(center_distances)

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
        np.asarray(centroid_embedding, dtype=np.float32)
        if centroid_embedding is not None
        else _build_centroid_embedding(kept_samples)
    )

    return ReferenceTemplate(
        name=name,
        samples=tuple(rebuilt_samples),
        kept_samples=tuple(kept_samples),
        centroid_embedding=template_centroid,
        match_threshold=float(default_threshold),
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
    template_distance = float(np.mean(top_k_distances))
    centroid_distance = _distance(engine, embedding, template.centroid_embedding)

    return TemplateMatchResult(
        template_distance=template_distance,
        centroid_distance=centroid_distance,
        matched=template_distance <= template.match_threshold,
        top_k_distances=top_k_distances,
    )
