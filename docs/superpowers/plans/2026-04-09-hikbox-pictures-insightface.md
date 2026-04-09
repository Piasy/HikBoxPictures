# HikBox Pictures InsightFace 迁移 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 将当前基于 `face_recognition` 的单参考图流程迁移到 `InsightFace + ArcFace` 的双目录多参考图流程，并保持输出目录结构、Live Photo `MOV` 复制与时间元数据行为不变。

**Architecture:** 新增 `insightface_engine.py` 作为唯一引擎边界，CLI、参考加载、匹配器、距离调试脚本、人脸裁剪脚本全部经过该边界拿到统一的检测结果。匹配层改为每张候选脸到 A/B 参考组的最小距离判定，并将距离计算函数与默认阈值放到 `matcher.py` 统一复用。

**Tech Stack:** Python 3.13+、InsightFace、ONNX Runtime、NumPy、Pillow、pillow-heif、pytest

---

## 文件结构

### 新增

- `src/hikbox_pictures/insightface_engine.py`
- `tests/test_insightface_engine.py`
- `tests/test_extract_faces.py`

### 修改

- `src/hikbox_pictures/reference_loader.py`
- `src/hikbox_pictures/matcher.py`
- `src/hikbox_pictures/cli.py`
- `scripts/inspect_distances.py`
- `scripts/extract_faces.py`
- `pyproject.toml`
- `scripts/install.sh`
- `README.md`
- `tests/test_reference_loader.py`
- `tests/test_matcher.py`
- `tests/test_cli.py`
- `tests/test_inspect_distances.py`
- `tests/test_repo_samples.py`
- `tests/test_smoke.py`

---

### Task 1: 建立 InsightFace 引擎边界

**Files:**
- Create: `src/hikbox_pictures/insightface_engine.py`
- Test: `tests/test_insightface_engine.py`

- [x] **Step 1: 先写失败测试**

```python
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from hikbox_pictures.insightface_engine import InsightFaceEngine, InsightFaceInferenceError, InsightFaceInitError


def test_create_uses_default_model_and_provider(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeFaceAnalysis:
        def __init__(self, *, name, providers):
            calls['name'] = name
            calls['providers'] = providers

        def prepare(self, ctx_id=0, det_size=(640, 640)):
            calls['ctx_id'] = ctx_id
            calls['det_size'] = det_size

    monkeypatch.setattr('hikbox_pictures.insightface_engine.FaceAnalysis', FakeFaceAnalysis)

    engine = InsightFaceEngine.create()

    assert calls['name'] == 'antelopev2'
    assert calls['providers'] == ['CPUExecutionProvider']
    assert calls['ctx_id'] == 0
    assert calls['det_size'] == (640, 640)
    assert engine is not None


def test_detect_faces_maps_bbox_and_embedding(monkeypatch, tmp_path: Path) -> None:
    class FakeFace:
        bbox = np.array([10.2, 20.3, 110.4, 220.5], dtype=np.float32)
        normed_embedding = np.array([0.1, 0.2, 0.3], dtype=np.float32)

    class FakeAnalyzer:
        def get(self, image):
            return [FakeFace()]

    monkeypatch.setattr('hikbox_pictures.insightface_engine.load_rgb_image', lambda _: 'image')

    faces = InsightFaceEngine(FakeAnalyzer()).detect_faces(tmp_path / 'sample.jpg')

    assert len(faces) == 1
    assert faces[0].bbox == (20, 110, 220, 10)
    assert faces[0].embedding.tolist() == [0.1, 0.2, 0.3]


def test_create_wraps_init_error(monkeypatch) -> None:
    class FakeFaceAnalysis:
        def __init__(self, *, name, providers):
            raise RuntimeError('init failed')

    monkeypatch.setattr('hikbox_pictures.insightface_engine.FaceAnalysis', FakeFaceAnalysis)

    with pytest.raises(InsightFaceInitError, match='init failed'):
        InsightFaceEngine.create()


def test_detect_faces_wraps_infer_error(monkeypatch, tmp_path: Path) -> None:
    class FakeAnalyzer:
        def get(self, image):
            raise RuntimeError('infer failed')

    monkeypatch.setattr('hikbox_pictures.insightface_engine.load_rgb_image', lambda _: 'image')

    with pytest.raises(InsightFaceInferenceError, match='infer failed'):
        InsightFaceEngine(FakeAnalyzer()).detect_faces(tmp_path / 'sample.jpg')
```

- [x] **Step 2: 运行测试确认失败**

Run: `PYTHONPATH=src python3 -m pytest tests/test_insightface_engine.py -v`
Expected: FAIL，`ModuleNotFoundError`

- [x] **Step 3: 写最小实现**

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from insightface.app import FaceAnalysis

from hikbox_pictures.image_io import load_rgb_image


class InsightFaceInitError(RuntimeError):
    pass


class InsightFaceInferenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class DetectedFace:
    bbox: tuple[int, int, int, int]
    embedding: np.ndarray


class InsightFaceEngine:
    def __init__(self, analyzer: Any):
        self._analyzer = analyzer

    @classmethod
    def create(
        cls,
        *,
        model_name: str = 'antelopev2',
        providers: list[str] | None = None,
        det_size: tuple[int, int] = (640, 640),
    ) -> 'InsightFaceEngine':
        runtime_providers = providers or ['CPUExecutionProvider']
        try:
            analyzer = FaceAnalysis(name=model_name, providers=runtime_providers)
            analyzer.prepare(ctx_id=0, det_size=det_size)
        except Exception as exc:  # pragma: no cover
            raise InsightFaceInitError(f'初始化 InsightFace 失败: {exc}') from exc
        return cls(analyzer)

    def detect_faces(self, image_path: Path) -> list[DetectedFace]:
        try:
            image = load_rgb_image(image_path)
            raw_faces = self._analyzer.get(image)
        except Exception as exc:
            raise InsightFaceInferenceError(f'处理图片失败 {image_path}: {exc}') from exc

        faces: list[DetectedFace] = []
        for raw_face in raw_faces:
            left, top, right, bottom = raw_face.bbox.tolist()
            faces.append(
                DetectedFace(
                    bbox=(int(round(top)), int(round(right)), int(round(bottom)), int(round(left))),
                    embedding=np.asarray(raw_face.normed_embedding, dtype=np.float32),
                )
            )
        return faces
```

- [x] **Step 4: 再跑测试确认通过**

Run: `PYTHONPATH=src python3 -m pytest tests/test_insightface_engine.py -v`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add src/hikbox_pictures/insightface_engine.py tests/test_insightface_engine.py
git commit -m 'feat: add insightface engine boundary'
```

---

### Task 2: 参考图目录加载与单脸校验

**Files:**
- Modify: `src/hikbox_pictures/reference_loader.py`
- Test: `tests/test_reference_loader.py`

- [x] **Step 1: 写失败测试**

```python
from __future__ import annotations

import numpy as np
import pytest

from hikbox_pictures.reference_loader import ReferenceImageError, load_reference_embeddings


class FakeFace:
    def __init__(self, embedding):
        self.embedding = embedding


class FakeEngine:
    def __init__(self, mapping):
        self.mapping = mapping

    def detect_faces(self, path):
        return self.mapping[path]


def test_load_reference_embeddings_recurses_and_filters(tmp_path) -> None:
    ref_dir = tmp_path / 'ref'
    nested = ref_dir / 'nested'
    nested.mkdir(parents=True)

    a = ref_dir / 'a.jpg'
    b = nested / 'b.HEIC'
    ignored = ref_dir / 'notes.txt'

    a.write_bytes(b'a')
    b.write_bytes(b'b')
    ignored.write_text('x')

    engine = FakeEngine(
        {
            a: [FakeFace(np.array([0.1], dtype=np.float32))],
            b: [FakeFace(np.array([0.2], dtype=np.float32))],
        }
    )

    embeddings, sources = load_reference_embeddings(ref_dir, engine)

    assert len(embeddings) == 2
    assert sources == [a, b]


def test_load_reference_embeddings_rejects_empty(tmp_path) -> None:
    ref_dir = tmp_path / 'ref'
    ref_dir.mkdir()

    with pytest.raises(ReferenceImageError, match='没有任何可用参考图'):
        load_reference_embeddings(ref_dir, FakeEngine({}))


def test_load_reference_embeddings_rejects_not_single_face(tmp_path) -> None:
    ref_dir = tmp_path / 'ref'
    ref_dir.mkdir()
    one = ref_dir / 'one.jpg'
    one.write_bytes(b'x')

    with pytest.raises(ReferenceImageError, match='必须且仅能检测到 1 张人脸'):
        load_reference_embeddings(ref_dir, FakeEngine({one: []}))
```

- [x] **Step 2: 运行确认失败**

Run: `PYTHONPATH=src python3 -m pytest tests/test_reference_loader.py -v`
Expected: FAIL，旧接口不满足目录语义

- [x] **Step 3: 实现目录加载**

```python
from __future__ import annotations

from pathlib import Path

import numpy as np

from hikbox_pictures.insightface_engine import InsightFaceEngine
from hikbox_pictures.scanner import SUPPORTED_EXTENSIONS


class ReferenceImageError(ValueError):
    pass


def _iter_reference_images(ref_dir: Path) -> list[Path]:
    return [
        path
        for path in sorted(ref_dir.rglob('*'))
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    ]


def load_reference_embeddings(ref_dir: Path, engine: InsightFaceEngine) -> tuple[list[np.ndarray], list[Path]]:
    image_paths = _iter_reference_images(ref_dir)
    if not image_paths:
        raise ReferenceImageError(f'参考目录 {ref_dir} 没有任何可用参考图')

    embeddings: list[np.ndarray] = []
    sources: list[Path] = []

    for image_path in image_paths:
        faces = engine.detect_faces(image_path)
        if len(faces) != 1:
            raise ReferenceImageError(f'参考图 {image_path} 必须且仅能检测到 1 张人脸，实际为 {len(faces)}')
        embeddings.append(np.asarray(faces[0].embedding, dtype=np.float32))
        sources.append(image_path)

    return embeddings, sources
```

- [x] **Step 4: 再跑测试**

Run: `PYTHONPATH=src python3 -m pytest tests/test_reference_loader.py -v`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add src/hikbox_pictures/reference_loader.py tests/test_reference_loader.py
git commit -m 'feat: load references from directories'
```

---

### Task 3: 匹配器改为最小距离

**Files:**
- Modify: `src/hikbox_pictures/matcher.py`
- Test: `tests/test_matcher.py`

- [x] **Step 1: 写失败测试**

```python
from __future__ import annotations

import numpy as np

from hikbox_pictures.matcher import compute_min_distances, evaluate_candidate_photo
from hikbox_pictures.models import CandidatePhoto, MatchBucket


class FakeFace:
    def __init__(self, embedding):
        self.embedding = embedding


class FakeEngine:
    def __init__(self, faces):
        self.faces = faces

    def detect_faces(self, path):
        return self.faces


def test_compute_min_distances_uses_min_value() -> None:
    faces = [FakeFace(np.array([1.0, 0.0], dtype=np.float32))]
    a_refs = [np.array([1.0, 0.0], dtype=np.float32), np.array([-1.0, 0.0], dtype=np.float32)]
    b_refs = [np.array([0.0, 1.0], dtype=np.float32)]

    distances = compute_min_distances(faces, a_refs, b_refs)

    assert distances[0][0] == 0.0
    assert distances[0][1] > 1.0


def test_evaluate_candidate_photo_requires_distinct_faces(tmp_path) -> None:
    candidate = CandidatePhoto(path=tmp_path / 'sample.jpg')
    faces = [
        FakeFace(np.array([1.0, 0.0], dtype=np.float32)),
        FakeFace(np.array([-1.0, 0.0], dtype=np.float32)),
    ]

    evaluation = evaluate_candidate_photo(
        candidate,
        FakeEngine(faces),
        [np.array([1.0, 0.0], dtype=np.float32)],
        [np.array([-1.0, 0.0], dtype=np.float32)],
        tolerance=0.1,
    )

    assert evaluation.bucket is MatchBucket.ONLY_TWO
```

- [x] **Step 2: 运行确认失败**

Run: `PYTHONPATH=src python3 -m pytest tests/test_matcher.py -v`
Expected: FAIL，旧签名和旧行为

- [x] **Step 3: 实现最小距离匹配器**

```python
from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from hikbox_pictures.insightface_engine import InsightFaceEngine
from hikbox_pictures.models import CandidatePhoto, MatchBucket, PhotoEvaluation

DEFAULT_DISTANCE_THRESHOLD = 1.0


class CandidateDecodeError(RuntimeError):
    pass


def _l2_distance(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.linalg.norm(a - b))


def compute_min_distances(faces: Sequence, ref_a_embeddings: Sequence[np.ndarray], ref_b_embeddings: Sequence[np.ndarray]) -> list[tuple[float, float]]:
    results: list[tuple[float, float]] = []
    for face in faces:
        min_a = min(_l2_distance(face.embedding, ref) for ref in ref_a_embeddings)
        min_b = min(_l2_distance(face.embedding, ref) for ref in ref_b_embeddings)
        results.append((min_a, min_b))
    return results


def _has_distinct_matches(matches_a: set[int], matches_b: set[int]) -> bool:
    return any(index_a != index_b for index_a in matches_a for index_b in matches_b)


def evaluate_candidate_photo(
    photo: CandidatePhoto,
    engine: InsightFaceEngine,
    ref_a_embeddings: Sequence[np.ndarray],
    ref_b_embeddings: Sequence[np.ndarray],
    *,
    tolerance: float = DEFAULT_DISTANCE_THRESHOLD,
) -> PhotoEvaluation:
    try:
        faces = engine.detect_faces(photo.path)
    except Exception as exc:
        raise CandidateDecodeError(f'Failed to decode {photo.path}: {exc}') from exc

    if not faces:
        return PhotoEvaluation(candidate=photo, detected_face_count=0, bucket=None)

    distances = compute_min_distances(faces, ref_a_embeddings, ref_b_embeddings)
    matches_a = {index for index, (dist_a, _) in enumerate(distances) if dist_a <= tolerance}
    matches_b = {index for index, (_, dist_b) in enumerate(distances) if dist_b <= tolerance}

    if not matches_a or not matches_b or not _has_distinct_matches(matches_a, matches_b):
        return PhotoEvaluation(candidate=photo, detected_face_count=len(faces), bucket=None)

    bucket = MatchBucket.ONLY_TWO if len(faces) == 2 else MatchBucket.GROUP
    return PhotoEvaluation(candidate=photo, detected_face_count=len(faces), bucket=bucket)
```

- [x] **Step 4: 再跑测试**

Run: `PYTHONPATH=src python3 -m pytest tests/test_matcher.py -v`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add src/hikbox_pictures/matcher.py tests/test_matcher.py
git commit -m 'feat: switch matcher to min distance'
```

---

### Task 4: CLI 切换到目录参数与单引擎实例

**Files:**
- Modify: `src/hikbox_pictures/cli.py`
- Test: `tests/test_cli.py`

- [x] **Step 1: 写失败测试**

```python
def _build_argv(input_dir, ref_a_dir, ref_b_dir, output_dir) -> list[str]:
    return [
        '--input',
        str(input_dir),
        '--ref-a-dir',
        str(ref_a_dir),
        '--ref-b-dir',
        str(ref_b_dir),
        '--output',
        str(output_dir),
    ]


def test_main_fails_when_reference_is_not_directory(tmp_path, capsys) -> None:
    input_dir = tmp_path / 'input'
    output_dir = tmp_path / 'output'
    ref_a = tmp_path / 'a.jpg'
    ref_b = tmp_path / 'ref-b'

    input_dir.mkdir()
    output_dir.mkdir()
    ref_a.write_bytes(b'a')
    ref_b.mkdir()

    exit_code = main(_build_argv(input_dir, ref_a, ref_b, output_dir))

    assert exit_code == 2
    assert '参考路径必须是目录' in capsys.readouterr().err
```

- [x] **Step 2: 运行确认失败**

Run: `PYTHONPATH=src python3 -m pytest tests/test_cli.py -v`
Expected: FAIL，旧参数仍是 `--ref-a` / `--ref-b`

- [x] **Step 3: 实现 CLI 改造**

```python
from hikbox_pictures.insightface_engine import InsightFaceEngine, InsightFaceInitError
from hikbox_pictures.reference_loader import ReferenceImageError, load_reference_embeddings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='hikbox-pictures')
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--ref-a-dir', required=True, type=Path)
    parser.add_argument('--ref-b-dir', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    return parser


def _validate_input_paths(input_path: Path, ref_a_dir: Path, ref_b_dir: Path) -> str | None:
    if not input_path.exists():
        return f'路径不存在: {input_path}'
    for ref_dir in (ref_a_dir, ref_b_dir):
        if not ref_dir.exists():
            return f'路径不存在: {ref_dir}'
        if not ref_dir.is_dir():
            return f'参考路径必须是目录: {ref_dir}'
    return None


# main 内
# 1) engine = InsightFaceEngine.create()
# 2) person_a_embeddings, _ = load_reference_embeddings(args.ref_a_dir, engine)
# 3) person_b_embeddings, _ = load_reference_embeddings(args.ref_b_dir, engine)
# 4) evaluate_candidate_photo(candidate, engine, person_a_embeddings, person_b_embeddings)
```

- [x] **Step 4: 再跑测试**

Run: `PYTHONPATH=src python3 -m pytest tests/test_cli.py -v`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add src/hikbox_pictures/cli.py tests/test_cli.py
git commit -m 'feat: migrate CLI to directory references'
```

---

### Task 5: 调试脚本切换目录输入并复用匹配距离

**Files:**
- Modify: `scripts/inspect_distances.py`
- Test: `tests/test_inspect_distances.py`

- [x] **Step 1: 写失败测试**

```python
exit_code = script.main(
    [
        '--input',
        str(input_dir),
        '--ref-a-dir',
        str(ref_a_dir),
        '--ref-b-dir',
        str(ref_b_dir),
        '--annotated-dir',
        str(annotated_dir),
    ]
)

assert exit_code == 0
assert 'dist_a=' in output
assert 'dist_b=' in output
```

- [x] **Step 2: 运行确认失败**

Run: `PYTHONPATH=src python3 -m pytest tests/test_inspect_distances.py -v`
Expected: FAIL，脚本仍使用旧参数

- [x] **Step 3: 实现脚本改造**

```python
from hikbox_pictures.insightface_engine import InsightFaceEngine
from hikbox_pictures.matcher import DEFAULT_DISTANCE_THRESHOLD, compute_min_distances
from hikbox_pictures.reference_loader import ReferenceImageError, load_reference_embeddings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='inspect_distances')
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--ref-a-dir', required=True, type=Path)
    parser.add_argument('--ref-b-dir', required=True, type=Path)
    parser.add_argument('--tolerance', type=float, default=DEFAULT_DISTANCE_THRESHOLD)
    parser.add_argument('--annotated-dir', type=Path)
    return parser


# main 内
# 1) engine = InsightFaceEngine.create()
# 2) ref_a_embeddings, _ = load_reference_embeddings(args.ref_a_dir, engine)
# 3) ref_b_embeddings, _ = load_reference_embeddings(args.ref_b_dir, engine)
# 4) faces = engine.detect_faces(candidate.path)
# 5) distances = compute_min_distances(faces, ref_a_embeddings, ref_b_embeddings)
```

- [x] **Step 4: 再跑测试**

Run: `PYTHONPATH=src python3 -m pytest tests/test_inspect_distances.py -v`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add scripts/inspect_distances.py tests/test_inspect_distances.py
git commit -m 'feat: migrate distance inspector to directory refs'
```

---

### Task 6: 人脸裁剪脚本切换新引擎检测

**Files:**
- Modify: `scripts/extract_faces.py`
- Create: `tests/test_extract_faces.py`

- [x] **Step 1: 写失败测试**

```python
from __future__ import annotations

import importlib.util
from pathlib import Path

from PIL import Image

SCRIPT_PATH = Path(__file__).resolve().parent.parent / 'scripts' / 'extract_faces.py'


def _load_script_module():
    spec = importlib.util.spec_from_file_location('extract_faces_script', SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_main_uses_engine_detect_faces(monkeypatch, tmp_path, capsys) -> None:
    script = _load_script_module()

    input_dir = tmp_path / 'input'
    output_dir = tmp_path / 'output'
    input_dir.mkdir()

    source = input_dir / 'a.jpg'
    Image.new('RGB', (64, 64), color='white').save(source)

    class Face:
        bbox = (10, 40, 40, 10)

    class Engine:
        def detect_faces(self, path):
            return [Face()]

    monkeypatch.setattr(script.InsightFaceEngine, 'create', lambda: Engine())

    code = script.main(['--input', str(input_dir), '--output', str(output_dir)])

    assert code == 0
    assert '输出人脸数: 1' in capsys.readouterr().out
    assert (output_dir / 'a__face_01.png').is_file()
```

- [x] **Step 2: 运行确认失败**

Run: `PYTHONPATH=src python3 -m pytest tests/test_extract_faces.py -v`
Expected: FAIL，旧脚本未使用新引擎

- [x] **Step 3: 实现脚本改造**

```python
from hikbox_pictures.insightface_engine import InsightFaceEngine, InsightFaceInitError


# main 内
try:
    engine = InsightFaceEngine.create()
except InsightFaceInitError as exc:
    print(str(exc), file=sys.stderr)
    return 2

# 每张图
# faces = engine.detect_faces(image_path)
# locations = [face.bbox for face in faces]
# 其余裁剪逻辑保持不变
```

- [x] **Step 4: 再跑测试**

Run: `PYTHONPATH=src python3 -m pytest tests/test_extract_faces.py -v`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add scripts/extract_faces.py tests/test_extract_faces.py
git commit -m 'feat: migrate extract_faces to insightface detection'
```

---

### Task 7: 依赖、安装脚本、README 同步迁移

**Files:**
- Modify: `pyproject.toml`
- Modify: `scripts/install.sh`
- Modify: `README.md`
- Modify: `tests/test_repo_samples.py`
- Modify: `tests/test_smoke.py`

- [x] **Step 1: 写失败测试**

```python
readme = Path('README.md').read_text(encoding='utf-8')
assert '--ref-a-dir' in readme
assert '--ref-b-dir' in readme
assert 'insightface' in readme
assert 'onnxruntime' in readme
assert '首次运行会自动下载模型' in readme
assert '非商业研究用途' in readme
```

```python
pyproject = Path('pyproject.toml').read_text(encoding='utf-8')
assert 'insightface' in pyproject
assert 'onnxruntime' in pyproject
assert 'face_recognition' not in pyproject
```

- [x] **Step 2: 运行确认失败**

Run: `PYTHONPATH=src python3 -m pytest tests/test_repo_samples.py tests/test_smoke.py -v`
Expected: FAIL

- [x] **Step 3: 更新文档和依赖**

```toml
[project]
dependencies = [
  'insightface>=0.7.3',
  'onnxruntime>=1.18.0',
  'numpy>=1.26.0',
  'Pillow>=10.0.0',
  'pillow-heif>=0.18.0',
]
```

```bash
python3 -m pip install --upgrade pip
if ! python3 -m pip install -e '.[dev]'; then
  cat >&2 <<'TEXT'
安装失败。

请确认：
1) Python 版本满足 3.13+
2) 网络可访问模型下载地址（首次运行自动下载模型）
3) 系统可安装 onnxruntime
TEXT
  exit 1
fi
```

```md
hikbox-pictures \
  --input /path/to/photo-library \
  --ref-a-dir /path/to/person-a-dir \
  --ref-b-dir /path/to/person-b-dir \
  --output /path/to/output

首次运行会自动下载 InsightFace 模型，需要联网，首次启动可能明显慢于后续运行。
InsightFace 官方预训练模型包仅允许非商业研究用途。
```

- [x] **Step 4: 再跑测试**

Run: `PYTHONPATH=src python3 -m pytest tests/test_repo_samples.py tests/test_smoke.py -v`
Expected: PASS

- [x] **Step 5: 提交**

```bash
git add pyproject.toml scripts/install.sh README.md tests/test_repo_samples.py tests/test_smoke.py
git commit -m 'chore: migrate deps and docs to insightface'
```

---

### Task 8: 回归与全量验证

**Files:**
- Modify: `tests/test_cli.py`
- Modify: `tests/test_exporter.py`
- Modify: `tests/test_metadata.py`

- [x] **Step 1: 补齐回归断言**

```python
assert 'Scanned files:' in captured.out
assert 'only-two matches:' in captured.out
assert 'group matches:' in captured.out
assert 'Skipped decode errors:' in captured.out
assert 'Skipped no-face photos:' in captured.out
assert 'Missing Live Photo videos:' in captured.out
```

```python
assert [path.name for path in copied] == ['IMG_0001.HEIC', '.IMG_0001_123456.MOV']
assert (tmp_path / 'output' / 'only-two' / '2025-04' / 'IMG_0001.HEIC').is_file()
```

```python
assert resolve_capture_datetime(photo) == expected
```

- [x] **Step 2: 运行受影响测试集**

Run: `PYTHONPATH=src python3 -m pytest tests/test_insightface_engine.py tests/test_reference_loader.py tests/test_matcher.py tests/test_cli.py tests/test_inspect_distances.py tests/test_extract_faces.py -q`
Expected: PASS

- [x] **Step 3: 运行全量测试**

Run: `PYTHONPATH=src python3 -m pytest -q`
Expected: PASS

- [x] **Step 4: 手工冒烟 CLI 参数**

Run: `PYTHONPATH=src python3 -m hikbox_pictures.cli --input test --ref-a-dir test/piasy --ref-b-dir test/penny --output test/output`
Expected: 参考目录不存在时给出明确错误；目录存在时输出摘要

- [x] **Step 5: 提交**

```bash
git add tests/test_cli.py tests/test_exporter.py tests/test_metadata.py
git commit -m 'test: add regression coverage for insightface migration'
```

---

## 自检

### Spec coverage

- 引擎替换与统一边界: Task 1, Task 5, Task 6
- 目录参考图与单脸校验: Task 2, Task 4
- 最小距离与不同人脸约束: Task 3, Task 5
- CLI breaking change: Task 4, Task 7
- 输出结构与 Live Photo 行为不变: Task 8
- 安装与 README 迁移说明: Task 7

### Placeholder scan

- 无 TBD/TODO
- 每个任务含明确命令、预期结果、代码片段

### Type consistency

- 目录接口统一为 `load_reference_embeddings`
- 匹配逻辑统一复用 `compute_min_distances`
- CLI 参数统一为 `--ref-a-dir` 与 `--ref-b-dir`

