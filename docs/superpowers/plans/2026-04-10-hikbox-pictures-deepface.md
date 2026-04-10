# HikBox Pictures DeepFace 迁移 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将当前 `InsightFace` 检测与匹配流程完整迁移到 `DeepFace`，并保持 `only-two` / `group` 导出语义、Live Photo `MOV` 复制与时间元数据行为不回归。

**Architecture:** 新增 `deepface_engine.py` 作为唯一 DeepFace 边界，集中处理 `represent` 调用、bbox 转换、距离计算和阈值来源。CLI、匹配器、参考图加载器、距离调试脚本、人脸裁剪脚本均只依赖该边界，不再直接依赖 DeepFace 原始 API。阈值策略统一为“显式 `--distance-threshold` 覆盖，否则使用 `find_threshold(model_name, distance_metric)`”。

**Tech Stack:** Python 3.13+、DeepFace、NumPy、Pillow、pillow-heif、pytest

---

## 文件结构

### 新增

- `src/hikbox_pictures/deepface_engine.py`
- `tests/test_deepface_engine.py`
- `docs/superpowers/plans/2026-04-10-hikbox-pictures-deepface.md`

### 修改

- `src/hikbox_pictures/cli.py`
- `src/hikbox_pictures/reference_loader.py`
- `src/hikbox_pictures/matcher.py`
- `scripts/inspect_distances.py`
- `scripts/extract_faces.py`
- `pyproject.toml`
- `scripts/install.sh`
- `README.md`
- `tests/test_cli.py`
- `tests/test_reference_loader.py`
- `tests/test_matcher.py`
- `tests/test_inspect_distances.py`
- `tests/test_extract_faces.py`
- `tests/test_repo_samples.py`
- `tests/test_smoke.py`

### 删除

- `src/hikbox_pictures/insightface_engine.py`
- `tests/test_insightface_engine.py`

---

### Task 1: 建立 DeepFace 引擎边界

**Files:**
- Create: `src/hikbox_pictures/deepface_engine.py`
- Test: `tests/test_deepface_engine.py`

- [ ] **Step 1: 先写失败测试，锁定引擎边界行为**

```python
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from hikbox_pictures.deepface_engine import (
    DeepFaceEngine,
    DeepFaceInferenceError,
    DeepFaceInitError,
    DetectedFace,
)


def test_create_uses_default_threshold_from_deepface(monkeypatch) -> None:
    monkeypatch.setattr("hikbox_pictures.deepface_engine._DEEPFACE", object())
    monkeypatch.setattr(
        "hikbox_pictures.deepface_engine._find_threshold",
        lambda model_name, distance_metric: 0.68,
    )

    engine = DeepFaceEngine.create()

    assert engine.model_name == "ArcFace"
    assert engine.detector_backend == "retinaface"
    assert engine.distance_metric == "cosine"
    assert engine.align is True
    assert engine.distance_threshold == pytest.approx(0.68)
    assert engine.threshold_source == "deepface-default"


def test_create_uses_explicit_threshold(monkeypatch) -> None:
    monkeypatch.setattr("hikbox_pictures.deepface_engine._DEEPFACE", object())

    engine = DeepFaceEngine.create(distance_threshold=0.42)

    assert engine.distance_threshold == pytest.approx(0.42)
    assert engine.threshold_source == "explicit"


def test_detect_faces_maps_facial_area_to_tlbr(monkeypatch, tmp_path: Path) -> None:
    image = tmp_path / "sample.jpg"
    image.write_bytes(b"img")

    fake_deepface = SimpleNamespace(
        represent=lambda **kwargs: [
            {
                "embedding": [0.1, 0.2],
                "facial_area": {"x": 10, "y": 20, "w": 30, "h": 40},
            }
        ]
    )
    monkeypatch.setattr("hikbox_pictures.deepface_engine._DEEPFACE", fake_deepface)

    engine = DeepFaceEngine.create(distance_threshold=0.5)
    faces = engine.detect_faces(image)

    assert faces == [DetectedFace(bbox=(20, 40, 60, 10), embedding=np.array([0.1, 0.2], dtype=np.float32))]


def test_distance_uses_configured_metric(monkeypatch) -> None:
    calls: list[tuple[tuple[float, ...], tuple[float, ...], str]] = []

    def fake_find_distance(lhs, rhs, metric):
        calls.append((tuple(lhs), tuple(rhs), metric))
        return 0.33

    monkeypatch.setattr("hikbox_pictures.deepface_engine._DEEPFACE", object())
    monkeypatch.setattr("hikbox_pictures.deepface_engine._find_distance", fake_find_distance)

    engine = DeepFaceEngine.create(distance_metric="euclidean_l2", distance_threshold=0.4)
    result = engine.distance([0.0, 1.0], [1.0, 0.0])

    assert result == pytest.approx(0.33)
    assert calls == [((0.0, 1.0), (1.0, 0.0), "euclidean_l2")]


def test_create_wraps_init_error_when_deepface_missing(monkeypatch) -> None:
    monkeypatch.setattr("hikbox_pictures.deepface_engine._DEEPFACE", None)

    with pytest.raises(DeepFaceInitError, match="deepface 未安装或不可用"):
        DeepFaceEngine.create()


def test_detect_faces_wraps_inference_error(monkeypatch, tmp_path: Path) -> None:
    image = tmp_path / "broken.jpg"
    image.write_bytes(b"img")

    fake_deepface = SimpleNamespace(
        represent=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    monkeypatch.setattr("hikbox_pictures.deepface_engine._DEEPFACE", fake_deepface)

    engine = DeepFaceEngine.create(distance_threshold=0.5)

    with pytest.raises(DeepFaceInferenceError, match="boom"):
        engine.detect_faces(image)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_deepface_engine.py -v`
Expected: FAIL，提示 `ModuleNotFoundError: No module named 'hikbox_pictures.deepface_engine'`

- [ ] **Step 3: 写最小实现，封装 DeepFace 配置、检测和距离**

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np

try:
    from deepface import DeepFace as _DeepFace
    from deepface.modules.verification import find_distance as _find_distance
    from deepface.modules.verification import find_threshold as _find_threshold
except Exception:  # pragma: no cover
    _DeepFace = None
    _find_distance = None
    _find_threshold = None


class DeepFaceInitError(RuntimeError):
    pass


class DeepFaceInferenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class DetectedFace:
    # (top, right, bottom, left)
    bbox: tuple[int, int, int, int]
    embedding: np.ndarray


@dataclass(frozen=True)
class DeepFaceEngine:
    model_name: str
    detector_backend: str
    distance_metric: str
    align: bool
    distance_threshold: float
    threshold_source: str

    @classmethod
    def create(
        cls,
        *,
        model_name: str = "ArcFace",
        detector_backend: str = "retinaface",
        distance_metric: str = "cosine",
        align: bool = True,
        distance_threshold: float | None = None,
    ) -> "DeepFaceEngine":
        if _DeepFace is None or _find_threshold is None or _find_distance is None:
            raise DeepFaceInitError("deepface 未安装或不可用")

        try:
            if distance_threshold is None:
                resolved_threshold = float(_find_threshold(model_name, distance_metric))
                threshold_source = "deepface-default"
            else:
                resolved_threshold = float(distance_threshold)
                threshold_source = "explicit"
        except Exception as exc:
            raise DeepFaceInitError(f"DeepFace 初始化失败: {exc}") from exc

        return cls(
            model_name=model_name,
            detector_backend=detector_backend,
            distance_metric=distance_metric,
            align=align,
            distance_threshold=resolved_threshold,
            threshold_source=threshold_source,
        )

    def detect_faces(self, image_path: Path) -> list[DetectedFace]:
        try:
            raw_results = _DeepFace.represent(
                img_path=str(image_path),
                model_name=self.model_name,
                detector_backend=self.detector_backend,
                enforce_detection=False,
                align=self.align,
            )
            if isinstance(raw_results, dict):
                raw_results = [raw_results]

            faces: list[DetectedFace] = []
            for raw_face in raw_results:
                facial_area = raw_face.get("facial_area") or {}
                x = int(facial_area.get("x", 0))
                y = int(facial_area.get("y", 0))
                w = int(facial_area.get("w", 0))
                h = int(facial_area.get("h", 0))
                faces.append(
                    DetectedFace(
                        bbox=(y, x + w, y + h, x),
                        embedding=np.asarray(raw_face.get("embedding", []), dtype=np.float32),
                    )
                )
            return faces
        except Exception as exc:
            raise DeepFaceInferenceError(f"DeepFace 推理失败: {exc}") from exc

    def distance(self, lhs: Sequence[float], rhs: Sequence[float]) -> float:
        return float(_find_distance(lhs, rhs, self.distance_metric))

    def min_distance(self, embedding: Sequence[float], references: Sequence[Sequence[float]]) -> float:
        if not references:
            return float("inf")
        return min(self.distance(embedding, reference) for reference in references)

    def is_match(self, distance: float) -> bool:
        return distance <= self.distance_threshold
```

- [ ] **Step 4: 运行测试确认通过**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_deepface_engine.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/hikbox_pictures/deepface_engine.py tests/test_deepface_engine.py
git commit -m "feat: add deepface engine boundary"
```

---

### Task 2: 参考图加载器切到 DeepFaceEngine

**Files:**
- Modify: `src/hikbox_pictures/reference_loader.py`
- Test: `tests/test_reference_loader.py`

- [ ] **Step 1: 先写失败测试，覆盖目录扫描和单脸约束**

```python
from pathlib import Path

import numpy as np
import pytest

from hikbox_pictures.deepface_engine import DetectedFace
from hikbox_pictures.reference_loader import ReferenceImageError, load_reference_embeddings


class FakeEngine:
    def __init__(self, mapping):
        self.mapping = mapping

    def detect_faces(self, image_path: Path):
        return self.mapping[image_path]


def _face(embedding: list[float]) -> DetectedFace:
    return DetectedFace(bbox=(1, 2, 3, 4), embedding=np.array(embedding, dtype=np.float32))


def test_load_reference_embeddings_recurses_and_returns_sources(tmp_path: Path) -> None:
    ref_dir = tmp_path / "ref"
    nested = ref_dir / "nested"
    nested.mkdir(parents=True)

    a = ref_dir / "a.jpg"
    b = nested / "b.HEIC"
    ignored = ref_dir / "notes.txt"

    a.write_bytes(b"a")
    b.write_bytes(b"b")
    ignored.write_text("x")

    engine = FakeEngine({a: [_face([0.1])], b: [_face([0.2])]})

    embeddings, sources = load_reference_embeddings(ref_dir, engine)

    assert sources == [a, b]
    assert len(embeddings) == 2
    assert embeddings[0].tolist() == [0.1]
    assert embeddings[1].tolist() == [0.2]


def test_load_reference_embeddings_rejects_zero_or_multiple_faces(tmp_path: Path) -> None:
    ref_dir = tmp_path / "ref"
    ref_dir.mkdir()
    photo = ref_dir / "person.jpg"
    photo.write_bytes(b"x")

    with pytest.raises(ReferenceImageError, match="必须且仅能检测到 1 张人脸"):
        load_reference_embeddings(ref_dir, FakeEngine({photo: []}))

    with pytest.raises(ReferenceImageError, match="必须且仅能检测到 1 张人脸"):
        load_reference_embeddings(ref_dir, FakeEngine({photo: [_face([0.1]), _face([0.2])]}))
```

- [ ] **Step 2: 运行测试确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_reference_loader.py -v`
Expected: FAIL，现有实现仍依赖 `insightface_engine`

- [ ] **Step 3: 切换实现，统一依赖 DeepFaceEngine**

```python
from __future__ import annotations

from pathlib import Path

import numpy as np

from hikbox_pictures.deepface_engine import DeepFaceEngine
from hikbox_pictures.scanner import SUPPORTED_EXTENSIONS


class ReferenceImageError(ValueError):
    pass


def _iter_reference_images(ref_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in ref_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )


def load_reference_embeddings(ref_dir: Path, engine: DeepFaceEngine) -> tuple[list[np.ndarray], list[Path]]:
    source_paths = _iter_reference_images(ref_dir)
    if not source_paths:
        raise ReferenceImageError(f"参考目录 {ref_dir} 没有任何可用参考图")

    embeddings: list[np.ndarray] = []
    for source_path in source_paths:
        faces = engine.detect_faces(source_path)
        if len(faces) != 1:
            raise ReferenceImageError(
                f"参考图 {source_path} 必须且仅能检测到 1 张人脸，实际为 {len(faces)}"
            )
        embeddings.append(np.asarray(faces[0].embedding, dtype=np.float32))

    return embeddings, source_paths
```

- [ ] **Step 4: 运行测试确认通过**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_reference_loader.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/hikbox_pictures/reference_loader.py tests/test_reference_loader.py
git commit -m "refactor: load references via deepface engine"
```

---

### Task 3: 匹配器切换到 DeepFace 距离与阈值

**Files:**
- Modify: `src/hikbox_pictures/matcher.py`
- Test: `tests/test_matcher.py`

- [ ] **Step 1: 先写失败测试，锁定“统一距离/阈值来源 + distinct faces”**

```python
from types import SimpleNamespace

import numpy as np

from hikbox_pictures.matcher import compute_min_distances, evaluate_candidate_photo
from hikbox_pictures.models import CandidatePhoto, MatchBucket


class FakeEngine:
    def __init__(self, faces, threshold=0.5):
        self._faces = faces
        self.distance_threshold = threshold

    def detect_faces(self, _path):
        return self._faces

    def min_distance(self, embedding, refs):
        return min(abs(float(embedding[0]) - float(ref[0])) for ref in refs)

    def is_match(self, distance):
        return distance <= self.distance_threshold


def test_compute_min_distances_uses_engine_distance_logic() -> None:
    faces = [SimpleNamespace(embedding=np.array([1.0], dtype=np.float32))]
    engine = FakeEngine(faces)

    distances = compute_min_distances(
        faces,
        [np.array([1.1], dtype=np.float32)],
        [np.array([3.0], dtype=np.float32)],
        engine=engine,
    )

    assert distances == [(0.1, 2.0)]


def test_evaluate_candidate_photo_requires_distinct_faces(tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "sample.jpg")
    faces = [
        SimpleNamespace(embedding=np.array([0.1], dtype=np.float32), bbox=(0, 10, 10, 0)),
        SimpleNamespace(embedding=np.array([1.0], dtype=np.float32), bbox=(0, 10, 10, 0)),
    ]

    evaluation = evaluate_candidate_photo(
        photo,
        [np.array([0.0], dtype=np.float32)],
        [np.array([1.0], dtype=np.float32)],
        engine=FakeEngine(faces, threshold=0.2),
    )

    assert evaluation.detected_face_count == 2
    assert evaluation.bucket is MatchBucket.ONLY_TWO
```

- [ ] **Step 2: 运行测试确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_matcher.py -v`
Expected: FAIL，现有逻辑仍绑定 `insightface` 距离常量与旧参数

- [ ] **Step 3: 实现匹配器改造，复用引擎阈值与距离**

```python
from __future__ import annotations

from collections.abc import Sequence

from hikbox_pictures.deepface_engine import DeepFaceEngine
from hikbox_pictures.models import CandidatePhoto, MatchBucket, PhotoEvaluation


class CandidateDecodeError(RuntimeError):
    pass


def compute_min_distances(
    faces: Sequence,
    ref_a_embeddings: Sequence[Sequence[float]],
    ref_b_embeddings: Sequence[Sequence[float]],
    *,
    engine: DeepFaceEngine,
) -> list[tuple[float, float]]:
    return [
        (
            engine.min_distance(face.embedding, ref_a_embeddings),
            engine.min_distance(face.embedding, ref_b_embeddings),
        )
        for face in faces
    ]


def evaluate_candidate_photo(
    photo: CandidatePhoto,
    person_a_embeddings: Sequence[Sequence[float]],
    person_b_embeddings: Sequence[Sequence[float]],
    *,
    engine: DeepFaceEngine,
) -> PhotoEvaluation:
    try:
        faces = engine.detect_faces(photo.path)
    except Exception as exc:  # pragma: no cover
        raise CandidateDecodeError(f"Failed to decode {photo.path}: {exc}") from exc

    if not faces:
        return PhotoEvaluation(candidate=photo, detected_face_count=0, bucket=None)

    distances = compute_min_distances(faces, person_a_embeddings, person_b_embeddings, engine=engine)
    matches_a = {index for index, (dist_a, _) in enumerate(distances) if engine.is_match(dist_a)}
    matches_b = {index for index, (_, dist_b) in enumerate(distances) if engine.is_match(dist_b)}

    # 其余逻辑保持：distinct faces + 大额外人脸判定
```

- [ ] **Step 4: 运行测试确认通过**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_matcher.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/hikbox_pictures/matcher.py tests/test_matcher.py
git commit -m "refactor: use deepface distance in matcher"
```

---

### Task 4: CLI 新增 DeepFace 调参参数并注入统一引擎

**Files:**
- Modify: `src/hikbox_pictures/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: 先写失败测试，锁定 CLI 参数与引擎初始化参数透传**

```python
def test_main_passes_deepface_runtime_options(monkeypatch, tmp_path) -> None:
    from hikbox_pictures.cli import main

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    ref_a_dir = tmp_path / "ref-a"
    ref_b_dir = tmp_path / "ref-b"
    input_dir.mkdir()
    output_dir.mkdir()
    ref_a_dir.mkdir()
    ref_b_dir.mkdir()

    create_calls = []

    def fake_create(**kwargs):
        create_calls.append(kwargs)
        return object()

    monkeypatch.setattr("hikbox_pictures.cli.DeepFaceEngine.create", fake_create)
    monkeypatch.setattr("hikbox_pictures.cli.load_reference_embeddings", lambda path, engine: ([[0.1]], [path / "a.jpg"]))
    monkeypatch.setattr("hikbox_pictures.cli.iter_candidate_photos", lambda _: iter([]))

    code = main(
        [
            "--input", str(input_dir),
            "--ref-a-dir", str(ref_a_dir),
            "--ref-b-dir", str(ref_b_dir),
            "--output", str(output_dir),
            "--model-name", "Facenet512",
            "--detector-backend", "mtcnn",
            "--distance-metric", "euclidean_l2",
            "--distance-threshold", "0.37",
            "--no-align",
        ]
    )

    assert code == 0
    assert create_calls == [
        {
            "model_name": "Facenet512",
            "detector_backend": "mtcnn",
            "distance_metric": "euclidean_l2",
            "align": False,
            "distance_threshold": 0.37,
        }
    ]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_cli.py -v`
Expected: FAIL，当前 CLI 还没有上述参数

- [ ] **Step 3: 修改 CLI，新增参数并传入 DeepFaceEngine.create**

```python
from hikbox_pictures.deepface_engine import DeepFaceEngine, DeepFaceInitError


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
    parser.add_argument("--align", dest="align", action="store_true", default=True)
    parser.add_argument("--no-align", dest="align", action="store_false")
    return parser


# main 内
engine = DeepFaceEngine.create(
    model_name=args.model_name,
    detector_backend=args.detector_backend,
    distance_metric=args.distance_metric,
    align=args.align,
    distance_threshold=args.distance_threshold,
)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/hikbox_pictures/cli.py tests/test_cli.py
git commit -m "feat: add deepface runtime options to cli"
```

---

### Task 5: 距离调试脚本与主流程参数对齐

**Files:**
- Modify: `scripts/inspect_distances.py`
- Test: `tests/test_inspect_distances.py`

- [ ] **Step 1: 先写失败测试，锁定配置打印与阈值来源行为**

```python
def test_main_prints_deepface_runtime_config(monkeypatch, tmp_path, capsys) -> None:
    script = _load_script_module()

    input_dir = tmp_path / "input"
    ref_a_dir = tmp_path / "ref-a"
    ref_b_dir = tmp_path / "ref-b"
    input_dir.mkdir()
    ref_a_dir.mkdir()
    ref_b_dir.mkdir()

    candidate = input_dir / "c.jpg"
    candidate.write_bytes(b"x")

    class FakeEngine:
        model_name = "ArcFace"
        detector_backend = "retinaface"
        distance_metric = "cosine"
        align = True
        distance_threshold = 0.66
        threshold_source = "deepface-default"

        def detect_faces(self, _path):
            return [type("Face", (), {"bbox": (1, 10, 11, 0), "embedding": [0.2]})()]

        def min_distance(self, embedding, refs):
            return 0.22 if refs == [[0.2]] else 0.88

        def is_match(self, distance):
            return distance <= self.distance_threshold

    monkeypatch.setattr(script.DeepFaceEngine, "create", lambda **kwargs: FakeEngine())
    monkeypatch.setattr(script, "load_reference_embeddings", lambda path, engine: ([[0.2]], [path / "a.jpg"]))
    monkeypatch.setattr(script, "iter_candidate_photos", lambda root: iter([CandidatePhoto(path=candidate)]))

    code = script.main([
        "--input", str(input_dir),
        "--ref-a-dir", str(ref_a_dir),
        "--ref-b-dir", str(ref_b_dir),
    ])

    out = capsys.readouterr().out
    assert code == 0
    assert "model_name: ArcFace" in out
    assert "detector_backend: retinaface" in out
    assert "distance_metric: cosine" in out
    assert "align: True" in out
    assert "distance_threshold: 0.6600 (deepface-default)" in out
```

- [ ] **Step 2: 运行测试确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_inspect_distances.py -v`
Expected: FAIL，现有脚本仍使用 `--tolerance` 和旧输出格式

- [ ] **Step 3: 修改脚本，复用 DeepFaceEngine 配置和阈值语义**

```python
from hikbox_pictures.deepface_engine import DeepFaceEngine, DeepFaceInitError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="inspect_distances")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--ref-a-dir", required=True, type=Path)
    parser.add_argument("--ref-b-dir", required=True, type=Path)
    parser.add_argument("--model-name", default="ArcFace")
    parser.add_argument("--detector-backend", default="retinaface")
    parser.add_argument("--distance-metric", default="cosine")
    parser.add_argument("--distance-threshold", type=float)
    parser.add_argument("--align", dest="align", action="store_true", default=True)
    parser.add_argument("--no-align", dest="align", action="store_false")
    parser.add_argument("--annotated-dir", type=Path)
    return parser


# main 内打印
print(f"model_name: {engine.model_name}")
print(f"detector_backend: {engine.detector_backend}")
print(f"distance_metric: {engine.distance_metric}")
print(f"align: {engine.align}")
print(f"distance_threshold: {engine.distance_threshold:.4f} ({engine.threshold_source})")

# 匹配判断
match_a = "Y" if engine.is_match(distance_a) else "N"
match_b = "Y" if engine.is_match(distance_b) else "N"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_inspect_distances.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add scripts/inspect_distances.py tests/test_inspect_distances.py
git commit -m "feat: migrate inspect_distances to deepface options"
```

---

### Task 6: 人脸裁剪脚本切换 DeepFace 检测源

**Files:**
- Modify: `scripts/extract_faces.py`
- Test: `tests/test_extract_faces.py`

- [ ] **Step 1: 先写失败测试，锁定脚本改为 DeepFaceEngine**

```python
def test_main_uses_deepface_engine(monkeypatch, tmp_path, capsys) -> None:
    script = _load_script_module()

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    photo = input_dir / "a.jpg"
    Image.new("RGB", (32, 32), color="white").save(photo)

    class FakeEngine:
        def detect_faces(self, image_path):
            return [SimpleNamespace(bbox=(4, 24, 24, 4))]

    monkeypatch.setattr(script.DeepFaceEngine, "create", lambda **kwargs: FakeEngine())
    monkeypatch.setattr(script, "load_rgb_image", lambda _path: np.full((32, 32, 3), 120, dtype=np.uint8))

    code = script.main(["--input", str(input_dir), "--output", str(output_dir)])

    out = capsys.readouterr().out
    assert code == 0
    assert "输出人脸数: 1" in out
    assert (output_dir / "a__face_01.png").is_file()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_extract_faces.py -v`
Expected: FAIL，脚本仍导入 `InsightFaceEngine`

- [ ] **Step 3: 修改脚本导入与初始化，保留裁剪算法不变**

```python
from hikbox_pictures.deepface_engine import DeepFaceEngine, DeepFaceInitError


try:
    engine = DeepFaceEngine.create()
except DeepFaceInitError as exc:
    print(str(exc), file=sys.stderr)
    return 2

# 检测与 bbox 使用保持不变
locations = [tuple(face.bbox) for face in engine.detect_faces(image_path)]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_extract_faces.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add scripts/extract_faces.py tests/test_extract_faces.py
git commit -m "refactor: switch extract_faces to deepface engine"
```

---

### Task 7: 依赖与文档迁移到 DeepFace

**Files:**
- Modify: `pyproject.toml`
- Modify: `scripts/install.sh`
- Modify: `README.md`
- Test: `tests/test_repo_samples.py`
- Test: `tests/test_smoke.py`

- [ ] **Step 1: 先写失败测试，锁定 README 和依赖断言**

```python
def test_readme_mentions_deepface_runtime_flags() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "deepface" in readme
    assert "--model-name" in readme
    assert "--detector-backend" in readme
    assert "--distance-metric" in readme
    assert "--distance-threshold" in readme
    assert "--align" in readme
    assert "--no-align" in readme
    assert "insightface" not in readme


def test_pyproject_dependencies_use_deepface() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    deps = pyproject["project"]["dependencies"]
    assert any(dep.startswith("deepface") for dep in deps)
    assert all(not dep.startswith("insightface") for dep in deps)
    assert all(not dep.startswith("onnxruntime") for dep in deps)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_repo_samples.py tests/test_smoke.py -v`
Expected: FAIL，文档和依赖仍是 `insightface` 路线

- [ ] **Step 3: 更新依赖、安装脚本与 README**

```toml
[project]
dependencies = [
  "deepface>=0.0.93",
  "numpy>=1.26.0",
  "Pillow>=10.0.0",
  "pillow-heif>=0.18.0",
]
```

```bash
echo "[hikbox-pictures] 安装项目及开发依赖（包含 deepface）"
if ! python3 -m pip install -e '.[dev]'; then
  cat >&2 <<'ERR'
安装失败。

请确认：
1) Python 版本满足 3.13+
2) 网络可用（首次运行 deepface 相关模型下载需联网）
3) 系统依赖安装完整（如 TensorFlow / OpenCV 的平台依赖）
ERR
  exit 1
fi
```

```md
hikbox-pictures \
  --input /path/to/photo-library \
  --ref-a-dir /path/to/person-a-dir \
  --ref-b-dir /path/to/person-b-dir \
  --output /path/to/output \
  --model-name ArcFace \
  --detector-backend retinaface \
  --distance-metric cosine \
  --align

首次运行可能触发模型下载，需联网，首次启动会明显慢于后续运行。
```

- [ ] **Step 4: 运行测试确认通过**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_repo_samples.py tests/test_smoke.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add pyproject.toml scripts/install.sh README.md tests/test_repo_samples.py tests/test_smoke.py
git commit -m "chore: migrate dependencies and docs to deepface"
```

---

### Task 8: 清理旧 InsightFace 边界并完成回归验证

**Files:**
- Delete: `src/hikbox_pictures/insightface_engine.py`
- Delete: `tests/test_insightface_engine.py`
- Modify: `tests/test_matcher.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_inspect_distances.py`
- Modify: `tests/test_extract_faces.py`

- [ ] **Step 1: 先写失败测试，确保仓库不再依赖 InsightFace 模块**

```python
def test_codebase_no_longer_references_insightface_imports() -> None:
    import subprocess

    result = subprocess.run(
        ["rg", "insightface_engine|InsightFaceEngine", "src", "scripts", "tests"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_cli.py tests/test_matcher.py tests/test_inspect_distances.py tests/test_extract_faces.py -v`
Expected: FAIL，尚有旧导入

- [ ] **Step 3: 删除旧模块并完成测试替换**

```bash
rm src/hikbox_pictures/insightface_engine.py
rm tests/test_insightface_engine.py
```

```python
# 所有测试中的导入改为：
from hikbox_pictures.deepface_engine import DeepFaceEngine
```

- [ ] **Step 4: 运行受影响测试 + 全量测试 + 一次 CLI 冒烟**

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest tests/test_deepface_engine.py tests/test_reference_loader.py tests/test_matcher.py tests/test_cli.py tests/test_inspect_distances.py tests/test_extract_faces.py -q`
Expected: PASS

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m pytest -q`
Expected: PASS

Run: `source .venv/bin/activate && PYTHONPATH=src python3 -m hikbox_pictures.cli --input tests/data --ref-a-dir tests/data --ref-b-dir tests/data --output /tmp/hikbox-pictures-output --distance-threshold 0.5 --no-align`
Expected: 程序可执行；若参考图不满足“单脸”规则，输出明确错误信息并返回非零

- [ ] **Step 5: 提交**

```bash
git add -A
git commit -m "refactor: complete deepface migration and remove insightface"
```

---

## 自检

### 1. Spec coverage

- `DeepFaceEngine` 统一边界：Task 1
- 参考图目录加载与单脸校验：Task 2
- 多参考图最小距离 + distinct faces 约束：Task 3
- CLI 新参数：`--model-name` / `--detector-backend` / `--distance-metric` / `--distance-threshold` / `--align|--no-align`：Task 4
- 距离调试脚本输出配置与阈值来源：Task 5
- 人脸裁剪脚本切换到 DeepFace 检测：Task 6
- 依赖、安装脚本、README 迁移：Task 7
- 清理旧 InsightFace 代码并回归验证：Task 8

### 2. Placeholder scan

- 无 `TBD`、`TODO`、`implement later`
- 每个任务都包含明确文件、命令、预期结果与关键代码

### 3. Type consistency

- 检测结果统一使用 `DetectedFace(bbox=(top,right,bottom,left), embedding=np.ndarray)`
- 距离与阈值统一从 `DeepFaceEngine` 读取，不再在 `matcher.py` 写死默认阈值
- 主 CLI 与 `inspect_distances.py` 参数集合保持一致
