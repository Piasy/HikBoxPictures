# HikBox Pictures 实现计划

> **给代理执行者的要求：** 必须使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 按任务逐步实现本计划。步骤使用复选框（`- [ ]`）语法追踪进度。

**目标：** 构建一个仅运行在 macOS 上的 Python CLI，递归扫描图片目录，找出同时包含两位参考人物的照片，并将命中结果导出到 `only-two/YYYY-MM` 和 `group/YYYY-MM`，同时复制配对的 Live Photo `MOV` 文件并保留文件时间戳。

**架构：** 在 `src/` 下实现一个小型 Python 包，明确拆分 CLI 编排、图片加载、参考图校验、递归扫描、人脸匹配、元数据解析和导出等模块。将文件系统访问和 face-recognition 调用封装在边界清晰的函数中，便于测试时 stub 掉底层依赖，在不依赖真实模型推理的情况下覆盖主要决策逻辑。

**技术栈：** Python 3.13+、`face_recognition`、`Pillow`、`pillow-heif`、`pytest`

---

## 文件结构

### 源码文件

- 新建：`pyproject.toml`
  定义包元数据、依赖、pytest 配置以及 `hikbox-pictures` 命令行入口。
- 新建：`src/hikbox_pictures/__init__.py`
  包标记文件和版本号定义。
- 新建：`src/hikbox_pictures/models.py`
  存放扫描候选、匹配结果和运行摘要统计所需的共享枚举与数据类。
- 新建：`src/hikbox_pictures/image_io.py`
  负责支持 HEIC 的 RGB 图片加载。
- 新建：`src/hikbox_pictures/reference_loader.py`
  负责参考照片校验和人脸编码提取。
- 新建：`src/hikbox_pictures/scanner.py`
  负责递归枚举候选图片并查找配对的 Live Photo `MOV` 文件。
- 新建：`src/hikbox_pictures/matcher.py`
  负责候选图片的人脸检测、双参考图匹配和结果分桶。
- 新建：`src/hikbox_pictures/metadata.py`
  负责拍摄时间解析和 `YYYY-MM` 目录格式化。
- 新建：`src/hikbox_pictures/exporter.py`
  负责确定性目标路径生成、冲突处理、保留元数据的复制以及配对 `MOV` 导出。
- 新建：`src/hikbox_pictures/cli.py`
  负责参数解析、路径校验、端到端流程编排和摘要输出。

### 测试文件

- 新建：`tests/test_smoke.py`
  覆盖初始化结构和共享模型的基本健全性检查。
- 新建：`tests/test_scanner.py`
  覆盖递归扫描和 Live Photo 配对行为。
- 新建：`tests/test_reference_loader.py`
  覆盖参考图中 `0 / 1 / 多张人脸` 的校验逻辑。
- 新建：`tests/test_matcher.py`
  覆盖命中判断、分桶逻辑、解码失败和不同人脸约束。
- 新建：`tests/test_metadata.py`
  覆盖元数据回退顺序和年月目录格式化。
- 新建：`tests/test_exporter.py`
  覆盖输出目录结构、防覆盖命名、复制后的时间戳以及配对 `MOV` 行为。
- 新建：`tests/test_cli.py`
  覆盖 CLI 编排、致命参考图错误、警告输出和摘要格式。
- 新建：`tests/test_repo_samples.py`
  利用仓库内样例资源验证 Live Photo 命名规则和期望的月份解析结果。

### 文档与仓库整理

- 修改：`.gitignore`
  忽略 `.venv/`、`.pytest_cache/` 和打包产物。
- 新建：`README.md`
  补充安装方式、依赖注意事项、CLI 用法、输出结构和限制说明。

## 实现说明

- 将 Live Photo 配对规则解释为 `.{image_path.stem}_*.MOV`。规格文档示例中 `IMG_8175.HEIC` 对应 `.IMG_8175_1771856408349261.MOV`，因此按文件 stem 匹配才符合仓库中现有样例数据。
- 要求人物 `A` 和人物 `B` 必须分别命中两张不同的检测人脸，否则同一张模糊人脸可能同时满足两个参考图。
- 单元测试中使用 mock 的 face-recognition 行为。`tests/data/` 目录中的占位样例文件只用于扫描器配对规则和仓库资源存在性的集成检查，不依赖真实人脸识别内容。

### 任务 1：初始化包结构与共享模型

**文件：**
- 新建：`pyproject.toml`
- 新建：`src/hikbox_pictures/__init__.py`
- 新建：`src/hikbox_pictures/models.py`
- 新建：`tests/test_smoke.py`
- 修改：`.gitignore`

- [x] **步骤 1：先写失败的 smoke 测试**

```python
from pathlib import Path

from hikbox_pictures.models import CandidatePhoto, MatchBucket, PhotoEvaluation, RunSummary


def test_shared_models_have_expected_defaults(tmp_path: Path) -> None:
    candidate = CandidatePhoto(path=tmp_path / "sample.jpg")
    evaluation = PhotoEvaluation(candidate=candidate, detected_face_count=2, bucket=MatchBucket.ONLY_TWO)
    summary = RunSummary()

    assert evaluation.bucket is MatchBucket.ONLY_TWO
    assert summary.scanned_files == 0
    assert summary.only_two_matches == 0
    assert summary.group_matches == 0
    assert summary.skipped_decode_errors == 0
    assert summary.warnings == []
```

- [x] **步骤 2：运行 smoke 测试，确认当前包尚不存在**

运行：`python3 -m pytest tests/test_smoke.py -v`
预期：失败，并出现 `ModuleNotFoundError: No module named 'hikbox_pictures'`

- [x] **步骤 3：创建包骨架和共享模型**

```toml
# pyproject.toml
[build-system]
requires = ["setuptools>=69", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "hikbox-pictures"
version = "0.1.0"
description = "Find photos that contain two specific people and export them by month."
readme = "README.md"
requires-python = ">=3.13"
dependencies = [
  "face_recognition>=1.3.0",
  "Pillow>=10.0.0",
  "pillow-heif>=0.18.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0.0"]

[project.scripts]
hikbox-pictures = "hikbox_pictures.cli:main"

[tool.pytest.ini_options]
testpaths = ["tests"]
```

```python
# src/hikbox_pictures/__init__.py
__all__ = ["__version__"]

__version__ = "0.1.0"
```

```python
# src/hikbox_pictures/models.py
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class MatchBucket(str, Enum):
    ONLY_TWO = "only-two"
    GROUP = "group"


@dataclass(frozen=True)
class CandidatePhoto:
    path: Path
    live_photo_video: Path | None = None


@dataclass(frozen=True)
class PhotoEvaluation:
    candidate: CandidatePhoto
    detected_face_count: int
    bucket: MatchBucket | None


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

```gitignore
# .gitignore
.DS_Store
.pytest_cache/
.venv/
build/
dist/
*.egg-info/
```

- [x] **步骤 4：再次运行 smoke 测试**

运行：`PYTHONPATH=src python3 -m pytest tests/test_smoke.py -v`
预期：通过

- [x] **步骤 5：提交初始化结果**

```bash
git add .gitignore pyproject.toml src/hikbox_pictures/__init__.py src/hikbox_pictures/models.py tests/test_smoke.py
git commit -m "chore: bootstrap hikbox pictures package"
```

### 任务 2：实现递归扫描器与 Live Photo 配对解析

**文件：**
- 新建：`src/hikbox_pictures/scanner.py`
- 新建：`tests/test_scanner.py`

- [x] **步骤 1：先写失败的扫描器测试**

```python
from hikbox_pictures.scanner import find_live_photo_video, iter_candidate_photos


def test_iter_candidate_photos_recurses_and_filters_supported_extensions(tmp_path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()

    jpg = tmp_path / "portrait.jpg"
    heic = nested / "IMG_0001.HEIC"
    ignored = tmp_path / "notes.txt"
    mov = nested / ".IMG_0001_123456.MOV"

    jpg.write_bytes(b"jpg")
    heic.write_bytes(b"heic")
    ignored.write_text("ignore me")
    mov.write_bytes(b"mov")

    candidates = list(iter_candidate_photos(tmp_path))

    assert [candidate.path.name for candidate in candidates] == ["IMG_0001.HEIC", "portrait.jpg"]
    assert candidates[0].live_photo_video == mov
    assert candidates[1].live_photo_video is None


def test_find_live_photo_video_ignores_non_matching_hidden_mov(tmp_path) -> None:
    heic = tmp_path / "IMG_0002.HEIC"
    heic.write_bytes(b"heic")
    (tmp_path / ".IMG_9999_987654.MOV").write_bytes(b"wrong")

    assert find_live_photo_video(heic) is None
```

- [x] **步骤 2：运行扫描器测试，确认模块尚不存在**

运行：`PYTHONPATH=src python3 -m pytest tests/test_scanner.py -v`
预期：失败，并出现 `ModuleNotFoundError: No module named 'hikbox_pictures.scanner'`

- [x] **步骤 3：实现扫描器模块**

```python
# src/hikbox_pictures/scanner.py
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from hikbox_pictures.models import CandidatePhoto

SUPPORTED_EXTENSIONS = {".heic", ".jpg", ".jpeg", ".png"}


def find_live_photo_video(image_path: Path) -> Path | None:
    if image_path.suffix.lower() != ".heic":
        return None

    matches = sorted(
        candidate
        for candidate in image_path.parent.glob(f".{image_path.stem}_*.MOV")
        if candidate.is_file() and candidate.suffix.lower() == ".mov"
    )
    return matches[0] if matches else None


def iter_candidate_photos(input_root: Path) -> Iterator[CandidatePhoto]:
    for path in sorted(input_root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        yield CandidatePhoto(path=path, live_photo_video=find_live_photo_video(path))
```

- [x] **步骤 4：再次运行扫描器测试**

运行：`PYTHONPATH=src python3 -m pytest tests/test_scanner.py -v`
预期：通过

- [x] **步骤 5：提交扫描器实现**

```bash
git add src/hikbox_pictures/scanner.py tests/test_scanner.py
git commit -m "feat: add recursive image scanner"
```

### 任务 3：增加支持 HEIC 的图片加载与参考图校验

**文件：**
- 新建：`src/hikbox_pictures/image_io.py`
- 新建：`src/hikbox_pictures/reference_loader.py`
- 新建：`tests/test_reference_loader.py`

- [x] **步骤 1：先写失败的参考图加载器测试**

```python
import pytest

from hikbox_pictures.reference_loader import ReferenceImageError, load_reference_encoding


def test_load_reference_encoding_rejects_zero_faces(monkeypatch, tmp_path) -> None:
    photo = tmp_path / "person-a.jpg"
    photo.write_bytes(b"image")
    monkeypatch.setattr("hikbox_pictures.reference_loader.load_rgb_image", lambda _: "image")
    monkeypatch.setattr("hikbox_pictures.reference_loader.face_recognition.face_encodings", lambda image: [])

    with pytest.raises(ReferenceImageError, match="exactly one face"):
        load_reference_encoding(photo)


def test_load_reference_encoding_returns_single_face(monkeypatch, tmp_path) -> None:
    photo = tmp_path / "person-b.jpg"
    photo.write_bytes(b"image")
    encoding = [0.1, 0.2, 0.3]
    monkeypatch.setattr("hikbox_pictures.reference_loader.load_rgb_image", lambda _: "image")
    monkeypatch.setattr(
        "hikbox_pictures.reference_loader.face_recognition.face_encodings",
        lambda image: [encoding],
    )

    assert load_reference_encoding(photo) == encoding


def test_load_reference_encoding_rejects_multiple_faces(monkeypatch, tmp_path) -> None:
    photo = tmp_path / "group.jpg"
    photo.write_bytes(b"image")
    monkeypatch.setattr("hikbox_pictures.reference_loader.load_rgb_image", lambda _: "image")
    monkeypatch.setattr(
        "hikbox_pictures.reference_loader.face_recognition.face_encodings",
        lambda image: [[0.1], [0.2]],
    )

    with pytest.raises(ReferenceImageError, match="exactly one face"):
        load_reference_encoding(photo)
```

- [x] **步骤 2：运行参考图加载器测试，确认模块尚不存在**

运行：`PYTHONPATH=src python3 -m pytest tests/test_reference_loader.py -v`
预期：失败，并出现 `ModuleNotFoundError: No module named 'hikbox_pictures.reference_loader'`

- [x] **步骤 3：实现支持 HEIC 的图片加载和参考图校验**

```python
# src/hikbox_pictures/image_io.py
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image
from pillow_heif import register_heif_opener

register_heif_opener()


def load_rgb_image(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.array(image.convert("RGB"))
```

```python
# src/hikbox_pictures/reference_loader.py
from __future__ import annotations

from pathlib import Path
from typing import Sequence

import face_recognition

from hikbox_pictures.image_io import load_rgb_image


class ReferenceImageError(ValueError):
    pass


def load_reference_encoding(image_path: Path) -> Sequence[float]:
    encodings = face_recognition.face_encodings(load_rgb_image(image_path))
    if len(encodings) != 1:
        raise ReferenceImageError(
            f"Reference image {image_path} must contain exactly one face; found {len(encodings)}."
        )
    return encodings[0]
```

- [x] **步骤 4：再次运行参考图加载器测试**

运行：`PYTHONPATH=src python3 -m pytest tests/test_reference_loader.py -v`
预期：通过

- [x] **步骤 5：提交参考图加载层**

```bash
git add src/hikbox_pictures/image_io.py src/hikbox_pictures/reference_loader.py tests/test_reference_loader.py
git commit -m "feat: validate reference images"
```

### 任务 4：实现候选图片匹配与结果分桶

**文件：**
- 新建：`src/hikbox_pictures/matcher.py`
- 新建：`tests/test_matcher.py`

- [x] **步骤 1：先写失败的匹配器测试**

```python
import pytest

from hikbox_pictures.matcher import CandidateDecodeError, evaluate_candidate_photo
from hikbox_pictures.models import CandidatePhoto, MatchBucket


def test_evaluate_candidate_photo_classifies_only_two(monkeypatch, tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "pair.jpg")
    monkeypatch.setattr("hikbox_pictures.matcher.load_rgb_image", lambda _: "image")
    monkeypatch.setattr("hikbox_pictures.matcher.face_recognition.face_locations", lambda _: [1, 2])
    monkeypatch.setattr(
        "hikbox_pictures.matcher.face_recognition.face_encodings",
        lambda image, known_face_locations=None: [["face-1"], ["face-2"]],
    )
    compare_results = iter([[True, False], [False, True]])
    monkeypatch.setattr(
        "hikbox_pictures.matcher.face_recognition.compare_faces",
        lambda encodings, target, tolerance=0.5: next(compare_results),
    )

    evaluation = evaluate_candidate_photo(photo, [0.1], [0.2])

    assert evaluation.detected_face_count == 2
    assert evaluation.bucket is MatchBucket.ONLY_TWO


def test_evaluate_candidate_photo_classifies_group(monkeypatch, tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "group.jpg")
    monkeypatch.setattr("hikbox_pictures.matcher.load_rgb_image", lambda _: "image")
    monkeypatch.setattr("hikbox_pictures.matcher.face_recognition.face_locations", lambda _: [1, 2, 3])
    monkeypatch.setattr(
        "hikbox_pictures.matcher.face_recognition.face_encodings",
        lambda image, known_face_locations=None: [["face-1"], ["face-2"], ["face-3"]],
    )
    compare_results = iter([[True, False, False], [False, True, False]])
    monkeypatch.setattr(
        "hikbox_pictures.matcher.face_recognition.compare_faces",
        lambda encodings, target, tolerance=0.5: next(compare_results),
    )

    evaluation = evaluate_candidate_photo(photo, [0.1], [0.2])

    assert evaluation.detected_face_count == 3
    assert evaluation.bucket is MatchBucket.GROUP


def test_evaluate_candidate_photo_requires_both_people(monkeypatch, tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "solo.jpg")
    monkeypatch.setattr("hikbox_pictures.matcher.load_rgb_image", lambda _: "image")
    monkeypatch.setattr("hikbox_pictures.matcher.face_recognition.face_locations", lambda _: [1, 2])
    monkeypatch.setattr(
        "hikbox_pictures.matcher.face_recognition.face_encodings",
        lambda image, known_face_locations=None: [["face-1"], ["face-2"]],
    )
    compare_results = iter([[True, False], [False, False]])
    monkeypatch.setattr(
        "hikbox_pictures.matcher.face_recognition.compare_faces",
        lambda encodings, target, tolerance=0.5: next(compare_results),
    )

    evaluation = evaluate_candidate_photo(photo, [0.1], [0.2])

    assert evaluation.bucket is None
    assert evaluation.detected_face_count == 2


def test_evaluate_candidate_photo_requires_distinct_matching_faces(monkeypatch, tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "ambiguous.jpg")
    monkeypatch.setattr("hikbox_pictures.matcher.load_rgb_image", lambda _: "image")
    monkeypatch.setattr("hikbox_pictures.matcher.face_recognition.face_locations", lambda _: [1])
    monkeypatch.setattr(
        "hikbox_pictures.matcher.face_recognition.face_encodings",
        lambda image, known_face_locations=None: [["face-1"]],
    )
    compare_results = iter([[True], [True]])
    monkeypatch.setattr(
        "hikbox_pictures.matcher.face_recognition.compare_faces",
        lambda encodings, target, tolerance=0.5: next(compare_results),
    )

    evaluation = evaluate_candidate_photo(photo, [0.1], [0.2])

    assert evaluation.bucket is None
    assert evaluation.detected_face_count == 1


def test_evaluate_candidate_photo_wraps_decode_errors(monkeypatch, tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "broken.jpg")

    def raise_decode_error(_path):
        raise OSError("cannot decode")

    monkeypatch.setattr("hikbox_pictures.matcher.load_rgb_image", raise_decode_error)

    with pytest.raises(CandidateDecodeError, match="cannot decode"):
        evaluate_candidate_photo(photo, [0.1], [0.2])
```

- [x] **步骤 2：运行匹配器测试，确认模块尚不存在**

运行：`PYTHONPATH=src python3 -m pytest tests/test_matcher.py -v`
预期：失败，并出现 `ModuleNotFoundError: No module named 'hikbox_pictures.matcher'`

- [x] **步骤 3：实现候选图片评估逻辑**

```python
# src/hikbox_pictures/matcher.py
from __future__ import annotations

from typing import Sequence

import face_recognition

from hikbox_pictures.image_io import load_rgb_image
from hikbox_pictures.models import CandidatePhoto, MatchBucket, PhotoEvaluation


class CandidateDecodeError(RuntimeError):
    pass


def _matching_face_indexes(
    encodings: list[Sequence[float]],
    target_encoding: Sequence[float],
    tolerance: float,
) -> set[int]:
    return {
        index
        for index, is_match in enumerate(
            face_recognition.compare_faces(encodings, target_encoding, tolerance=tolerance)
        )
        if is_match
    }


def _has_distinct_matches(matches_a: set[int], matches_b: set[int]) -> bool:
    return any(index_a != index_b for index_a in matches_a for index_b in matches_b)


def evaluate_candidate_photo(
    photo: CandidatePhoto,
    person_a_encoding: Sequence[float],
    person_b_encoding: Sequence[float],
    *,
    tolerance: float = 0.5,
) -> PhotoEvaluation:
    try:
        image = load_rgb_image(photo.path)
    except Exception as exc:  # pragma: no cover
        raise CandidateDecodeError(f"Failed to decode {photo.path}: {exc}") from exc

    locations = face_recognition.face_locations(image)
    if not locations:
        return PhotoEvaluation(candidate=photo, detected_face_count=0, bucket=None)

    encodings = face_recognition.face_encodings(image, known_face_locations=locations)
    matches_a = _matching_face_indexes(encodings, person_a_encoding, tolerance)
    matches_b = _matching_face_indexes(encodings, person_b_encoding, tolerance)

    if not matches_a or not matches_b or not _has_distinct_matches(matches_a, matches_b):
        return PhotoEvaluation(candidate=photo, detected_face_count=len(encodings), bucket=None)

    bucket = MatchBucket.ONLY_TWO if len(encodings) == 2 else MatchBucket.GROUP
    return PhotoEvaluation(candidate=photo, detected_face_count=len(encodings), bucket=bucket)
```

- [x] **步骤 4：再次运行匹配器测试**

运行：`PYTHONPATH=src python3 -m pytest tests/test_matcher.py -v`
预期：通过

- [x] **步骤 5：提交匹配器实现**

```bash
git add src/hikbox_pictures/matcher.py tests/test_matcher.py
git commit -m "feat: classify matching candidate photos"
```

### 任务 5：解析拍摄时间并格式化年月目录

**文件：**
- 新建：`src/hikbox_pictures/metadata.py`
- 新建：`tests/test_metadata.py`

- [x] **步骤 1：先写失败的元数据测试**

```python
from datetime import datetime, timezone

from hikbox_pictures.metadata import format_year_month, resolve_capture_datetime


def test_resolve_capture_datetime_prefers_content_creation_date(monkeypatch, tmp_path) -> None:
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"image")
    expected = datetime(2025, 4, 3, 10, 30, tzinfo=timezone.utc)

    monkeypatch.setattr("hikbox_pictures.metadata.read_content_creation_datetime", lambda _: expected)
    monkeypatch.setattr("hikbox_pictures.metadata.read_birthtime_datetime", lambda _: None)
    monkeypatch.setattr(
        "hikbox_pictures.metadata.read_modification_datetime",
        lambda _: datetime(2025, 4, 4, 10, 30, tzinfo=timezone.utc),
    )

    assert resolve_capture_datetime(photo) == expected


def test_resolve_capture_datetime_falls_back_to_birthtime_then_mtime(monkeypatch, tmp_path) -> None:
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"image")
    birthtime = datetime(2025, 2, 1, 8, 0, tzinfo=timezone.utc)
    mtime = datetime(2025, 2, 2, 8, 0, tzinfo=timezone.utc)

    monkeypatch.setattr("hikbox_pictures.metadata.read_content_creation_datetime", lambda _: None)
    monkeypatch.setattr("hikbox_pictures.metadata.read_birthtime_datetime", lambda _: birthtime)
    monkeypatch.setattr("hikbox_pictures.metadata.read_modification_datetime", lambda _: mtime)
    assert resolve_capture_datetime(photo) == birthtime

    monkeypatch.setattr("hikbox_pictures.metadata.read_birthtime_datetime", lambda _: None)
    assert resolve_capture_datetime(photo) == mtime


def test_format_year_month_returns_directory_name() -> None:
    moment = datetime(2025, 12, 31, 23, 59, tzinfo=timezone.utc)
    assert format_year_month(moment) == moment.astimezone().strftime("%Y-%m")


def test_resolve_capture_datetime_falls_back_when_content_creation_date_is_unparsable(
    monkeypatch, tmp_path
) -> None:
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"image")
    birthtime = datetime(2025, 3, 1, 8, 0).astimezone()

    def raise_value_error(_path):
        raise ValueError("bad mdls value")

    monkeypatch.setattr(
        "hikbox_pictures.metadata.read_content_creation_datetime",
        raise_value_error,
    )
    monkeypatch.setattr("hikbox_pictures.metadata.read_birthtime_datetime", lambda _: birthtime)
    monkeypatch.setattr("hikbox_pictures.metadata.read_modification_datetime", lambda _: None)

    assert resolve_capture_datetime(photo) == birthtime
```

- [x] **步骤 2：运行元数据测试，确认模块尚不存在**

运行：`PYTHONPATH=src python3 -m pytest tests/test_metadata.py -v`
预期：失败，并出现 `ModuleNotFoundError: No module named 'hikbox_pictures.metadata'`

- [x] **步骤 3：实现元数据解析**

```python
# src/hikbox_pictures/metadata.py
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import subprocess

MDLS_DATE_FORMAT = "%Y-%m-%d %H:%M:%S %z"


def _read_mdls_value(path: Path, attribute: str) -> str | None:
    result = subprocess.run(
        ["mdls", "-raw", "-name", attribute, str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None

    value = result.stdout.strip()
    if value in {"", "(null)", "<nil>"}:
        return None
    return value


def read_content_creation_datetime(path: Path) -> datetime | None:
    value = _read_mdls_value(path, "kMDItemContentCreationDate")
    return datetime.strptime(value, MDLS_DATE_FORMAT) if value else None


def read_birthtime_datetime(path: Path) -> datetime | None:
    birthtime = getattr(path.stat(), "st_birthtime", None)
    if birthtime is None:
        return None
    return datetime.fromtimestamp(birthtime, tz=timezone.utc)


def read_modification_datetime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def resolve_capture_datetime(path: Path) -> datetime:
    for reader in (
        read_content_creation_datetime,
        read_birthtime_datetime,
        read_modification_datetime,
    ):
        try:
            value = reader(path)
        except ValueError:
            value = None
        if value is not None:
            return value
    raise RuntimeError(f"Unable to resolve capture time for {path}")


def format_year_month(moment: datetime) -> str:
    return moment.astimezone().strftime("%Y-%m")
```

- [x] **步骤 4：再次运行元数据测试**

运行：`PYTHONPATH=src python3 -m pytest tests/test_metadata.py -v`
预期：通过

- [x] **步骤 5：提交元数据解析实现**

```bash
git add src/hikbox_pictures/metadata.py tests/test_metadata.py
git commit -m "feat: resolve capture month metadata"
```

### 任务 6：导出命中文件并避免覆盖已有结果

**文件：**
- 新建：`src/hikbox_pictures/exporter.py`
- 新建：`tests/test_exporter.py`

- [x] **步骤 1：先写失败的导出器测试**

```python
from datetime import datetime, timezone

from hikbox_pictures.exporter import build_destination_path, copy_with_metadata, export_match
from hikbox_pictures.models import CandidatePhoto, MatchBucket, PhotoEvaluation


def test_build_destination_path_uses_bucket_and_year_month(tmp_path) -> None:
    source = tmp_path / "source.jpg"
    source.write_bytes(b"image")

    destination = build_destination_path(
        source,
        output_root=tmp_path / "output",
        bucket=MatchBucket.ONLY_TWO,
        year_month="2025-04",
    )

    assert destination == tmp_path / "output" / "only-two" / "2025-04" / "source.jpg"


def test_build_destination_path_avoids_overwriting_existing_files(tmp_path) -> None:
    source = tmp_path / "duplicate.jpg"
    source.write_bytes(b"image")
    occupied = tmp_path / "output" / "group" / "2025-04" / "duplicate.jpg"
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b"existing")

    destination = build_destination_path(
        source,
        output_root=tmp_path / "output",
        bucket=MatchBucket.GROUP,
        year_month="2025-04",
    )

    assert destination.name.startswith("duplicate__")
    assert destination.suffix == ".jpg"


def test_copy_with_metadata_preserves_mtime(monkeypatch, tmp_path) -> None:
    source = tmp_path / "source.jpg"
    destination = tmp_path / "copy.jpg"
    source.write_bytes(b"image")

    monkeypatch.setattr("hikbox_pictures.exporter.set_creation_time", lambda source_path, dest_path: None)

    copy_with_metadata(source, destination)

    assert destination.read_bytes() == b"image"
    assert int(destination.stat().st_mtime) == int(source.stat().st_mtime)


def test_export_match_copies_photo_and_paired_live_photo(monkeypatch, tmp_path) -> None:
    photo_path = tmp_path / "IMG_0001.HEIC"
    mov_path = tmp_path / ".IMG_0001_123456.MOV"
    photo_path.write_bytes(b"photo")
    mov_path.write_bytes(b"movie")

    evaluation = PhotoEvaluation(
        candidate=CandidatePhoto(path=photo_path, live_photo_video=mov_path),
        detected_face_count=2,
        bucket=MatchBucket.ONLY_TWO,
    )
    monkeypatch.setattr("hikbox_pictures.exporter.set_creation_time", lambda source_path, dest_path: None)

    copied = export_match(
        evaluation,
        output_root=tmp_path / "output",
        capture_datetime=datetime(2025, 4, 3, 10, 30, tzinfo=timezone.utc),
    )

    assert [path.name for path in copied] == ["IMG_0001.HEIC", ".IMG_0001_123456.MOV"]
    assert (tmp_path / "output" / "only-two" / "2025-04" / "IMG_0001.HEIC").read_bytes() == b"photo"
    assert (tmp_path / "output" / "only-two" / "2025-04" / ".IMG_0001_123456.MOV").read_bytes() == b"movie"
```

- [x] **步骤 2：运行导出器测试，确认模块尚不存在**

运行：`PYTHONPATH=src python3 -m pytest tests/test_exporter.py -v`
预期：失败，并出现 `ModuleNotFoundError: No module named 'hikbox_pictures.exporter'`

- [x] **步骤 3：实现导出器**

```python
# src/hikbox_pictures/exporter.py
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

    return copied_paths
```

- [x] **步骤 4：再次运行导出器测试**

运行：`PYTHONPATH=src python3 -m pytest tests/test_exporter.py -v`
预期：通过

- [x] **步骤 5：提交导出器实现**

```bash
git add src/hikbox_pictures/exporter.py tests/test_exporter.py
git commit -m "feat: export matched files by month"
```

### 任务 7：串联 CLI 与运行摘要输出

**文件：**
- 新建：`src/hikbox_pictures/cli.py`
- 新建：`tests/test_cli.py`

- [x] **步骤 1：先写失败的 CLI 测试**

```python
from datetime import datetime

import pytest

from hikbox_pictures.cli import main
from hikbox_pictures.models import CandidatePhoto, MatchBucket, PhotoEvaluation


def test_main_exports_only_hits_and_prints_summary(monkeypatch, tmp_path, capsys) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    ref_a = tmp_path / "ref-a.jpg"
    ref_b = tmp_path / "ref-b.jpg"
    input_dir.mkdir()
    output_dir.mkdir()
    ref_a.write_bytes(b"a")
    ref_b.write_bytes(b"b")

    candidate_hit = CandidatePhoto(path=input_dir / "pair.jpg")
    candidate_miss = CandidatePhoto(path=input_dir / "miss.jpg")
    candidate_hit.path.write_bytes(b"pair")
    candidate_miss.path.write_bytes(b"miss")

    monkeypatch.setattr(
        "sys.argv",
        [
            "hikbox-pictures",
            "--input",
            str(input_dir),
            "--ref-a",
            str(ref_a),
            "--ref-b",
            str(ref_b),
            "--output",
            str(output_dir),
        ],
    )
    monkeypatch.setattr(
        "hikbox_pictures.cli.load_reference_encoding",
        lambda path: [0.1] if path == ref_a else [0.2],
    )
    monkeypatch.setattr(
        "hikbox_pictures.cli.iter_candidate_photos",
        lambda path: iter([candidate_hit, candidate_miss]),
    )
    evaluations = iter(
        [
            PhotoEvaluation(candidate=candidate_hit, detected_face_count=2, bucket=MatchBucket.ONLY_TWO),
            PhotoEvaluation(candidate=candidate_miss, detected_face_count=1, bucket=None),
        ]
    )
    monkeypatch.setattr(
        "hikbox_pictures.cli.evaluate_candidate_photo",
        lambda candidate, enc_a, enc_b: next(evaluations),
    )
    monkeypatch.setattr(
        "hikbox_pictures.cli.resolve_capture_datetime",
        lambda path: datetime(2025, 4, 3, 10, 30),
    )
    exported = []
    monkeypatch.setattr(
        "hikbox_pictures.cli.export_match",
        lambda evaluation, output_root, capture_datetime: exported.append(evaluation.candidate.path),
    )

    exit_code = main()

    stdout = capsys.readouterr().out
    assert exit_code == 0
    assert exported == [candidate_hit.path]
    assert "Scanned files: 2" in stdout
    assert "only-two matches: 1" in stdout
    assert "group matches: 0" in stdout


def test_main_exits_nonzero_for_invalid_reference_photo(monkeypatch, tmp_path, capsys) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    ref_a = tmp_path / "ref-a.jpg"
    ref_b = tmp_path / "ref-b.jpg"
    input_dir.mkdir()
    output_dir.mkdir()
    ref_a.write_bytes(b"a")
    ref_b.write_bytes(b"b")

    monkeypatch.setattr(
        "sys.argv",
        [
            "hikbox-pictures",
            "--input",
            str(input_dir),
            "--ref-a",
            str(ref_a),
            "--ref-b",
            str(ref_b),
            "--output",
            str(output_dir),
        ],
    )

    from hikbox_pictures.reference_loader import ReferenceImageError

    def raise_reference_error(path):
        raise ReferenceImageError(f"bad ref: {path.name}")

    monkeypatch.setattr("hikbox_pictures.cli.load_reference_encoding", raise_reference_error)

    exit_code = main()

    stderr = capsys.readouterr().err
    assert exit_code == 2
    assert "bad ref: ref-a.jpg" in stderr
```

- [x] **步骤 2：运行 CLI 测试，确认模块尚不存在**

运行：`PYTHONPATH=src python3 -m pytest tests/test_cli.py -v`
预期：失败，并出现 `ModuleNotFoundError: No module named 'hikbox_pictures.cli'`

- [x] **步骤 3：实现 CLI 编排逻辑**

```python
# src/hikbox_pictures/cli.py
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from hikbox_pictures.exporter import export_match
from hikbox_pictures.matcher import CandidateDecodeError, evaluate_candidate_photo
from hikbox_pictures.metadata import resolve_capture_datetime
from hikbox_pictures.models import MatchBucket, RunSummary
from hikbox_pictures.reference_loader import ReferenceImageError, load_reference_encoding
from hikbox_pictures.scanner import iter_candidate_photos


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hikbox-pictures")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--ref-a", required=True, type=Path)
    parser.add_argument("--ref-b", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def _print_summary(summary: RunSummary) -> None:
    print(f"Scanned files: {summary.scanned_files}")
    print(f"only-two matches: {summary.only_two_matches}")
    print(f"group matches: {summary.group_matches}")
    print(f"Skipped decode errors: {summary.skipped_decode_errors}")
    print(f"Skipped no-face photos: {summary.skipped_no_faces}")
    print(f"Missing Live Photo videos: {summary.missing_live_photo_videos}")
    for warning in summary.warnings:
        print(f"WARNING: {warning}", file=sys.stderr)


def main() -> int:
    args = build_parser().parse_args()
    for path in (args.input, args.ref_a, args.ref_b):
        if not path.exists():
            print(f"Path does not exist: {path}", file=sys.stderr)
            return 2
    args.output.mkdir(parents=True, exist_ok=True)

    try:
        person_a_encoding = load_reference_encoding(args.ref_a)
        person_b_encoding = load_reference_encoding(args.ref_b)
    except ReferenceImageError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    summary = RunSummary()
    for candidate in iter_candidate_photos(args.input):
        summary.scanned_files += 1
        try:
            evaluation = evaluate_candidate_photo(candidate, person_a_encoding, person_b_encoding)
        except CandidateDecodeError as exc:
            summary.skipped_decode_errors += 1
            summary.warnings.append(str(exc))
            continue

        if evaluation.detected_face_count == 0:
            summary.skipped_no_faces += 1
            continue
        if evaluation.bucket is None:
            continue

        capture_datetime = resolve_capture_datetime(candidate.path)
        export_match(evaluation, output_root=args.output, capture_datetime=capture_datetime)
        if evaluation.bucket is MatchBucket.ONLY_TWO:
            summary.only_two_matches += 1
        else:
            summary.group_matches += 1
        if candidate.path.suffix.lower() == ".heic" and candidate.live_photo_video is None:
            summary.missing_live_photo_videos += 1
            summary.warnings.append(f"Missing Live Photo MOV for {candidate.path}")

    _print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [x] **步骤 4：再次运行 CLI 测试**

运行：`PYTHONPATH=src python3 -m pytest tests/test_cli.py -v`
预期：通过

- [x] **步骤 5：提交 CLI 实现**

```bash
git add src/hikbox_pictures/cli.py tests/test_cli.py
git commit -m "feat: add hikbox pictures cli"
```

### 任务 8：校验仓库内置样例资源并补充说明文档

**文件：**
- 新建：`tests/test_repo_samples.py`
- 新建：`README.md`

- [x] **步骤 1：先写失败的仓库样例与 README 检查**

```python
from pathlib import Path

from hikbox_pictures.scanner import find_live_photo_video


def test_sample_heic_finds_bundled_live_photo_video() -> None:
    sample = Path("tests/data/IMG_8175.HEIC")
    assert find_live_photo_video(sample) == Path("tests/data/.IMG_8175_1771856408349261.MOV")


def test_sample_files_exist_as_placeholder_assets() -> None:
    sample = Path("tests/data/IMG_8175.HEIC")
    mov = Path("tests/data/.IMG_8175_1771856408349261.MOV")
    assert sample.is_file()
    assert mov.is_file()
    assert sample.stat().st_size == 0
    assert mov.stat().st_size == 0


def test_readme_mentions_macos_dependencies() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "macOS" in readme
    assert "face_recognition" in readme
    assert "hikbox-pictures --input" in readme
```

- [x] **步骤 2：运行样例与 README 测试，确认当前会失败**

运行：`PYTHONPATH=src python3 -m pytest tests/test_repo_samples.py -v`
预期：失败，因为此时 `README.md` 还不存在

- [x] **步骤 3：补齐仓库样例测试和 README**

```python
# tests/test_repo_samples.py
from pathlib import Path

from hikbox_pictures.scanner import find_live_photo_video


def test_sample_heic_finds_bundled_live_photo_video() -> None:
    sample = Path("tests/data/IMG_8175.HEIC")
    assert find_live_photo_video(sample) == Path("tests/data/.IMG_8175_1771856408349261.MOV")


def test_sample_files_exist_as_placeholder_assets() -> None:
    sample = Path("tests/data/IMG_8175.HEIC")
    mov = Path("tests/data/.IMG_8175_1771856408349261.MOV")
    assert sample.is_file()
    assert mov.is_file()
    assert sample.stat().st_size == 0
    assert mov.stat().st_size == 0


def test_readme_mentions_macos_dependencies() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "macOS" in readme
    assert "face_recognition" in readme
    assert "hikbox-pictures --input" in readme
```

```markdown
# HikBox Pictures

HikBox Pictures is a local macOS CLI that recursively scans a photo directory, finds images containing both reference people, and copies matching photos into `only-two/YYYY-MM` and `group/YYYY-MM` output buckets.

## Requirements

- macOS
- Python 3.13+
- Xcode Command Line Tools
- `face_recognition` runtime dependencies, including `dlib`

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e '.[dev]'
```

If `dlib` fails to build, install the Xcode Command Line Tools and retry inside the virtual environment.

## Usage

```bash
hikbox-pictures --input /path/to/photo-library --ref-a /path/to/person-a.jpg --ref-b /path/to/person-b.jpg --output /path/to/output
```

## Output

- `only-two/YYYY-MM/` contains hits with exactly two detected faces.
- `group/YYYY-MM/` contains hits with more than two detected faces.
- Matching `HEIC` files copy their paired hidden Live Photo `MOV` when present.

## Limitations

- Matching quality depends on the bundled `face_recognition` model and image quality.
- The tool scans image files only; videos are not analyzed.
- Creation-time preservation relies on macOS `SetFile` and is best-effort.
```

- [x] **步骤 4：再次运行样例与 README 测试**

运行：`PYTHONPATH=src python3 -m pytest tests/test_repo_samples.py -v`
预期：通过

- [x] **步骤 5：提交文档和样例检查**

```bash
git add README.md tests/test_repo_samples.py
git commit -m "docs: add setup guide and sample checks"
```

### 任务 9：运行完整测试并验证 CLI 入口

**文件：**
- 测试：`tests/test_smoke.py`
- 测试：`tests/test_scanner.py`
- 测试：`tests/test_reference_loader.py`
- 测试：`tests/test_matcher.py`
- 测试：`tests/test_metadata.py`
- 测试：`tests/test_exporter.py`
- 测试：`tests/test_cli.py`
- 测试：`tests/test_repo_samples.py`

- [x] **步骤 1：运行完整 pytest 测试套件**

运行：`PYTHONPATH=src python3 -m pytest -v`
预期：所有测试模块都通过

- [x] **步骤 2：做一次已安装 CLI 帮助输出的冒烟验证**

运行：`python3 -m pip install -e '.[dev]' && hikbox-pictures --help`
预期：帮助输出中包含 `--input`、`--ref-a`、`--ref-b` 和 `--output`

- [x] **步骤 3：提交最终验证通过的状态**

```bash
git add pyproject.toml README.md src/hikbox_pictures tests
git commit -m "feat: ship hikbox pictures v1 cli"
```

## 自检

### 规格覆盖情况

- `HEIC`、`JPG`、`JPEG`、`PNG` 的递归扫描：任务 2。
- 两张参考照片都必须且只能检测到一张脸：任务 3。
- 本地人脸匹配、两人同时命中的判断规则，以及 `only-two` / `group` 分类：任务 4。
- 拍摄时间回退顺序和 `YYYY-MM` 目录布局：任务 5。
- 防覆盖复制、元数据保留和配对 Live Photo `MOV` 复制：任务 6。
- CLI 参数、摘要输出、致命/非致命错误处理和警告：任务 7。
- 安装说明文档和样例资源检查：任务 8。
- 完成前的完整验证：任务 9。

### 占位符检查

- 不再保留 `TBD`、`TODO` 或“后续再实现”之类的延后项。
- 每个任务都包含明确文件路径、具体代码、精确命令和预期结果。

### 类型一致性

- `PhotoEvaluation` 在测试和实现片段中始终使用 `candidate` 字段。
- `MatchBucket` 的取值始终与输出目录名 `only-two` 和 `group` 保持一致。
- CLI 摘要计数字段与 `RunSummary` 中的定义保持一致。

计划已完成并保存到 `docs/superpowers/plans/2026-04-09-hikbox-pictures.md`。后续有两种执行方式：

1. 子代理驱动（推荐）- 我会为每个任务分配一个新的子代理，在任务之间做评审，迭代更快

2. 当前会话内直接执行 - 在这个会话里使用 `executing-plans` 按检查点分批执行任务

你想采用哪种方式？
