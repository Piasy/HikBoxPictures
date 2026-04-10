# HikBox Pictures Template Matching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 DeepFace 版本的双人检索工具上实现参考图模板化、模板匹配、A/B 独立阈值与阈值标定脚本，提升特定人物检索稳定性，同时保持 `only-two` / `group` 导出语义不变。

**Architecture:** 新增 `reference_template.py` 负责参考图质量评分、离群清洗、模板聚合与模板分数计算；`matcher.py` 从“裸 embedding 列表 + min-distance”切到“A/B 模板 + top-k mean + joint_distance”；CLI 和调试脚本只编排模板构建、模板匹配和阈值注入，不直接理解模板内部实现。`deepface_engine.py` 继续作为底层检测、embedding、距离和默认阈值边界，避免把人物级策略塞回引擎层。

**Tech Stack:** Python 3.13+、DeepFace、NumPy、Pillow、pillow-heif、pytest

---

## 文件结构

### 新增

- `src/hikbox_pictures/reference_template.py`
- `scripts/calibrate_thresholds.py`
- `tests/test_reference_template.py`
- `docs/superpowers/plans/2026-04-10-hikbox-pictures-template-matching.md`

### 修改

- `src/hikbox_pictures/models.py`
- `src/hikbox_pictures/matcher.py`
- `src/hikbox_pictures/cli.py`
- `scripts/inspect_distances.py`
- `README.md`
- `tests/test_matcher.py`
- `tests/test_cli.py`
- `tests/test_inspect_distances.py`
- `tests/test_repo_samples.py`
- `tests/test_smoke.py`

### 保持不变

- `src/hikbox_pictures/deepface_engine.py`
- `src/hikbox_pictures/exporter.py`
- `src/hikbox_pictures/scanner.py`
- `src/hikbox_pictures/reference_loader.py`
- `scripts/extract_faces.py`
- `scripts/install.sh`

---

### Task 1: 定义模板匹配数据模型

**Files:**
- Modify: `src/hikbox_pictures/models.py`
- Test: `tests/test_smoke.py`

- [ ] **Step 1: 先写失败测试，锁定新模型默认值与兼容面**

```python
from pathlib import Path

import numpy as np

from hikbox_pictures.models import (
    CandidatePhoto,
    MatchBucket,
    PhotoEvaluation,
    ReferenceSample,
    ReferenceTemplate,
    TemplateMatchResult,
)


def test_shared_models_have_expected_template_defaults(tmp_path: Path) -> None:
    sample = ReferenceSample(
        path=tmp_path / "ref.jpg",
        embedding=np.asarray([0.1, 0.2], dtype=np.float32),
        bbox=(1, 5, 6, 0),
        image_size=(100, 80),
        face_area_ratio=0.25,
        sharpness_score=12.0,
        quality_score=0.8,
        center_distance=0.15,
        kept=True,
        drop_reason=None,
    )
    template = ReferenceTemplate(
        name="A",
        samples=[sample],
        kept_samples=[sample],
        centroid_embedding=np.asarray([0.1, 0.2], dtype=np.float32),
        match_threshold=0.42,
        top_k=1,
    )
    result = TemplateMatchResult(
        template_distance=0.2,
        centroid_distance=0.25,
        matched=True,
        top_k_distances=[0.2],
    )
    candidate = CandidatePhoto(path=tmp_path / "pair.jpg")
    evaluation = PhotoEvaluation(
        candidate=candidate,
        detected_face_count=2,
        bucket=MatchBucket.ONLY_TWO,
        joint_distance=0.2,
        best_match_pair=(0, 1),
    )

    assert template.name == "A"
    assert template.dropped_samples == []
    assert template.match_threshold == 0.42
    assert template.top_k == 1
    assert result.matched is True
    assert result.top_k_distances == [0.2]
    assert evaluation.bucket is MatchBucket.ONLY_TWO
    assert evaluation.joint_distance == 0.2
    assert evaluation.best_match_pair == (0, 1)
```

- [ ] **Step 2: 运行测试，确认当前代码缺少这些数据模型**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_smoke.py::test_shared_models_have_expected_template_defaults -v`
Expected: FAIL，提示 `ReferenceSample` / `ReferenceTemplate` / `TemplateMatchResult` 不存在，或 `PhotoEvaluation` 不接受 `joint_distance`。

- [ ] **Step 3: 最小实现模型定义，保持 exporter 兼容**

```python
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import numpy as np
import numpy.typing as npt


Embedding = npt.NDArray[np.float32]
BBoxTLBR = tuple[int, int, int, int]
ImageSize = tuple[int, int]


class MatchBucket(str, Enum):
    ONLY_TWO = "only-two"
    GROUP = "group"


@dataclass(frozen=True)
class CandidatePhoto:
    path: Path
    live_photo_video: Path | None = None


@dataclass(frozen=True)
class ReferenceSample:
    path: Path
    embedding: Embedding
    bbox: BBoxTLBR
    image_size: ImageSize
    face_area_ratio: float
    sharpness_score: float
    quality_score: float
    center_distance: float | None
    kept: bool
    drop_reason: str | None


@dataclass(frozen=True)
class ReferenceTemplate:
    name: str
    samples: list[ReferenceSample]
    kept_samples: list[ReferenceSample]
    centroid_embedding: Embedding
    match_threshold: float
    top_k: int

    @property
    def dropped_samples(self) -> list[ReferenceSample]:
        return [sample for sample in self.samples if not sample.kept]


@dataclass(frozen=True)
class TemplateMatchResult:
    template_distance: float
    centroid_distance: float
    matched: bool
    top_k_distances: list[float]


@dataclass(frozen=True)
class PhotoEvaluation:
    candidate: CandidatePhoto
    detected_face_count: int
    bucket: MatchBucket | None
    joint_distance: float | None = None
    best_match_pair: tuple[int, int] | None = None


@dataclass
class RunSummary:
    scanned_files: int = 0
    only_two_matches: int = 0
    group_matches: int = 0
    skipped_decode_errors: int = 0
    skipped_no_faces: int = 0
    missing_live_photo_videos: int = 0
    warnings: list[str] = field(default_factory=list)
```

- [ ] **Step 4: 运行测试，确认新模型通过且旧默认行为未回归**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_smoke.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/hikbox_pictures/models.py tests/test_smoke.py
git commit -m "feat: add template matching models"
```

### Task 2: 实现参考模板构建与清洗

**Files:**
- Create: `src/hikbox_pictures/reference_template.py`
- Test: `tests/test_reference_template.py`

- [ ] **Step 1: 先写失败测试，覆盖质量分数、清洗和 top-k**

```python
from pathlib import Path

import numpy as np
import pytest

from hikbox_pictures.models import ReferenceSample
from hikbox_pictures.reference_template import (
    build_reference_template,
    compute_template_match,
    select_template_threshold,
)


class FakeEngine:
    def __init__(self, distance_map: dict[tuple[float, float], float]) -> None:
        self.distance_map = distance_map

    def distance(self, lhs, rhs) -> float:
        lhs_key = float(np.asarray(lhs, dtype=np.float32).reshape(-1)[0])
        rhs_key = float(np.asarray(rhs, dtype=np.float32).reshape(-1)[0])
        return self.distance_map[(lhs_key, rhs_key)]


def _sample(tmp_path: Path, name: str, embedding_scalar: float, *, area: float, sharpness: float) -> ReferenceSample:
    return ReferenceSample(
        path=tmp_path / name,
        embedding=np.asarray([embedding_scalar], dtype=np.float32),
        bbox=(0, 10, 10, 0),
        image_size=(20, 20),
        face_area_ratio=area,
        sharpness_score=sharpness,
        quality_score=0.0,
        center_distance=None,
        kept=True,
        drop_reason=None,
    )


def test_build_reference_template_does_not_drop_small_reference_sets(tmp_path: Path) -> None:
    samples = [
        _sample(tmp_path, "a.jpg", 1.0, area=0.7, sharpness=10.0),
        _sample(tmp_path, "b.jpg", 1.1, area=0.6, sharpness=9.0),
        _sample(tmp_path, "c.jpg", 5.0, area=0.1, sharpness=1.0),
    ]
    engine = FakeEngine(
        {
            (1.0, 1.0): 0.0,
            (1.0, 1.1): 0.1,
            (1.0, 5.0): 0.9,
            (1.1, 1.0): 0.1,
            (1.1, 1.1): 0.0,
            (1.1, 5.0): 0.8,
            (5.0, 1.0): 0.9,
            (5.0, 1.1): 0.8,
            (5.0, 5.0): 0.0,
        }
    )

    template = build_reference_template("A", samples, engine=engine, default_threshold=0.42)

    assert [sample.path.name for sample in template.kept_samples] == ["a.jpg", "b.jpg", "c.jpg"]
    assert template.top_k == 3
    assert template.match_threshold == 0.42


def test_build_reference_template_drops_outlier_when_reference_set_is_large(tmp_path: Path) -> None:
    samples = [
        _sample(tmp_path, "a.jpg", 1.0, area=0.8, sharpness=10.0),
        _sample(tmp_path, "b.jpg", 1.1, area=0.7, sharpness=9.5),
        _sample(tmp_path, "c.jpg", 1.2, area=0.7, sharpness=9.0),
        _sample(tmp_path, "d.jpg", 1.3, area=0.6, sharpness=8.5),
        _sample(tmp_path, "outlier.jpg", 5.0, area=0.1, sharpness=1.0),
    ]
    engine = FakeEngine({
        (1.0, 1.0): 0.0, (1.0, 1.1): 0.1, (1.0, 1.2): 0.2, (1.0, 1.3): 0.3, (1.0, 5.0): 2.5,
        (1.1, 1.0): 0.1, (1.1, 1.1): 0.0, (1.1, 1.2): 0.1, (1.1, 1.3): 0.2, (1.1, 5.0): 2.4,
        (1.2, 1.0): 0.2, (1.2, 1.1): 0.1, (1.2, 1.2): 0.0, (1.2, 1.3): 0.1, (1.2, 5.0): 2.3,
        (1.3, 1.0): 0.3, (1.3, 1.1): 0.2, (1.3, 1.2): 0.1, (1.3, 1.3): 0.0, (1.3, 5.0): 2.2,
        (5.0, 1.0): 2.5, (5.0, 1.1): 2.4, (5.0, 1.2): 2.3, (5.0, 1.3): 2.2, (5.0, 5.0): 0.0,
    })

    template = build_reference_template("A", samples, engine=engine, default_threshold=0.5)

    assert [sample.path.name for sample in template.kept_samples] == ["a.jpg", "b.jpg", "c.jpg", "d.jpg"]
    assert [sample.path.name for sample in template.dropped_samples] == ["outlier.jpg"]
    assert template.top_k == 3


def test_compute_template_match_uses_top_k_mean_and_centroid_distance(tmp_path: Path) -> None:
    samples = [
        _sample(tmp_path, "a.jpg", 1.0, area=0.7, sharpness=10.0),
        _sample(tmp_path, "b.jpg", 1.1, area=0.7, sharpness=9.0),
        _sample(tmp_path, "c.jpg", 1.2, area=0.7, sharpness=8.0),
    ]
    engine = FakeEngine({
        (9.0, 1.0): 0.2,
        (9.0, 1.1): 0.1,
        (9.0, 1.2): 0.3,
        (9.0, 9.9): 0.15,
    })

    template = build_reference_template(
        "A",
        samples,
        engine=engine,
        default_threshold=0.18,
        centroid_embedding=np.asarray([9.9], dtype=np.float32),
    )

    result = compute_template_match(np.asarray([9.0], dtype=np.float32), template, engine=engine)

    assert result.template_distance == pytest.approx(0.2)
    assert result.centroid_distance == pytest.approx(0.15)
    assert result.top_k_distances == pytest.approx([0.1, 0.2, 0.3])
    assert result.matched is False


def test_select_template_threshold_prefers_override_then_global_then_engine_default() -> None:
    assert select_template_threshold(override_threshold=0.3, fallback_threshold=0.4, engine_threshold=0.5) == 0.3
    assert select_template_threshold(override_threshold=None, fallback_threshold=0.4, engine_threshold=0.5) == 0.4
    assert select_template_threshold(override_threshold=None, fallback_threshold=None, engine_threshold=0.5) == 0.5
```

- [ ] **Step 2: 运行测试，确认新模块尚不存在**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_reference_template.py -q`
Expected: FAIL，提示 `tests/test_reference_template.py` 或 `hikbox_pictures.reference_template` 不存在。

- [ ] **Step 3: 实现模板构建与匹配最小代码**

```python
from __future__ import annotations

from dataclasses import replace
from statistics import median

import numpy as np

from hikbox_pictures.models import ReferenceSample, ReferenceTemplate, TemplateMatchResult


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


def _robust_normalize(values: list[float]) -> list[float]:
    if not values:
        return []
    minimum = min(values)
    maximum = max(values)
    if maximum <= minimum:
        return [1.0] * len(values)
    return [(value - minimum) / (maximum - minimum) for value in values]


def _with_quality_scores(samples: list[ReferenceSample]) -> list[ReferenceSample]:
    area_scores = _robust_normalize([sample.face_area_ratio for sample in samples])
    sharpness_scores = _robust_normalize([sample.sharpness_score for sample in samples])

    scored: list[ReferenceSample] = []
    for sample, area_score, sharpness_score in zip(samples, area_scores, sharpness_scores, strict=True):
        quality_score = 0.6 * area_score + 0.4 * sharpness_score
        scored.append(replace(sample, quality_score=quality_score))
    return scored


def _center_distances(samples: list[ReferenceSample], *, engine) -> list[float]:
    distances: list[float] = []
    for index, sample in enumerate(samples):
        other_embeddings = [other.embedding for other_index, other in enumerate(samples) if other_index != index]
        if not other_embeddings:
            distances.append(0.0)
            continue
        distances.append(float(np.mean([engine.distance(sample.embedding, other) for other in other_embeddings])))
    return distances


def _mad_threshold(distances: list[float]) -> float:
    center = median(distances)
    deviations = [abs(distance - center) for distance in distances]
    mad = median(deviations)
    if mad == 0:
        return center
    return center + 2.5 * mad


def _weighted_centroid(samples: list[ReferenceSample]) -> np.ndarray:
    weights = np.asarray([max(sample.quality_score, 1e-6) for sample in samples], dtype=np.float32)
    vectors = np.stack([sample.embedding for sample in samples]).astype(np.float32)
    centroid = np.average(vectors, axis=0, weights=weights)
    norm = float(np.linalg.norm(centroid))
    if norm > 0:
        centroid = centroid / norm
    return centroid.astype(np.float32)


def build_reference_template(
    name: str,
    samples: list[ReferenceSample],
    *,
    engine,
    default_threshold: float,
    override_threshold: float | None = None,
    fallback_threshold: float | None = None,
    centroid_embedding: np.ndarray | None = None,
) -> ReferenceTemplate:
    scored_samples = _with_quality_scores(samples)
    center_distances = _center_distances(scored_samples, engine=engine)
    annotated = [replace(sample, center_distance=center_distance) for sample, center_distance in zip(scored_samples, center_distances, strict=True)]

    kept_samples = annotated
    if len(annotated) >= 5:
        threshold = _mad_threshold(center_distances)
        kept_samples = [replace(sample, kept=sample.center_distance <= threshold, drop_reason=None if sample.center_distance <= threshold else "离群样本") for sample in annotated]
        kept_samples = [sample for sample in kept_samples if sample.kept]
        if len(kept_samples) < 3:
            ranked = sorted(annotated, key=lambda sample: (sample.center_distance if sample.center_distance is not None else float("inf"), sample.path.name))
            kept_names = {sample.path for sample in ranked[:3]}
            kept_samples = [replace(sample, kept=sample.path in kept_names, drop_reason=None if sample.path in kept_names else "离群样本") for sample in annotated]
            kept_samples = [sample for sample in kept_samples if sample.kept]

    kept_paths = {sample.path for sample in kept_samples}
    final_samples = [replace(sample, kept=sample.path in kept_paths, drop_reason=None if sample.path in kept_paths else "离群样本") for sample in annotated]
    top_k = min(3, len(kept_samples))
    threshold = select_template_threshold(
        override_threshold=override_threshold,
        fallback_threshold=fallback_threshold,
        engine_threshold=default_threshold,
    )

    return ReferenceTemplate(
        name=name,
        samples=final_samples,
        kept_samples=kept_samples,
        centroid_embedding=_weighted_centroid(kept_samples) if centroid_embedding is None else centroid_embedding.astype(np.float32),
        match_threshold=threshold,
        top_k=top_k,
    )


def compute_template_match(embedding: np.ndarray, template: ReferenceTemplate, *, engine) -> TemplateMatchResult:
    distances = sorted(float(engine.distance(embedding, sample.embedding)) for sample in template.kept_samples)
    top_k_distances = distances[: template.top_k]
    template_distance = float(np.mean(top_k_distances)) if top_k_distances else float("inf")
    centroid_distance = float(engine.distance(embedding, template.centroid_embedding))
    return TemplateMatchResult(
        template_distance=template_distance,
        centroid_distance=centroid_distance,
        matched=template_distance <= template.match_threshold,
        top_k_distances=top_k_distances,
    )
```

- [ ] **Step 4: 运行测试，确认模板逻辑符合规格**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_reference_template.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/hikbox_pictures/reference_template.py tests/test_reference_template.py
git commit -m "feat: add reference template builder"
```

### Task 3: 将 matcher 切换到模板匹配

**Files:**
- Modify: `src/hikbox_pictures/matcher.py`
- Test: `tests/test_matcher.py`

- [ ] **Step 1: 先写失败测试，锁定模板匹配、joint_distance 与最佳配对**

```python
from types import SimpleNamespace

import numpy as np

from hikbox_pictures.matcher import evaluate_candidate_photo
from hikbox_pictures.models import CandidatePhoto, MatchBucket, ReferenceTemplate, ReferenceSample


class FakeEngine:
    def __init__(self, faces) -> None:
        self._faces = faces

    def detect_faces(self, image_path):
        return self._faces

    def distance(self, lhs, rhs) -> float:
        lhs_value = float(np.asarray(lhs, dtype=np.float32).reshape(-1)[0])
        rhs_value = float(np.asarray(rhs, dtype=np.float32).reshape(-1)[0])
        return abs(lhs_value - rhs_value) / 10


def _face(face_id: float, bbox=(0, 10, 10, 0)):
    return SimpleNamespace(embedding=np.asarray([face_id], dtype=np.float32), bbox=bbox)


def _template(name: str, threshold: float, *values: float) -> ReferenceTemplate:
    samples = [
        ReferenceSample(
            path=Path(f"{name}-{index}.jpg"),
            embedding=np.asarray([value], dtype=np.float32),
            bbox=(0, 10, 10, 0),
            image_size=(20, 20),
            face_area_ratio=0.5,
            sharpness_score=1.0,
            quality_score=1.0,
            center_distance=0.1,
            kept=True,
            drop_reason=None,
        )
        for index, value in enumerate(values)
    ]
    return ReferenceTemplate(
        name=name,
        samples=samples,
        kept_samples=samples,
        centroid_embedding=np.asarray([values[0]], dtype=np.float32),
        match_threshold=threshold,
        top_k=min(3, len(samples)),
    )


def test_evaluate_candidate_photo_uses_template_thresholds_and_joint_distance(tmp_path: Path) -> None:
    photo = CandidatePhoto(path=tmp_path / "pair.jpg")
    engine = FakeEngine([_face(10.0), _face(30.0)])

    evaluation = evaluate_candidate_photo(
        photo,
        _template("A", 0.2, 11.0, 12.0, 13.0),
        _template("B", 0.2, 29.0, 30.0, 31.0),
        engine=engine,
    )

    assert evaluation.bucket is MatchBucket.ONLY_TWO
    assert evaluation.joint_distance == 0.1
    assert evaluation.best_match_pair == (0, 1)
```

- [ ] **Step 2: 运行测试，确认现有 matcher 仍然只接受 embedding 列表**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_matcher.py::test_evaluate_candidate_photo_uses_template_thresholds_and_joint_distance -v`
Expected: FAIL，提示 `ReferenceTemplate` 不被支持或 `PhotoEvaluation` 未包含 `joint_distance`。

- [ ] **Step 3: 最小实现模板匹配逻辑，保留分桶语义**

```python
from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from hikbox_pictures.deepface_engine import DeepFaceEngine, EmbeddingLike
from hikbox_pictures.models import CandidatePhoto, MatchBucket, PhotoEvaluation, ReferenceTemplate
from hikbox_pictures.reference_template import compute_template_match


@runtime_checkable
class MatcherEngineProtocol(Protocol):
    def detect_faces(self, image_path: object) -> list[object]:
        ...

    def distance(self, lhs: EmbeddingLike, rhs: EmbeddingLike) -> float:
        ...


def _compute_template_matches(candidate_embeddings: Sequence[EmbeddingLike], template: ReferenceTemplate, *, engine: MatcherEngineProtocol):
    return [compute_template_match(np.asarray(candidate_embedding, dtype=np.float32), template, engine=engine) for candidate_embedding in candidate_embeddings]


def _select_best_joint_pair(matches_a, matches_b) -> tuple[tuple[int, int] | None, float | None]:
    best_pair: tuple[int, int] | None = None
    best_joint_distance: float | None = None
    for index_a, match_a in enumerate(matches_a):
        if not match_a.matched:
            continue
        for index_b, match_b in enumerate(matches_b):
            if index_a == index_b or not match_b.matched:
                continue
            joint_distance = max(match_a.template_distance, match_b.template_distance)
            if best_joint_distance is None or joint_distance < best_joint_distance:
                best_pair = (index_a, index_b)
                best_joint_distance = joint_distance
    return best_pair, best_joint_distance


def evaluate_candidate_photo(
    photo: CandidatePhoto,
    person_a_template: ReferenceTemplate,
    person_b_template: ReferenceTemplate,
    *,
    engine: MatcherEngineProtocol | None = None,
    distance_threshold: float = DEFAULT_DISTANCE_THRESHOLD,
    tolerance: float | None = None,
) -> PhotoEvaluation:
    if tolerance is not None:
        raise ValueError("tolerance 参数已弃用，请使用模板阈值语义")
    if distance_threshold != DEFAULT_DISTANCE_THRESHOLD:
        raise ValueError("distance_threshold 不再生效，请改用模板阈值")

    face_engine = _validate_matcher_engine(engine if engine is not None else _get_cached_matcher_engine())
    faces = face_engine.detect_faces(photo.path)
    if not faces:
        return PhotoEvaluation(candidate=photo, detected_face_count=0, bucket=None)

    candidate_embeddings = [face.embedding for face in faces]
    matches_a = _compute_template_matches(candidate_embeddings, person_a_template, engine=face_engine)
    matches_b = _compute_template_matches(candidate_embeddings, person_b_template, engine=face_engine)
    best_pair, joint_distance = _select_best_joint_pair(matches_a, matches_b)

    if best_pair is None:
        return PhotoEvaluation(candidate=photo, detected_face_count=len(candidate_embeddings), bucket=None)

    if len(candidate_embeddings) == 2:
        bucket = MatchBucket.ONLY_TWO
    else:
        face_areas = [_face_area(face) for face in faces]
        primary_pair = _select_largest_matching_pair({best_pair[0]}, {best_pair[1]}, face_areas)
        bucket = MatchBucket.GROUP if _has_large_extra_face(face_areas, primary_pair=primary_pair) else MatchBucket.ONLY_TWO

    return PhotoEvaluation(
        candidate=photo,
        detected_face_count=len(candidate_embeddings),
        bucket=bucket,
        joint_distance=joint_distance,
        best_match_pair=best_pair,
    )
```

- [ ] **Step 4: 扩展测试到显式 engine、distinct faces 与 group/only-two**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_matcher.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/hikbox_pictures/matcher.py tests/test_matcher.py
git commit -m "feat: switch matcher to templates"
```

### Task 4: 将 CLI 切到模板构建与独立阈值

**Files:**
- Modify: `src/hikbox_pictures/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: 先写失败测试，锁定 A/B 阈值参数与模板构建调用**

```python
from datetime import datetime

from hikbox_pictures.cli import main
from hikbox_pictures.models import CandidatePhoto, MatchBucket, PhotoEvaluation


def test_main_builds_templates_and_passes_person_thresholds(monkeypatch, tmp_path, capsys) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    ref_a_dir = tmp_path / "ref-a"
    ref_b_dir = tmp_path / "ref-b"
    input_dir.mkdir()
    output_dir.mkdir()
    ref_a_dir.mkdir()
    ref_b_dir.mkdir()

    candidate = CandidatePhoto(path=input_dir / "pair.jpg")
    candidate.path.write_bytes(b"pair")

    fake_engine = object()
    monkeypatch.setattr("hikbox_pictures.cli.DeepFaceEngine.create", lambda **kwargs: fake_engine)
    monkeypatch.setattr(
        "hikbox_pictures.cli.load_reference_embeddings",
        lambda path, engine: (([[0.1]], [path / "sample.jpg"]) if path == ref_a_dir else ([[0.2]], [path / "sample.jpg"])),
    )

    template_calls = []

    def fake_build_reference_template(name, samples, *, engine, default_threshold, override_threshold=None, fallback_threshold=None):
        template_calls.append(
            {
                "name": name,
                "count": len(samples),
                "default_threshold": default_threshold,
                "override_threshold": override_threshold,
                "fallback_threshold": fallback_threshold,
            }
        )
        return object()

    monkeypatch.setattr("hikbox_pictures.cli.build_reference_template", fake_build_reference_template)
    monkeypatch.setattr("hikbox_pictures.cli.iter_candidate_photos", lambda root: iter([candidate]))
    monkeypatch.setattr(
        "hikbox_pictures.cli.evaluate_candidate_photo",
        lambda candidate, template_a, template_b, *, engine: PhotoEvaluation(candidate=candidate, detected_face_count=2, bucket=MatchBucket.ONLY_TWO),
    )
    monkeypatch.setattr("hikbox_pictures.cli.resolve_capture_datetime", lambda path: datetime(2025, 4, 3, 10, 30))
    monkeypatch.setattr("hikbox_pictures.cli.export_match", lambda evaluation, output_root, capture_datetime: None)

    exit_code = main(
        [
            "--input", str(input_dir),
            "--ref-a-dir", str(ref_a_dir),
            "--ref-b-dir", str(ref_b_dir),
            "--output", str(output_dir),
            "--distance-threshold", "0.40",
            "--distance-threshold-a", "0.32",
            "--distance-threshold-b", "0.36",
        ]
    )

    assert exit_code == 0
    assert template_calls == [
        {"name": "A", "count": 1, "default_threshold": 0.4, "override_threshold": 0.32, "fallback_threshold": 0.4},
        {"name": "B", "count": 1, "default_threshold": 0.4, "override_threshold": 0.36, "fallback_threshold": 0.4},
    ]
```

- [ ] **Step 2: 运行测试，确认 CLI 尚未具备这些参数和构建流程**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_cli.py::test_main_builds_templates_and_passes_person_thresholds -v`
Expected: FAIL，提示 `--distance-threshold-a` / `--distance-threshold-b` 未定义或 `build_reference_template` 不存在。

- [ ] **Step 3: 最小实现 CLI 编排改造**

```python
from hikbox_pictures.reference_template import build_reference_template, build_reference_samples_from_embeddings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hikbox-pictures")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--ref-a-dir", required=True, type=Path)
    parser.add_argument("--ref-b-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model-name", default="ArcFace")
    parser.add_argument("--detector-backend", default="retinaface")
    parser.add_argument("--distance-metric", default="cosine")
    parser.add_argument("--distance-threshold", type=float)
    parser.add_argument("--distance-threshold-a", type=float)
    parser.add_argument("--distance-threshold-b", type=float)
    parser.add_argument("--align", dest="align", action=argparse.BooleanOptionalAction, default=True)
    return parser


def _build_template(name: str, ref_dir: Path, *, engine: DeepFaceEngine, fallback_threshold: float | None, override_threshold: float | None):
    embeddings, source_paths = load_reference_embeddings(ref_dir, engine)
    samples = build_reference_samples_from_embeddings(source_paths, embeddings, engine=engine)
    default_threshold = fallback_threshold if fallback_threshold is not None else engine.distance_threshold
    return build_reference_template(
        name,
        samples,
        engine=engine,
        default_threshold=default_threshold,
        override_threshold=override_threshold,
        fallback_threshold=fallback_threshold,
    )


def main(argv: list[str] | None = None) -> int:
    ...
    engine = DeepFaceEngine.create(...)
    template_a = _build_template("A", args.ref_a_dir, engine=engine, fallback_threshold=args.distance_threshold, override_threshold=args.distance_threshold_a)
    template_b = _build_template("B", args.ref_b_dir, engine=engine, fallback_threshold=args.distance_threshold, override_threshold=args.distance_threshold_b)
    ...
    evaluation = evaluate_candidate_photo(candidate, template_a, template_b, engine=engine)
```

- [ ] **Step 4: 运行 CLI 测试，确认旧摘要与新参数同时可用**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_cli.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add src/hikbox_pictures/cli.py tests/test_cli.py
git commit -m "feat: build templates in cli"
```

### Task 5: 升级距离调试脚本输出模板诊断信息

**Files:**
- Modify: `scripts/inspect_distances.py`
- Test: `tests/test_inspect_distances.py`

- [ ] **Step 1: 先写失败测试，锁定模板输出字段**

```python
from types import SimpleNamespace

from hikbox_pictures.models import CandidatePhoto


def test_main_prints_template_distances_and_joint_distance(monkeypatch, tmp_path, capsys) -> None:
    script = _load_script_module()
    input_dir = tmp_path / "input"
    ref_a_dir = tmp_path / "ref-a"
    ref_b_dir = tmp_path / "ref-b"
    input_dir.mkdir()
    ref_a_dir.mkdir()
    ref_b_dir.mkdir()

    candidate_path = input_dir / "candidate.jpg"
    candidate_path.write_bytes(b"img")

    fake_engine = SimpleNamespace(
        model_name="ArcFace",
        detector_backend="retinaface",
        distance_metric="cosine",
        align=True,
        distance_threshold=0.4,
        threshold_source="explicit",
    )
    monkeypatch.setattr(script.DeepFaceEngine, "create", lambda **kwargs: fake_engine)
    monkeypatch.setattr(script, "iter_candidate_photos", lambda root: iter([CandidatePhoto(path=candidate_path)]))
    monkeypatch.setattr(script, "_load_candidate_face_encodings", lambda path, engine: ([(0, 10, 10, 0), (0, 20, 20, 0)], [[1.0], [2.0]]))

    fake_template = SimpleNamespace(
        kept_samples=[SimpleNamespace(path=ref_a_dir / "a.jpg")],
        dropped_samples=[],
        match_threshold=0.32,
        top_k=1,
    )
    monkeypatch.setattr(script, "build_reference_template", lambda *args, **kwargs: fake_template)
    monkeypatch.setattr(script, "build_reference_samples_from_embeddings", lambda paths, embeddings, *, engine: [object()])
    monkeypatch.setattr(script, "load_reference_embeddings", lambda path, engine: ([[0.1]], [path / "sample.jpg"]))

    results = iter(
        [
            SimpleNamespace(template_distance=0.12, centroid_distance=0.13, matched=True),
            SimpleNamespace(template_distance=0.44, centroid_distance=0.40, matched=False),
            SimpleNamespace(template_distance=0.45, centroid_distance=0.41, matched=False),
            SimpleNamespace(template_distance=0.11, centroid_distance=0.14, matched=True),
        ]
    )
    monkeypatch.setattr(script, "compute_template_match", lambda embedding, template, *, engine: next(results))

    exit_code = script.main([
        "--input", str(input_dir),
        "--ref-a-dir", str(ref_a_dir),
        "--ref-b-dir", str(ref_b_dir),
        "--distance-threshold-a", "0.32",
        "--distance-threshold-b", "0.35",
    ])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "template_threshold_a=0.3200" in output
    assert "template_dist_a=0.1200" in output
    assert "centroid_dist_b=0.4100" in output
    assert "joint_distance=0.1200" in output
```

- [ ] **Step 2: 运行测试，确认脚本当前只会打印 `dist_a/dist_b`**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_inspect_distances.py::test_main_prints_template_distances_and_joint_distance -v`
Expected: FAIL，输出中没有 `template_dist_a` / `joint_distance`。

- [ ] **Step 3: 最小实现脚本模板输出**

```python
from hikbox_pictures.reference_template import (
    build_reference_samples_from_embeddings,
    build_reference_template,
    compute_template_match,
)


def _best_joint_distance(matches_a, matches_b):
    joint_distances = [
        max(match_a.template_distance, match_b.template_distance)
        for index_a, match_a in enumerate(matches_a)
        for index_b, match_b in enumerate(matches_b)
        if index_a != index_b and match_a.matched and match_b.matched
    ]
    return min(joint_distances) if joint_distances else None


def main(argv: list[str] | None = None) -> int:
    ...
    ref_a_embeddings, ref_a_paths = load_reference_embeddings(args.ref_a_dir, engine)
    ref_b_embeddings, ref_b_paths = load_reference_embeddings(args.ref_b_dir, engine)
    ref_a_samples = build_reference_samples_from_embeddings(ref_a_paths, ref_a_embeddings, engine=engine)
    ref_b_samples = build_reference_samples_from_embeddings(ref_b_paths, ref_b_embeddings, engine=engine)
    template_a = build_reference_template("A", ref_a_samples, engine=engine, default_threshold=engine.distance_threshold, override_threshold=args.distance_threshold_a, fallback_threshold=args.distance_threshold)
    template_b = build_reference_template("B", ref_b_samples, engine=engine, default_threshold=engine.distance_threshold, override_threshold=args.distance_threshold_b, fallback_threshold=args.distance_threshold)
    print(f"模板配置: template_threshold_a={_format_distance(template_a.match_threshold)} template_threshold_b={_format_distance(template_b.match_threshold)} top_k_a={template_a.top_k} top_k_b={template_b.top_k}")
    ...
    matches_a = [compute_template_match(encoding, template_a, engine=engine) for encoding in encodings]
    matches_b = [compute_template_match(encoding, template_b, engine=engine) for encoding in encodings]
    joint_distance = _best_joint_distance(matches_a, matches_b)
    ...
    print(f"  face[{index}] location={location} template_dist_a={_format_distance(match_a.template_distance)} template_dist_b={_format_distance(match_b.template_distance)} centroid_dist_a={_format_distance(match_a.centroid_distance)} centroid_dist_b={_format_distance(match_b.centroid_distance)} match_a={'Y' if match_a.matched else 'N'} match_b={'Y' if match_b.matched else 'N'}")
    if joint_distance is not None:
        print(f"  joint_distance={_format_distance(joint_distance)}")
```

- [ ] **Step 4: 运行测试，确认调试脚本输出与标注逻辑仍然稳定**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_inspect_distances.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add scripts/inspect_distances.py tests/test_inspect_distances.py
git commit -m "feat: inspect template match distances"
```

### Task 6: 新增阈值标定脚本

**Files:**
- Create: `scripts/calibrate_thresholds.py`
- Test: `tests/test_repo_samples.py`

- [ ] **Step 1: 先写失败测试，锁定脚本在 README 中可见并能给出建议阈值**

```python
from pathlib import Path


def test_readme_mentions_template_calibration_workflow() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "calibrate_thresholds.py" in readme
    assert "--distance-threshold-a" in readme
    assert "--distance-threshold-b" in readme
    assert "joint_distance" in readme
```

- [ ] **Step 2: 运行测试，确认 README 尚未包含新脚本与新参数**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_repo_samples.py::test_readme_mentions_template_calibration_workflow -v`
Expected: FAIL。

- [ ] **Step 3: 实现脚本最小版本，输出建议阈值**

```python
#!/usr/bin/env python3

from __future__ import annotations

import argparse
from pathlib import Path

from hikbox_pictures.deepface_engine import DeepFaceEngine
from hikbox_pictures.reference_loader import load_reference_embeddings
from hikbox_pictures.reference_template import (
    build_reference_samples_from_embeddings,
    build_reference_template,
    compute_best_face_distance_in_directory,
    scan_threshold_metrics,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="calibrate_thresholds")
    parser.add_argument("--ref-dir", required=True, type=Path)
    parser.add_argument("--positive-dir", required=True, type=Path)
    parser.add_argument("--negative-dir", required=True, type=Path)
    parser.add_argument("--model-name", default="ArcFace")
    parser.add_argument("--detector-backend", default="retinaface")
    parser.add_argument("--distance-metric", default="cosine")
    parser.add_argument("--distance-threshold", type=float)
    parser.add_argument("--align", dest="align", action=argparse.BooleanOptionalAction, default=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    engine = DeepFaceEngine.create(
        model_name=args.model_name,
        detector_backend=args.detector_backend,
        distance_metric=args.distance_metric,
        align=args.align,
        distance_threshold=args.distance_threshold,
    )
    embeddings, source_paths = load_reference_embeddings(args.ref_dir, engine)
    samples = build_reference_samples_from_embeddings(source_paths, embeddings, engine=engine)
    template = build_reference_template("target", samples, engine=engine, default_threshold=engine.distance_threshold, fallback_threshold=args.distance_threshold)
    positive_scores = compute_best_face_distance_in_directory(args.positive_dir, template, engine=engine)
    negative_scores = compute_best_face_distance_in_directory(args.negative_dir, template, engine=engine)
    metrics = scan_threshold_metrics(positive_scores, negative_scores)
    print(f"best_f1_threshold={metrics.best_f1_threshold:.4f}")
    print(f"best_youden_j_threshold={metrics.best_youden_j_threshold:.4f}")
    print("建议：将结果传给 --distance-threshold-a 或 --distance-threshold-b")
    return 0
```

- [ ] **Step 4: 更新 README 文档并跑测试**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_repo_samples.py tests/test_smoke.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add scripts/calibrate_thresholds.py README.md tests/test_repo_samples.py tests/test_smoke.py
git commit -m "feat: add threshold calibration script"
```

### Task 7: 跑回归测试并整理交付说明

**Files:**
- Modify: `README.md`

- [ ] **Step 1: 运行模板匹配相关测试集**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_reference_template.py tests/test_matcher.py tests/test_cli.py tests/test_inspect_distances.py tests/test_smoke.py tests/test_repo_samples.py -q`
Expected: PASS。

- [ ] **Step 2: 运行全量测试，确认无回归**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest -q`
Expected: PASS。

- [ ] **Step 3: 手动检查 README 样例命令覆盖新参数与脚本**

```markdown
```bash
hikbox-pictures \
  --input /path/to/photo-library \
  --ref-a-dir /path/to/person-a-images \
  --ref-b-dir /path/to/person-b-images \
  --output /path/to/output \
  --model-name ArcFace \
  --detector-backend retinaface \
  --distance-metric cosine \
  --distance-threshold-a 0.32 \
  --distance-threshold-b 0.36 \
  --align
```

```bash
source .venv/bin/activate
PYTHONPATH=src python3 scripts/calibrate_thresholds.py \
  --ref-dir /path/to/person-a-images \
  --positive-dir /path/to/person-a-positive \
  --negative-dir /path/to/person-a-negative \
  --model-name ArcFace \
  --detector-backend retinaface \
  --distance-metric cosine \
  --align
```
```
```

- [ ] **Step 4: 提交最终文档整理**

```bash
git add README.md
git commit -m "docs: describe template matching workflow"
```

## 自检结论

- 规格覆盖：模板构建、质量评分、离群清洗、top-k mean、A/B 独立阈值、`joint_distance`、调试输出、阈值标定脚本、README 文档都已有对应任务。
- 占位检查：计划中没有 `TODO` / `TBD` / “类似 Task N” 之类占位语句；每个任务都给出了文件、测试、命令和最小代码骨架。
- 类型一致性：全程统一使用 `ReferenceSample`、`ReferenceTemplate`、`TemplateMatchResult`、`PhotoEvaluation.joint_distance` 和 `PhotoEvaluation.best_match_pair` 这组命名，CLI 和调试脚本都依赖同一模板接口。
