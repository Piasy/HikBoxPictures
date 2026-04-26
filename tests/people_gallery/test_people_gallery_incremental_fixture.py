from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from PIL import Image


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "people_gallery_scan_2"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"
REPORT_DIR = REPO_ROOT / ".tmp" / "people-gallery-scan-2"
REAL_PROBE_DET_THRESH = 0.7
EXPECTED_SINGLE_TARGET_COUNTS = {
    "target_alex": 5,
    "target_blair": 5,
    "target_casey": 5,
}


def test_people_gallery_scan_2_single_target_assets_have_detectable_face() -> None:
    manifest = _load_manifest()
    assets = manifest["assets"]
    assert len(assets) == 15, f"增量 fixture 资产数量应为 15，实际 {len(assets)}"

    detector = _load_insightface_detector()
    report: dict[str, Any] = {"assets": {}, "summary": {}}
    single_counts: dict[str, int] = defaultdict(int)

    for asset in assets:
        assert asset["scan_supported"] is True, f"增量 fixture 不应包含非扫描样本: {asset['file']}"
        assert asset["category"] == "single_target", f"增量 fixture 只应包含单人照: {asset['file']}"
        assert asset["tolerance"] is False, f"增量 fixture 核心样本不得依赖 tolerance: {asset['file']}"
        assert len(asset["expected_target_people"]) == 1, f"增量 fixture 单人照目标人物数错误: {asset['file']}"

        face_count = _detect_face_count(detector, FIXTURE_DIR / asset["file"])
        person_label = asset["expected_target_people"][0]
        report["assets"][asset["id"]] = {
            "file": asset["file"],
            "expected_target_people": asset["expected_target_people"],
            "detected_face_count": face_count,
        }
        assert face_count >= 1, f"InsightFace 未在增量单人照中检测到人脸: {asset['file']}"
        single_counts[person_label] += 1

    assert dict(sorted(single_counts.items())) == EXPECTED_SINGLE_TARGET_COUNTS, (
        f"增量 fixture 单人照分布错误: {dict(single_counts)}"
    )
    report["summary"] = {
        "det_thresh": REAL_PROBE_DET_THRESH,
        "single_target_passed_by_person": dict(sorted(single_counts.items())),
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "pytest_insightface_probe_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _load_manifest() -> dict[str, Any]:
    assert MANIFEST_PATH.exists(), f"缺少 manifest: {MANIFEST_PATH.relative_to(REPO_ROOT)}"
    with MANIFEST_PATH.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    assert isinstance(manifest, dict), "manifest 顶层必须是 JSON object"
    return manifest


def _load_insightface_detector() -> Any:
    from insightface.model_zoo import model_zoo

    model_root = _find_model_root()
    detector_path = model_root / "models" / "buffalo_l" / "det_10g.onnx"
    assert detector_path.exists(), f"缺少 InsightFace detector 模型: {detector_path}"
    detector = model_zoo.get_model(detector_path.as_posix(), providers=["CPUExecutionProvider"])
    detector.prepare(ctx_id=0, det_thresh=REAL_PROBE_DET_THRESH, input_size=(640, 640))
    return detector


def _find_model_root() -> Path:
    candidates = [REPO_ROOT / ".insightface", Path.home() / ".insightface"]
    candidates.extend(parent / ".insightface" for parent in REPO_ROOT.parents)
    for candidate in candidates:
        if (candidate / "models" / "buffalo_l" / "det_10g.onnx").exists():
            return candidate
    raise AssertionError("缺少 InsightFace buffalo_l 模型目录，无法执行真实探针")


def _detect_face_count(detector: Any, image_path: Path) -> int:
    import numpy as np

    with Image.open(image_path) as image:
        rgb = image.convert("RGB")
        bgr = np.asarray(rgb, dtype=np.uint8)[:, :, ::-1]
    bboxes, _landmarks = detector.detect(bgr)
    return int(bboxes.shape[0])
