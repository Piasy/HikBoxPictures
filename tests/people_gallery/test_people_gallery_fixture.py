from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, UnidentifiedImageError


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "people_gallery_scan"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"
REPORT_DIR = REPO_ROOT / ".tmp" / "people-gallery-test-gallery"
SUPPORTED_SCAN_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".heif"}
TARGET_PEOPLE = {"target_alex", "target_blair", "target_casey"}
REAL_PROBE_DET_THRESH = 0.7
EXPECTED_CATEGORY_COUNTS = {
    "single_target": 30,
    "target_group": 8,
    "non_target_person": 4,
    "faceless": 4,
    "live_positive": 2,
    "live_negative": 2,
}


def test_people_gallery_fixture_contract_and_real_face_probe() -> None:
    manifest = _load_manifest()
    assets = manifest["assets"]
    checksums = manifest["checksums"]
    expected_person_groups = manifest["expected_person_groups"]
    expected_exports = manifest["expected_exports"]
    tolerances = manifest["tolerances"]

    _assert_top_level_schema(manifest)
    _assert_provenance_contract(manifest["provenance"])
    _assert_files_and_checksums(assets=assets, checksums=checksums)
    _assert_asset_contract(assets=assets, tolerances=tolerances)
    _assert_precise_counts(assets)
    _assert_expected_references(
        assets=assets,
        expected_person_groups=expected_person_groups,
        expected_exports=expected_exports,
    )
    _assert_decoding_contract(assets)
    _assert_live_photo_contract(assets=assets, checksums=checksums)
    _assert_real_insightface_probe(assets=assets)
    _assert_files_and_checksums(assets=assets, checksums=checksums)


def _load_manifest() -> dict[str, Any]:
    assert MANIFEST_PATH.exists(), f"缺少 manifest: {MANIFEST_PATH.relative_to(REPO_ROOT)}"
    with MANIFEST_PATH.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    assert isinstance(manifest, dict), "manifest 顶层必须是 JSON object"
    return manifest


def _assert_top_level_schema(manifest: dict[str, Any]) -> None:
    required = {
        "people",
        "assets",
        "expected_person_groups",
        "expected_exports",
        "tolerances",
        "checksums",
        "provenance",
    }
    missing = required - set(manifest)
    assert not missing, f"manifest 缺少顶层字段: {sorted(missing)}"
    assert isinstance(manifest["people"], list) and manifest["people"], "people 必须是非空列表"
    assert isinstance(manifest["assets"], list) and manifest["assets"], "assets 必须是非空列表"
    assert isinstance(manifest["checksums"], dict) and manifest["checksums"], "checksums 必须是非空映射"
    people_by_label = {item["label"]: item for item in manifest["people"]}
    assert TARGET_PEOPLE <= set(people_by_label), f"缺少目标人物标签: {sorted(TARGET_PEOPLE - set(people_by_label))}"
    for label in TARGET_PEOPLE:
        assert people_by_label[label]["expected_auto_person"] is True, f"{label} 应声明自动形成匿名人物"


def _assert_provenance_contract(provenance: dict[str, Any]) -> None:
    assert isinstance(provenance.get("source_images"), list), "provenance.source_images 必须是列表"
    for source_image in provenance["source_images"]:
        source_path = Path(source_image)
        assert not source_path.is_absolute(), f"provenance.source_images 不得包含本机绝对路径: {source_image}"


def _assert_files_and_checksums(*, assets: list[dict[str, Any]], checksums: dict[str, str]) -> None:
    expected_files = {asset["file"] for asset in assets}
    for asset in assets:
        live = asset.get("live_photo") or {}
        if live.get("mov_file"):
            expected_files.add(live["mov_file"])
    actual_files = {
        path.relative_to(FIXTURE_DIR).as_posix()
        for path in FIXTURE_DIR.iterdir()
        if path.is_file()
        and path.name != "manifest.json"
    }
    assert actual_files == expected_files, (
        "fixture 文件集合不精确；"
        f"缺少 {sorted(expected_files - actual_files)}，多出 {sorted(actual_files - expected_files)}"
    )

    assert set(checksums) == actual_files, (
        "checksums 文件集合不精确；"
        f"缺少 {sorted(actual_files - set(checksums))}，多出 {sorted(set(checksums) - actual_files)}"
    )
    for relative_path, expected_sha256 in sorted(checksums.items()):
        actual_sha256 = _sha256(FIXTURE_DIR / relative_path)
        assert actual_sha256 == expected_sha256, f"checksum 不匹配: {relative_path}"

    for asset in assets:
        assert asset["checksum_sha256"] == checksums[asset["file"]], f"asset checksum 未关联: {asset['id']}"


def _assert_asset_contract(*, assets: list[dict[str, Any]], tolerances: dict[str, Any]) -> None:
    seen_ids: set[str] = set()
    seen_files: set[str] = set()
    tolerance_asset_ids = set(tolerances.get("assets", {}))
    for asset in assets:
        missing = {
            "id",
            "file",
            "checksum_sha256",
            "capture_month",
            "category",
            "scan_supported",
            "decode_expected",
            "people",
            "expected_target_people",
            "live_photo",
            "is_faceless",
            "is_corrupt",
            "is_unsupported_extension",
            "tolerance",
        } - set(asset)
        assert not missing, f"asset {asset.get('id', '<unknown>')} 缺少字段: {sorted(missing)}"
        assert asset["id"] not in seen_ids, f"重复 asset id: {asset['id']}"
        assert asset["file"] not in seen_files, f"重复 asset file: {asset['file']}"
        seen_ids.add(asset["id"])
        seen_files.add(asset["file"])
        assert asset["file"] == Path(asset["file"]).name, f"asset file 不得包含目录: {asset['file']}"
        assert len(asset["capture_month"]) == 7 and asset["capture_month"][4] == "-", f"拍摄月份格式错误: {asset['id']}"
        assert isinstance(asset["people"], list), f"people 必须是列表: {asset['id']}"
        assert isinstance(asset["expected_target_people"], list), f"expected_target_people 必须是列表: {asset['id']}"
        assert set(asset["expected_target_people"]) <= TARGET_PEOPLE, f"未知目标人物: {asset['id']}"
        assert asset["tolerance"] == (asset["id"] in tolerance_asset_ids), f"tolerance 标记不一致: {asset['id']}"
        if asset["scan_supported"]:
            assert Path(asset["file"]).suffix.lower() in SUPPORTED_SCAN_SUFFIXES, f"支持扫描文件后缀错误: {asset['file']}"
        if asset["is_unsupported_extension"]:
            assert asset["scan_supported"] is False, f"非支持后缀不得计入扫描: {asset['id']}"
        if asset["is_corrupt"]:
            assert asset["decode_expected"] is False, f"损坏图片不得声明可解码: {asset['id']}"

    sorted_files = sorted(seen_files)
    assert [asset["file"] for asset in assets] == sorted_files, "manifest assets 必须按文件名稳定排序"


def _assert_precise_counts(assets: list[dict[str, Any]]) -> None:
    supported = [asset for asset in assets if asset["scan_supported"]]
    unsupported = [asset for asset in assets if asset["is_unsupported_extension"]]
    corrupt = [asset for asset in assets if asset["is_corrupt"]]
    assert len(supported) == 50, f"支持扫描照片数量应为 50，实际 {len(supported)}"
    assert len(unsupported) == 1, f"非支持后缀文件数量应为 1，实际 {len(unsupported)}"
    assert len(corrupt) == 1, f"损坏/不可解码图片数量应为 1，实际 {len(corrupt)}"

    counts = Counter(asset["category"] for asset in supported)
    assert counts == EXPECTED_CATEGORY_COUNTS, f"类别数量不符: {dict(counts)}"

    single_by_person: dict[str, int] = defaultdict(int)
    for asset in supported:
        if asset["category"] == "single_target":
            assert len(asset["expected_target_people"]) == 1, f"单目标人物照片人物数错误: {asset['id']}"
            single_by_person[asset["expected_target_people"][0]] += 1
        if asset["category"] == "target_group":
            assert len(asset["expected_target_people"]) in {2, 3}, f"合照人物数错误: {asset['id']}"
    assert single_by_person == {label: 10 for label in sorted(TARGET_PEOPLE)}, f"单人照片分布错误: {dict(single_by_person)}"

    group_sizes = Counter(len(asset["expected_target_people"]) for asset in supported if asset["category"] == "target_group")
    assert group_sizes == {2: 6, 3: 2}, f"合照人数分布错误: {dict(group_sizes)}"

    assert all(asset["expected_target_people"] == [] for asset in supported if asset["category"] == "non_target_person")
    assert all(asset["people"] and not set(asset["people"]) & TARGET_PEOPLE for asset in supported if asset["category"] == "non_target_person")
    assert all(asset["is_faceless"] for asset in supported if asset["category"] == "faceless")


def _assert_expected_references(
    *,
    assets: list[dict[str, Any]],
    expected_person_groups: dict[str, list[str]],
    expected_exports: dict[str, Any],
) -> None:
    asset_ids = {asset["id"] for asset in assets}
    supported_ids = {asset["id"] for asset in assets if asset["scan_supported"]}
    assert set(expected_person_groups) == TARGET_PEOPLE, "expected_person_groups 必须只覆盖 3 个目标人物"
    for label, group_asset_ids in expected_person_groups.items():
        expected_group_asset_ids = sorted(
            asset["id"]
            for asset in assets
            if asset["scan_supported"] and label in asset["expected_target_people"]
        )
        assert group_asset_ids == expected_group_asset_ids, (
            f"{label} 期望人物组必须精确等于所有包含该目标人物的 supported asset"
        )
        assert set(group_asset_ids) <= supported_ids, f"{label} 引用了非支持扫描 asset"
        for asset_id in group_asset_ids:
            asset = _asset_by_id(assets, asset_id)
            assert label in asset["expected_target_people"], f"{label} 人物组引用了不含该人物的 asset: {asset_id}"

    assert isinstance(expected_exports, dict) and expected_exports, "expected_exports 必须是非空映射"
    for export_name, export_spec in expected_exports.items():
        assert set(export_spec["selected_people"]) <= TARGET_PEOPLE, f"{export_name} 选择了未知人物"
        for bucket_name in ("only", "group"):
            for month, file_names in export_spec.get(bucket_name, {}).items():
                assert len(month) == 7 and month[4] == "-", f"{export_name} 月份格式错误: {month}"
                for file_name in file_names:
                    asset = next((item for item in assets if item["file"] == file_name), None)
                    assert asset is not None and asset["id"] in asset_ids, f"{export_name} 引用了未知导出文件: {file_name}"
        selected_people = set(export_spec["selected_people"])
        assert selected_people, f"{export_name} 必须选择至少一个人物"
        expected_only = _bucket_files_by_month(
            asset
            for asset in assets
            if asset["scan_supported"] and set(asset["expected_target_people"]) == selected_people
        )
        expected_group = _bucket_files_by_month(
            asset
            for asset in assets
            if asset["scan_supported"]
            and selected_people < set(asset["expected_target_people"])
        )
        assert export_spec.get("only", {}) == expected_only, f"{export_name}.only golden 语义不完整"
        assert export_spec.get("group", {}) == expected_group, f"{export_name}.group golden 语义不完整"


def _bucket_files_by_month(assets: Any) -> dict[str, list[str]]:
    buckets: dict[str, list[str]] = defaultdict(list)
    for asset in assets:
        buckets[asset["capture_month"]].append(asset["file"])
    return {
        month: sorted(file_names)
        for month, file_names in sorted(buckets.items())
    }


def _assert_decoding_contract(assets: list[dict[str, Any]]) -> None:
    heif_assets = [asset for asset in assets if Path(asset["file"]).suffix.lower() in {".heic", ".heif"}]
    assert len(heif_assets) == 2, f"HEIC/HEIF 样本数量应为 2，实际 {len(heif_assets)}"
    try:
        from pillow_heif import register_heif_opener
    except ImportError as exc:  # pragma: no cover - 依赖缺失时必须红灯
        raise AssertionError("缺少 HEIC/HEIF 解码依赖 pillow-heif") from exc
    register_heif_opener()

    for asset in assets:
        path = FIXTURE_DIR / asset["file"]
        if asset["decode_expected"]:
            with Image.open(path) as image:
                image.load()
                assert image.width >= 160 and image.height >= 160, f"图片尺寸过小: {asset['file']}"
                assert image.mode in {"RGB", "RGBA", "L"}, f"图片模式异常: {asset['file']}={image.mode}"
        else:
            with pytest.raises((UnidentifiedImageError, OSError)):
                with Image.open(path) as image:
                    image.load()


def _assert_live_photo_contract(*, assets: list[dict[str, Any]], checksums: dict[str, str]) -> None:
    positives = [asset for asset in assets if asset["live_photo"]["role"] == "positive"]
    negatives = [asset for asset in assets if asset["live_photo"]["role"] == "negative"]
    mov_assets = [asset for asset in assets if asset["live_photo"]["mov_file"] is not None]
    assert len(positives) == 2, f"Live Photo 正例数量应为 2，实际 {len(positives)}"
    assert len(negatives) == 2, f"Live Photo 反例数量应为 2，实际 {len(negatives)}"
    assert len(mov_assets) == 4, f"带 MOV 的 asset 总数应为 4，实际 {len(mov_assets)}"

    positive_suffix_sets = {Path(asset["file"]).suffix for asset in positives}
    positive_mov_suffix_sets = {Path(asset["live_photo"]["mov_file"]).suffix for asset in positives}
    assert positive_suffix_sets == {".HEIC", ".heif"}, f"Live 正例图片大小写覆盖错误: {positive_suffix_sets}"
    assert positive_mov_suffix_sets == {".MOV", ".mov"}, f"Live 正例 MOV 大小写覆盖错误: {positive_mov_suffix_sets}"
    for asset in assets:
        live = asset["live_photo"]
        if live["role"] == "none":
            assert live["mov_file"] is None, f"非 Live asset 不得声明 MOV: {asset['id']}"
    for asset in positives + negatives:
        live = asset["live_photo"]
        mov_file = live["mov_file"]
        assert mov_file in checksums, f"MOV 缺少 checksum: {mov_file}"
        assert (FIXTURE_DIR / mov_file).exists(), f"MOV 文件不存在: {mov_file}"
        assert (FIXTURE_DIR / mov_file).stat().st_size > 0, f"MOV 文件为空: {mov_file}"
        assert live["expected_pair"] is (live["role"] == "positive"), f"Live 配对标记错误: {asset['id']}"
    for asset in positives:
        image_path = Path(asset["file"])
        mov_path = Path(asset["live_photo"]["mov_file"])
        expected_mov_name = f".{image_path.stem}{mov_path.suffix}"
        assert mov_path.name == expected_mov_name, f"Live 正例 MOV 命名未与图片 stem 配对: {asset['id']}"
    for asset in negatives:
        image_path = Path(asset["file"])
        mov_path = Path(asset["live_photo"]["mov_file"])
        hidden_pair_names = {f".{image_path.stem}.MOV", f".{image_path.stem}.mov"}
        assert mov_path.name not in hidden_pair_names, f"Live 反例不得满足真实隐藏配对命名: {asset['id']}"
    assert all(Path(asset["live_photo"]["mov_file"]).name.startswith(".") for asset in positives), "正例 MOV 必须是隐藏文件"
    assert all(not Path(asset["live_photo"]["mov_file"]).name.startswith(".") for asset in negatives), "反例 MOV 不应伪装成隐藏配对"


def _assert_real_insightface_probe(assets: list[dict[str, Any]]) -> None:
    probe_assets = [
        asset
        for asset in assets
        if asset["category"] in {"single_target", "target_group"} and not asset["tolerance"]
    ]
    assert len([asset for asset in probe_assets if asset["category"] == "single_target"]) == 30
    assert len([asset for asset in probe_assets if asset["category"] == "target_group"]) == 8

    detector = _load_insightface_detector()
    report: dict[str, Any] = {"assets": {}, "summary": {}}
    single_counts: dict[str, int] = defaultdict(int)
    group_passed = 0
    for asset in probe_assets:
        face_count = _detect_face_count(detector, FIXTURE_DIR / asset["file"])
        report["assets"][asset["id"]] = {
            "file": asset["file"],
            "category": asset["category"],
            "expected_target_people": asset["expected_target_people"],
            "detected_face_count": face_count,
        }
        if asset["category"] == "single_target":
            assert face_count >= 1, f"InsightFace 未在单人核心照片中检测到人脸: {asset['file']}"
            single_counts[asset["expected_target_people"][0]] += 1
        elif asset["category"] == "target_group":
            assert face_count >= 2, f"InsightFace 未在合照中检测到至少两张人脸: {asset['file']}={face_count}"
            group_passed += 1

    assert single_counts == {label: 10 for label in sorted(TARGET_PEOPLE)}, f"InsightFace 单人通过分布错误: {dict(single_counts)}"
    assert group_passed == 8, f"InsightFace 合照通过数量错误: {group_passed}"
    report["summary"] = {
        "det_thresh": REAL_PROBE_DET_THRESH,
        "single_target_passed_by_person": dict(sorted(single_counts.items())),
        "target_group_passed": group_passed,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "insightface_probe_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


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


def _asset_by_id(assets: list[dict[str, Any]], asset_id: str) -> dict[str, Any]:
    for asset in assets:
        if asset["id"] == asset_id:
            return asset
    raise AssertionError(f"未知 asset id: {asset_id}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
