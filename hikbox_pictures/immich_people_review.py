"""基于 Immich 风格识别结果导出人物原图 review 页面。"""

from __future__ import annotations

from copy import deepcopy
import json
import os
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any
import gc
import uuid

from PIL import Image
from PIL import ImageDraw

from hikbox_pictures.immich_face_single_file import FaceDetectionBackend
from hikbox_pictures.immich_face_single_file import ImmichLikeFaceEngine
from hikbox_pictures.immich_face_single_file import load_rgb_image_with_exif
from hikbox_pictures.immich_people_sqlite import ImmichPeopleSqliteStore

SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic"}


def _register_heif_opener() -> None:
    import pillow_heif

    pillow_heif.register_heif_opener()


def _discover_images(input_root: Path) -> list[Path]:
    return sorted(
        [
            path
            for path in input_root.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS
        ],
        key=lambda path: path.name.lower(),
    )


def _chunked_paths(image_paths: list[Path], batch_size: int) -> Iterator[list[Path]]:
    safe_batch_size = max(1, int(batch_size))
    for index in range(0, len(image_paths), safe_batch_size):
        yield image_paths[index : index + safe_batch_size]


def _stable_asset_id(image_path: Path) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, str(image_path.expanduser().resolve())))


def _render_relpath(*, target: Path, output_dir: Path) -> str:
    return os.path.relpath(target.resolve(), start=output_dir.resolve()).replace("\\", "/")


def _clamp_bbox(*, x1: float, y1: float, x2: float, y2: float, width: int, height: int) -> tuple[int, int, int, int]:
    left = max(0, min(int(round(x1)), max(width - 1, 0)))
    top = max(0, min(int(round(y1)), max(height - 1, 0)))
    right = max(left + 1, min(int(round(x2)), width))
    bottom = max(top + 1, min(int(round(y2)), height))
    return left, top, right, bottom


def _resize_to_480p(image: Image.Image) -> tuple[Image.Image, float]:
    width, height = image.size
    short_edge = min(width, height)
    if short_edge <= 0:
        return image.copy(), 1.0
    scale = 480.0 / float(short_edge)
    resized = image.resize(
        (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
        Image.Resampling.LANCZOS,
    )
    return resized, scale


def _generate_face_artifacts(
    *,
    summary_json_path: Path,
    engine: ImmichLikeFaceEngine,
    included_face_ids: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    _register_heif_opener()
    artifact_root = summary_json_path.parent / "artifacts"
    crop_dir = artifact_root / "crops"
    context_dir = artifact_root / "context"
    crop_dir.mkdir(parents=True, exist_ok=True)
    context_dir.mkdir(parents=True, exist_ok=True)

    artifact_by_face_id: dict[str, dict[str, Any]] = {}
    faces_by_asset_id: dict[str, list[FaceRecord]] = defaultdict(list)
    for face in engine.faces.values():
        if included_face_ids is not None and face.id not in included_face_ids:
            continue
        faces_by_asset_id[face.asset_id].append(face)

    for asset_id, faces in faces_by_asset_id.items():
        asset = engine.assets[asset_id]
        image = load_rgb_image_with_exif(asset.image_path)
        try:
            width, height = image.size
            base_context_image, scale = _resize_to_480p(image)
            try:
                for face in faces:
                    left, top, right, bottom = _clamp_bbox(
                        x1=face.bounding_box.x1,
                        y1=face.bounding_box.y1,
                        x2=face.bounding_box.x2,
                        y2=face.bounding_box.y2,
                        width=width,
                        height=height,
                    )
                    crop_path = (crop_dir / f"{face.id}.jpg").resolve()
                    crop_image = image.crop((left, top, right, bottom))
                    try:
                        crop_image.save(crop_path, format="JPEG", quality=90)
                    finally:
                        crop_image.close()

                    context_path = (context_dir / f"{face.id}.jpg").resolve()
                    context_image = base_context_image.copy()
                    try:
                        draw = ImageDraw.Draw(context_image)
                        outline_width = max(2, min(8, int(max(context_image.size) * 0.008)))
                        draw.rectangle(
                            (
                                int(round(left * scale)),
                                int(round(top * scale)),
                                int(round(right * scale)),
                                int(round(bottom * scale)),
                            ),
                            outline=(225, 48, 48),
                            width=outline_width,
                        )
                        context_image.save(context_path, format="JPEG", quality=90)
                    finally:
                        context_image.close()
                    artifact_by_face_id[face.id] = {
                        "crop_path": str(crop_path),
                        "context_path": str(context_path),
                        "bbox": {
                            "x1": float(face.bounding_box.x1),
                            "y1": float(face.bounding_box.y1),
                            "x2": float(face.bounding_box.x2),
                            "y2": float(face.bounding_box.y2),
                        },
                        "score": float(face.score),
                    }
            finally:
                base_context_image.close()
        finally:
            image.close()
    return artifact_by_face_id


def _split_precomputed_artifacts(
    *,
    precomputed_artifact_by_face_id: dict[str, dict[str, Any]] | None,
    included_face_ids: set[str],
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    if not precomputed_artifact_by_face_id:
        return {}, set(included_face_ids)

    resolved_artifact_by_face_id: dict[str, dict[str, Any]] = {}
    missing_face_ids = set(included_face_ids)
    for face_id in included_face_ids:
        artifact = precomputed_artifact_by_face_id.get(face_id)
        if artifact is None:
            continue
        crop_path = Path(str(artifact["crop_path"]))
        context_path = Path(str(artifact["context_path"]))
        if not crop_path.exists() or not context_path.exists():
            continue
        resolved_artifact_by_face_id[face_id] = artifact
        missing_face_ids.discard(face_id)
    return resolved_artifact_by_face_id, missing_face_ids


def _recognition_status_value(result: Any) -> str:
    if isinstance(result, str):
        return result
    return str(result.status)


def _run_people_review(
    *,
    input_root: Path,
    backend: FaceDetectionBackend,
    min_score: float,
    max_distance: float,
    min_faces: int,
    db_path: Path | None = None,
) -> tuple[ImmichLikeFaceEngine, list[Path], list[str], list[dict[str, Any]], list[dict[str, Any]], list[Any], list[Any]]:
    image_paths = _discover_images(input_root)
    engine, current_asset_ids, asset_rows, errors, first_pass_results, second_pass_results, _ = _run_people_review_for_image_paths(
        input_root=input_root,
        image_paths=image_paths,
        backend=backend,
        min_score=min_score,
        max_distance=max_distance,
        min_faces=min_faces,
        db_path=db_path,
    )
    return engine, image_paths, current_asset_ids, asset_rows, errors, first_pass_results, second_pass_results


def _run_people_review_for_image_paths(
    *,
    input_root: Path,
    image_paths: list[Path],
    backend: FaceDetectionBackend,
    min_score: float,
    max_distance: float,
    min_faces: int,
    db_path: Path | None = None,
    summary_json_path: Path | None = None,
) -> tuple[
    ImmichLikeFaceEngine,
    list[str],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[Any],
    list[Any],
    dict[str, dict[str, Any]],
]:
    engine = ImmichLikeFaceEngine(
        backend=backend,
        min_score=min_score,
        max_distance=max_distance,
        min_faces=min_faces,
    )
    if db_path is not None:
        ImmichPeopleSqliteStore(db_path).load_into_engine(engine)
    current_asset_ids: list[str] = []
    asset_rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    artifact_by_face_id: dict[str, dict[str, Any]] = {}
    persisted_image_paths = {str(asset.image_path.expanduser().resolve()) for asset in engine.assets.values()}

    for index, image_path in enumerate(image_paths, start=1):
        asset_id = _stable_asset_id(image_path)
        resolved_image_path = str(image_path.expanduser().resolve())
        try:
            if resolved_image_path in persisted_image_paths or asset_id in engine.assets:
                raise ValueError(f"SQLite 增量库已包含该图片，暂不支持同路径重跑: {resolved_image_path}")
            engine.add_asset(asset_id=asset_id, image_path=image_path)
            detect_result = engine.detect_asset_faces(asset_id)
            current_asset_ids.append(asset_id)
            asset_rows.append(
                {
                    "asset_id": asset_id,
                    "file_name": image_path.name,
                    "extension": image_path.suffix.lower().lstrip("."),
                    "detect_status": "ok",
                    "face_count": len(engine.assets[asset_id].face_ids),
                    "new_face_count": len(detect_result.new_face_ids),
                    "matched_face_count": len(detect_result.matched_face_ids),
                    "removed_face_count": len(detect_result.removed_face_ids),
                }
            )
            if summary_json_path is not None and engine.assets[asset_id].face_ids:
                artifact_by_face_id.update(
                    _generate_face_artifacts(
                        summary_json_path=summary_json_path,
                        engine=engine,
                        included_face_ids=set(engine.assets[asset_id].face_ids),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            errors.append(
                {
                    "asset_id": asset_id,
                    "file_name": image_path.name,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
            asset_rows.append(
                {
                    "asset_id": asset_id,
                    "file_name": image_path.name,
                    "extension": image_path.suffix.lower().lstrip("."),
                    "detect_status": "error",
                    "face_count": 0,
                    "new_face_count": 0,
                    "matched_face_count": 0,
                    "removed_face_count": 0,
                }
            )

    first_pass_face_ids = list(dict.fromkeys(engine.pending_recognition_face_ids))
    engine.pending_recognition_face_ids = []
    first_pass_results = [engine.recognize_face(face_id, deferred=False) for face_id in first_pass_face_ids if face_id in engine.faces]

    deferred_face_ids = [
        face_id
        for face_id, result in zip(first_pass_face_ids, first_pass_results, strict=False)
        if result.status == "deferred"
    ]
    second_pass_results = [engine.recognize_face(face_id, deferred=True) for face_id in deferred_face_ids if face_id in engine.faces]
    if db_path is not None and current_asset_ids:
        ImmichPeopleSqliteStore(db_path).persist_current_assets(
            input_root=input_root,
            engine=engine,
            asset_ids=current_asset_ids,
        )
    return engine, current_asset_ids, asset_rows, errors, first_pass_results, second_pass_results, artifact_by_face_id


class _SummaryOnlyBackend:
    def detect_faces(self, image_path: Path, *, min_score: float) -> tuple[int, int, list[Any]]:
        raise RuntimeError(f"summary 重建阶段不应调用 detect_faces: {image_path}")


def run_people_summary_batch(
    *,
    input_root: Path,
    image_paths: list[Path],
    backend: FaceDetectionBackend,
    db_path: Path,
    summary_json_path: Path | None = None,
    min_score: float = 0.7,
    max_distance: float = 0.5,
    min_faces: int = 3,
) -> dict[str, Any]:
    _register_heif_opener()
    engine, current_asset_ids, asset_rows, errors, first_pass_results, second_pass_results, artifact_by_face_id = _run_people_review_for_image_paths(
        input_root=Path(input_root).expanduser().resolve(),
        image_paths=[Path(path).expanduser().resolve() for path in image_paths],
        backend=backend,
        db_path=Path(db_path).expanduser().resolve(),
        summary_json_path=Path(summary_json_path).expanduser().resolve() if summary_json_path is not None else None,
        min_score=min_score,
        max_distance=max_distance,
        min_faces=min_faces,
    )
    return {
        "current_asset_ids": list(current_asset_ids),
        "asset_rows": asset_rows,
        "errors": errors,
        "first_pass_statuses": [_recognition_status_value(result) for result in first_pass_results],
        "second_pass_statuses": [_recognition_status_value(result) for result in second_pass_results],
        "artifact_by_face_id": artifact_by_face_id,
    }


def write_people_summary_batched(
    *,
    input_root: Path,
    summary_json_path: Path,
    db_path: Path,
    batch_size: int,
    backend_factory: Callable[[], FaceDetectionBackend] | None = None,
    batch_runner: Callable[[list[Path]], dict[str, Any]] | None = None,
    min_score: float = 0.7,
    max_distance: float = 0.5,
    min_faces: int = 3,
) -> dict[str, Any]:
    input_root = Path(input_root).expanduser().resolve()
    summary_json_path = Path(summary_json_path).expanduser().resolve()
    resolved_db_path = Path(db_path).expanduser().resolve()
    _register_heif_opener()
    if not input_root.exists():
        raise ValueError(f"输入目录不存在: {input_root}")
    if not input_root.is_dir():
        raise ValueError(f"输入路径不是目录: {input_root}")
    if (backend_factory is None) == (batch_runner is None):
        raise ValueError("backend_factory 与 batch_runner 必须且只能提供一个")

    summary_json_path.parent.mkdir(parents=True, exist_ok=True)
    image_paths = _discover_images(input_root)
    current_asset_ids: list[str] = []
    asset_rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    first_pass_results: list[Any] = []
    second_pass_results: list[Any] = []
    precomputed_artifact_by_face_id: dict[str, dict[str, Any]] = {}

    for batch_image_paths in _chunked_paths(image_paths, batch_size):
        if batch_runner is not None:
            batch_payload = batch_runner(batch_image_paths)
        else:
            backend = backend_factory()
            try:
                batch_payload = run_people_summary_batch(
                    input_root=input_root,
                    image_paths=batch_image_paths,
                    backend=backend,
                    db_path=resolved_db_path,
                    summary_json_path=summary_json_path,
                    min_score=min_score,
                    max_distance=max_distance,
                    min_faces=min_faces,
                )
            finally:
                del backend
                gc.collect()
        current_asset_ids.extend([str(item) for item in batch_payload.get("current_asset_ids", [])])
        asset_rows.extend(list(batch_payload.get("asset_rows", [])))
        errors.extend(list(batch_payload.get("errors", [])))
        first_pass_results.extend(list(batch_payload.get("first_pass_statuses", [])))
        second_pass_results.extend(list(batch_payload.get("second_pass_statuses", [])))
        precomputed_artifact_by_face_id.update(
            {
                str(face_id): dict(artifact)
                for face_id, artifact in dict(batch_payload.get("artifact_by_face_id", {})).items()
            }
        )

    engine = ImmichLikeFaceEngine(
        backend=_SummaryOnlyBackend(),
        min_score=min_score,
        max_distance=max_distance,
        min_faces=min_faces,
    )
    ImmichPeopleSqliteStore(resolved_db_path).load_into_engine(engine)
    summary = _build_summary(
        input_root=input_root,
        summary_json_path=summary_json_path,
        engine=engine,
        image_paths=image_paths,
        current_asset_ids=current_asset_ids,
        asset_rows=asset_rows,
        errors=errors,
        first_pass_results=first_pass_results,
        second_pass_results=second_pass_results,
        precomputed_artifact_by_face_id=precomputed_artifact_by_face_id,
        min_score=min_score,
        max_distance=max_distance,
        min_faces=min_faces,
    )
    summary_json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "input_root": str(input_root),
        "summary_json": str(summary_json_path),
        "db_path": str(resolved_db_path),
        "image_count": int(summary["meta"]["image_count"]),
        "face_count": int(summary["meta"]["face_count"]),
        "person_count": int(summary["meta"]["person_count"]),
        "failed_image_count": int(summary["meta"]["failed_image_count"]),
    }


def _build_summary(
    *,
    input_root: Path,
    summary_json_path: Path,
    engine: ImmichLikeFaceEngine,
    image_paths: list[Path],
    current_asset_ids: list[str],
    asset_rows: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    first_pass_results: list[Any],
    second_pass_results: list[Any],
    precomputed_artifact_by_face_id: dict[str, dict[str, Any]] | None = None,
    min_score: float,
    max_distance: float,
    min_faces: int,
) -> dict[str, Any]:
    current_asset_id_set = set(current_asset_ids)
    included_face_ids = {
        face.id
        for face in engine.faces.values()
        if face.asset_id in current_asset_id_set
    }
    artifact_by_face_id, missing_face_ids = _split_precomputed_artifacts(
        precomputed_artifact_by_face_id=precomputed_artifact_by_face_id,
        included_face_ids=included_face_ids,
    )
    if missing_face_ids:
        artifact_by_face_id.update(
            _generate_face_artifacts(
                summary_json_path=summary_json_path,
                engine=engine,
                included_face_ids=missing_face_ids,
            )
        )
    person_assets: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    person_face_counts: Counter[str] = Counter()
    unassigned_assets: dict[str, dict[str, Any]] = {}

    for face in engine.faces.values():
        if face.asset_id not in current_asset_id_set:
            continue
        asset = engine.assets[face.asset_id]
        image_path = str(asset.image_path.resolve())
        face_entry = {
            "face_id": face.id,
            **artifact_by_face_id[face.id],
        }
        if face.person_id:
            person_face_counts[face.person_id] += 1
            asset_row = person_assets[face.person_id].setdefault(
                face.asset_id,
                {
                    "asset_id": face.asset_id,
                    "file_name": asset.image_path.name,
                    "image_path": image_path,
                    "face_count_in_asset": 0,
                    "extension": asset.image_path.suffix.lower().lstrip("."),
                    "faces": [],
                },
            )
            asset_row["face_count_in_asset"] += 1
            asset_row["faces"].append(face_entry)
            continue
        asset_row = unassigned_assets.setdefault(
            face.asset_id,
            {
                "asset_id": face.asset_id,
                "file_name": asset.image_path.name,
                "image_path": image_path,
                "face_count_in_asset": 0,
                "extension": asset.image_path.suffix.lower().lstrip("."),
                "faces": [],
            },
        )
        asset_row["face_count_in_asset"] += 1
        asset_row["faces"].append(face_entry)

    persons: list[dict[str, Any]] = []
    for index, (person_id, assets_by_id) in enumerate(
        sorted(
            person_assets.items(),
            key=lambda item: (-person_face_counts[item[0]], item[0]),
        ),
        start=1,
    ):
        assets = sorted(
            assets_by_id.values(),
            key=lambda item: (-int(item["face_count_in_asset"]), str(item["file_name"])),
        )
        for asset in assets:
            asset["faces"].sort(key=lambda item: str(item["face_id"]))
        persons.append(
            {
                "person_id": person_id,
                "person_label": f"人物 {index:02d}",
                "person_face_count": int(person_face_counts[person_id]),
                "asset_count": len(assets),
                "assets": assets,
            }
        )

    meta = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "input_root": str(input_root),
        "image_count": len(image_paths),
        "face_count": len(included_face_ids),
        "person_count": len(persons),
        "unassigned_face_count": sum(
            1
            for face in engine.faces.values()
            if face.asset_id in current_asset_id_set and not face.person_id
        ),
        "asset_with_faces_count": sum(1 for row in asset_rows if int(row["face_count"]) > 0),
        "failed_image_count": len(errors),
        "model": "insightface/buffalo_l",
        "max_distance": max_distance,
        "min_faces": min_faces,
        "min_score": min_score,
        "recognition_first_pass": dict(
            sorted(Counter(_recognition_status_value(result) for result in first_pass_results).items())
        ),
        "recognition_second_pass": dict(
            sorted(Counter(_recognition_status_value(result) for result in second_pass_results).items())
        ),
    }
    return {
        "meta": meta,
        "persons": persons,
        "unassigned_assets": sorted(
            unassigned_assets.values(),
            key=lambda item: (-int(item["face_count_in_asset"]), str(item["file_name"])),
        ),
        "assets": asset_rows,
        "errors": errors,
    }


def _merge_summary_asset_rows(
    *,
    assets_by_id: dict[str, dict[str, Any]],
    asset: dict[str, Any],
) -> None:
    asset_id = str(asset["asset_id"])
    asset_row = assets_by_id.get(asset_id)
    if asset_row is None:
        asset_row = {
            "asset_id": asset_id,
            "file_name": str(asset["file_name"]),
            "image_path": str(asset["image_path"]),
            "face_count_in_asset": 0,
            "extension": str(asset["extension"]),
            "faces": [],
        }
        assets_by_id[asset_id] = asset_row
    existing_faces = {str(face["face_id"]): face for face in asset_row["faces"]}
    for face in asset.get("faces", []):
        face_id = str(face["face_id"])
        if face_id in existing_faces:
            continue
        copied_face = {
            "face_id": face_id,
            "crop_path": str(face["crop_path"]),
            "context_path": str(face["context_path"]),
        }
        asset_row["faces"].append(copied_face)
        existing_faces[face_id] = copied_face
    asset_row["face_count_in_asset"] = len(asset_row["faces"])
    asset_row["faces"].sort(key=lambda item: str(item["face_id"]))


def merge_people_review_summaries(
    *,
    summary_json_paths: list[Path],
    input_root_label: str,
) -> dict[str, Any]:
    if not summary_json_paths:
        raise ValueError("至少需要一个 summary.json 才能生成汇总结果")

    summaries: list[tuple[Path, dict[str, Any]]] = []
    for summary_json_path in summary_json_paths:
        resolved_path = Path(summary_json_path).expanduser().resolve()
        if not resolved_path.exists():
            raise ValueError(f"summary.json 不存在: {resolved_path}")
        summaries.append((resolved_path, json.loads(resolved_path.read_text(encoding="utf-8"))))

    person_assets: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    person_face_ids: dict[str, set[str]] = defaultdict(set)
    unassigned_assets: dict[str, dict[str, Any]] = {}
    source_input_roots: list[str] = []
    recognition_first_pass: Counter[str] = Counter()
    recognition_second_pass: Counter[str] = Counter()
    errors: list[dict[str, Any]] = []
    failed_image_count = 0
    image_count = 0
    asset_with_faces_count = 0

    first_meta = dict(summaries[0][1].get("meta", {}))
    for summary_json_path, summary in summaries:
        meta = dict(summary.get("meta", {}))
        input_root = str(meta.get("input_root", "")).strip()
        if input_root:
            source_input_roots.append(input_root)
        recognition_first_pass.update(
            {
                str(key): int(value)
                for key, value in dict(meta.get("recognition_first_pass", {})).items()
            }
        )
        recognition_second_pass.update(
            {
                str(key): int(value)
                for key, value in dict(meta.get("recognition_second_pass", {})).items()
            }
        )
        failed_image_count += int(meta.get("failed_image_count", len(summary.get("errors", []))))
        image_count += int(meta.get("image_count", 0))
        asset_with_faces_count += int(meta.get("asset_with_faces_count", 0))
        for error in summary.get("errors", []):
            error_row = deepcopy(error)
            error_row["summary_json"] = str(summary_json_path)
            errors.append(error_row)

        for person in summary.get("persons", []):
            person_id = str(person["person_id"])
            assets_by_id = person_assets[person_id]
            for asset in person.get("assets", []):
                _merge_summary_asset_rows(assets_by_id=assets_by_id, asset=asset)
                for face in asset.get("faces", []):
                    person_face_ids[person_id].add(str(face["face_id"]))

        for asset in summary.get("unassigned_assets", []):
            _merge_summary_asset_rows(assets_by_id=unassigned_assets, asset=asset)

    persons: list[dict[str, Any]] = []
    for index, (person_id, assets_by_id) in enumerate(
        sorted(person_assets.items(), key=lambda item: (-len(person_face_ids[item[0]]), item[0])),
        start=1,
    ):
        assets = sorted(
            assets_by_id.values(),
            key=lambda item: (-int(item["face_count_in_asset"]), str(item["file_name"])),
        )
        persons.append(
            {
                "person_id": person_id,
                "person_label": f"人物 {index:02d}",
                "person_face_count": len(person_face_ids[person_id]),
                "asset_count": len(assets),
                "assets": assets,
            }
        )

    merged_unassigned_assets = sorted(
        unassigned_assets.values(),
        key=lambda item: (-int(item["face_count_in_asset"]), str(item["file_name"])),
    )
    all_assets = {
        str(asset["asset_id"]): asset
        for person in persons
        for asset in person["assets"]
    }
    for asset in merged_unassigned_assets:
        all_assets[str(asset["asset_id"])] = asset

    face_count = sum(int(person["person_face_count"]) for person in persons) + sum(
        int(asset["face_count_in_asset"]) for asset in merged_unassigned_assets
    )
    meta = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "input_root": str(input_root_label),
        "image_count": image_count,
        "face_count": face_count,
        "person_count": len(persons),
        "unassigned_face_count": sum(int(asset["face_count_in_asset"]) for asset in merged_unassigned_assets),
        "asset_with_faces_count": asset_with_faces_count or len(all_assets),
        "failed_image_count": failed_image_count,
        "model": str(first_meta.get("model", "insightface/buffalo_l")),
        "max_distance": float(first_meta.get("max_distance", 0.5)),
        "min_faces": int(first_meta.get("min_faces", 3)),
        "min_score": float(first_meta.get("min_score", 0.7)),
        "recognition_first_pass": dict(sorted(recognition_first_pass.items())),
        "recognition_second_pass": dict(sorted(recognition_second_pass.items())),
        "source_summary_count": len(summaries),
        "source_summary_jsons": [str(path) for path, _ in summaries],
        "source_input_roots": source_input_roots,
    }
    return {
        "meta": meta,
        "persons": persons,
        "unassigned_assets": merged_unassigned_assets,
        "assets": [
            {
                "asset_id": str(asset["asset_id"]),
                "image_path": str(asset["image_path"]),
                "face_count": int(asset["face_count_in_asset"]),
            }
            for asset in sorted(
                all_assets.values(),
                key=lambda item: (-int(item["face_count_in_asset"]), str(item["file_name"])),
            )
        ],
        "errors": errors,
    }


def _summary_to_render_payload(*, summary: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    payload = deepcopy(summary)
    for person in payload.get("persons", []):
        for asset in person.get("assets", []):
            asset["image_relpath"] = _render_relpath(
                target=Path(str(asset["image_path"])),
                output_dir=output_dir,
            )
            for face in asset.get("faces", []):
                face["crop_relpath"] = _render_relpath(
                    target=Path(str(face["crop_path"])),
                    output_dir=output_dir,
                )
                face["context_relpath"] = _render_relpath(
                    target=Path(str(face["context_path"])),
                    output_dir=output_dir,
                )
    for asset in payload.get("unassigned_assets", []):
        asset["image_relpath"] = _render_relpath(
            target=Path(str(asset["image_path"])),
            output_dir=output_dir,
        )
        for face in asset.get("faces", []):
            face["crop_relpath"] = _render_relpath(
                target=Path(str(face["crop_path"])),
                output_dir=output_dir,
            )
            face["context_relpath"] = _render_relpath(
                target=Path(str(face["context_path"])),
                output_dir=output_dir,
            )
    return payload


def render_people_review_html(payload: dict[str, Any]) -> str:
    meta = payload["meta"]
    person_sections: list[str] = []
    for person in payload["persons"]:
        asset_cards = ""
        for asset in person["assets"]:
            face_cards = "".join(
                f"""
                <div class="face-evidence">
                  <figure class="evidence-panel">
                    <img src="{escape(str(face['crop_relpath']))}" alt="crop {escape(str(asset['file_name']))}" loading="lazy" />
                    <figcaption>Crop</figcaption>
                  </figure>
                  <figure class="evidence-panel">
                    <img src="{escape(str(face['context_relpath']))}" alt="context {escape(str(asset['file_name']))}" loading="lazy" />
                    <figcaption>Context</figcaption>
                  </figure>
                </div>
                """
                for face in asset.get("faces", [])
            )
            asset_cards += f"""
            <article class="asset-card">
              <div class="asset-meta">
                <div class="asset-name">
                  <a href="{escape(str(asset['image_relpath']))}" target="_blank" rel="noreferrer">{escape(str(asset['file_name']))}</a>
                </div>
                <div class="asset-count">命中人脸数：{int(asset['face_count_in_asset'])}</div>
              </div>
              <div class="evidence-grid">{face_cards}</div>
            </article>
            """
        person_sections.append(
            f"""
            <section class="person-section" id="{escape(str(person['person_id']))}">
              <header class="person-header">
                <div>
                  <h2>{escape(str(person['person_label']))}</h2>
                  <div class="person-id">{escape(str(person['person_id']))}</div>
                </div>
                <div class="person-stats">
                  <span>人脸 {int(person['person_face_count'])}</span>
                  <span>照片 {int(person['asset_count'])}</span>
                </div>
              </header>
              <div class="asset-grid">{asset_cards}</div>
            </section>
            """
        )

    unassigned_section = ""
    if payload["unassigned_assets"]:
        asset_cards = ""
        for asset in payload["unassigned_assets"]:
            face_cards = "".join(
                f"""
                <div class="face-evidence">
                  <figure class="evidence-panel">
                    <img src="{escape(str(face['crop_relpath']))}" alt="crop {escape(str(asset['file_name']))}" loading="lazy" />
                    <figcaption>Crop</figcaption>
                  </figure>
                  <figure class="evidence-panel">
                    <img src="{escape(str(face['context_relpath']))}" alt="context {escape(str(asset['file_name']))}" loading="lazy" />
                    <figcaption>Context</figcaption>
                  </figure>
                </div>
                """
                for face in asset.get("faces", [])
            )
            asset_cards += f"""
            <article class="asset-card">
              <div class="asset-meta">
                <div class="asset-name">
                  <a href="{escape(str(asset['image_relpath']))}" target="_blank" rel="noreferrer">{escape(str(asset['file_name']))}</a>
                </div>
                <div class="asset-count">未归属人脸数：{int(asset['face_count_in_asset'])}</div>
              </div>
              <div class="evidence-grid">{face_cards}</div>
            </article>
            """
        unassigned_section = f"""
        <section class="person-section">
          <header class="person-header">
            <div>
              <h2>未归属</h2>
              <div class="person-id">尚未挂到任何 person 的图片</div>
            </div>
            <div class="person-stats">
              <span>照片 {len(payload['unassigned_assets'])}</span>
            </div>
          </header>
          <div class="asset-grid">{asset_cards}</div>
        </section>
        """

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>人物原图 Review</title>
  <style>
    :root {{
      --bg: #f5f1e8;
      --panel: rgba(255, 252, 247, 0.92);
      --border: rgba(79, 55, 37, 0.18);
      --text: #2d2117;
      --muted: #6f5b4a;
      --accent: #9a4f2d;
      --shadow: 0 16px 36px rgba(79, 55, 37, 0.12);
      --radius: 22px;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(217, 156, 96, 0.22), transparent 34%),
        radial-gradient(circle at right 10%, rgba(124, 151, 110, 0.22), transparent 30%),
        linear-gradient(180deg, #fbf8f2 0%, var(--bg) 100%);
      font-family: "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", sans-serif;
    }}
    .page {{
      max-width: 1680px;
      margin: 0 auto;
      padding: 36px 24px 64px;
    }}
    .hero {{
      padding: 28px 30px;
      border: 1px solid var(--border);
      border-radius: 28px;
      background: linear-gradient(135deg, rgba(255,255,255,0.92), rgba(250,243,232,0.9));
      box-shadow: var(--shadow);
    }}
    .hero h1 {{
      margin: 0 0 10px;
      font-size: 34px;
      line-height: 1.1;
    }}
    .hero p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.6;
    }}
    .meta-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 14px;
      margin-top: 24px;
    }}
    .meta-card {{
      padding: 16px 18px;
      border-radius: 18px;
      background: var(--panel);
      border: 1px solid var(--border);
    }}
    .meta-card .label {{
      font-size: 12px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    .meta-card .value {{
      margin-top: 6px;
      font-size: 24px;
      font-weight: 700;
    }}
    .person-list {{
      margin-top: 28px;
      display: grid;
      gap: 24px;
    }}
    .person-section {{
      padding: 24px;
      border-radius: var(--radius);
      border: 1px solid var(--border);
      background: var(--panel);
      box-shadow: var(--shadow);
    }}
    .person-header {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 18px;
    }}
    .person-header h2 {{
      margin: 0;
      font-size: 24px;
    }}
    .person-id {{
      margin-top: 6px;
      color: var(--muted);
      font-size: 13px;
      word-break: break-all;
    }}
    .person-stats {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      color: var(--accent);
      font-weight: 700;
    }}
    .asset-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 16px;
    }}
    .asset-card {{
      overflow: hidden;
      border-radius: 18px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.94);
    }}
    .asset-meta {{
      padding: 12px 14px 14px;
    }}
    .asset-name {{
      font-weight: 700;
      word-break: break-all;
    }}
    .asset-name a {{
      color: inherit;
      text-decoration: none;
      border-bottom: 1px solid rgba(154, 79, 45, 0.28);
    }}
    .asset-count {{
      margin-top: 6px;
      color: var(--muted);
      font-size: 13px;
    }}
    .evidence-grid {{
      display: grid;
      gap: 10px;
      padding: 0 14px 14px;
    }}
    .face-evidence {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      align-items: start;
    }}
    .evidence-panel {{
      margin: 0;
      border-radius: 14px;
      overflow: hidden;
      border: 1px solid var(--border);
      background: #ebe2d6;
    }}
    .evidence-panel img {{
      display: block;
      width: 100%;
      height: 156px;
      object-fit: cover;
    }}
    .evidence-panel figcaption {{
      padding: 8px 10px;
      font-size: 12px;
      color: var(--muted);
      background: rgba(255,255,255,0.96);
    }}
    .empty {{
      margin-top: 24px;
      padding: 24px;
      border-radius: var(--radius);
      border: 1px dashed var(--border);
      background: rgba(255,255,255,0.72);
      color: var(--muted);
      text-align: center;
    }}
    @media (max-width: 1500px) {{
      .asset-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    }}
    @media (max-width: 1120px) {{
      .asset-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (max-width: 720px) {{
      .page {{ padding: 18px 14px 36px; }}
      .hero {{ padding: 20px 18px; }}
      .hero h1 {{ font-size: 28px; }}
      .person-header {{ flex-direction: column; }}
      .asset-grid {{ grid-template-columns: 1fr; }}
      .evidence-panel img {{ height: 132px; }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <section class="hero">
      <h1>人物原图 Review</h1>
      <p>输入目录：{escape(str(meta['input_root']))}</p>
      <div class="meta-grid">
        <div class="meta-card"><div class="label">图片</div><div class="value">{int(meta['image_count'])}</div></div>
        <div class="meta-card"><div class="label">检测到的人脸</div><div class="value">{int(meta['face_count'])}</div></div>
        <div class="meta-card"><div class="label">识别出的人物</div><div class="value">{int(meta['person_count'])}</div></div>
        <div class="meta-card"><div class="label">未归属人脸</div><div class="value">{int(meta['unassigned_face_count'])}</div></div>
      </div>
    </section>
    <section class="person-list">
      {''.join(person_sections) if person_sections else '<div class="empty">没有识别出可展示的人物。</div>'}
      {unassigned_section}
    </section>
  </main>
</body>
</html>
"""


def write_people_review(
    *,
    input_root: Path,
    output_dir: Path,
    backend: FaceDetectionBackend,
    summary_json_path: Path | None = None,
    db_path: Path | None = None,
    min_score: float = 0.7,
    max_distance: float = 0.5,
    min_faces: int = 3,
) -> dict[str, Any]:
    input_root = Path(input_root).expanduser().resolve()
    output_dir = Path(output_dir).expanduser().resolve()
    if not input_root.exists():
        raise ValueError(f"输入目录不存在: {input_root}")
    if not input_root.is_dir():
        raise ValueError(f"输入路径不是目录: {input_root}")

    output_dir.mkdir(parents=True, exist_ok=True)
    summary_json_path = (
        Path(summary_json_path).expanduser().resolve()
        if summary_json_path is not None
        else (output_dir / "summary.json").resolve()
    )
    summary_result = write_people_summary(
        input_root=input_root,
        summary_json_path=summary_json_path,
        backend=backend,
        db_path=db_path,
        min_score=min_score,
        max_distance=max_distance,
        min_faces=min_faces,
    )
    html_result = write_people_review_html_from_summary(
        summary_json_path=summary_json_path,
        output_dir=output_dir,
    )
    return {**summary_result, **html_result}


def write_people_summary(
    *,
    input_root: Path,
    summary_json_path: Path,
    backend: FaceDetectionBackend,
    db_path: Path | None = None,
    min_score: float = 0.7,
    max_distance: float = 0.5,
    min_faces: int = 3,
) -> dict[str, Any]:
    input_root = Path(input_root).expanduser().resolve()
    summary_json_path = Path(summary_json_path).expanduser().resolve()
    _register_heif_opener()
    if not input_root.exists():
        raise ValueError(f"输入目录不存在: {input_root}")
    if not input_root.is_dir():
        raise ValueError(f"输入路径不是目录: {input_root}")

    summary_json_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_db_path = Path(db_path).expanduser().resolve() if db_path is not None else None
    engine, image_paths, current_asset_ids, asset_rows, errors, first_pass_results, second_pass_results = _run_people_review(
        input_root=input_root,
        backend=backend,
        db_path=resolved_db_path,
        min_score=min_score,
        max_distance=max_distance,
        min_faces=min_faces,
    )
    summary = _build_summary(
        input_root=input_root,
        summary_json_path=summary_json_path,
        engine=engine,
        image_paths=image_paths,
        current_asset_ids=current_asset_ids,
        asset_rows=asset_rows,
        errors=errors,
        first_pass_results=first_pass_results,
        second_pass_results=second_pass_results,
        min_score=min_score,
        max_distance=max_distance,
        min_faces=min_faces,
    )
    summary_json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "input_root": str(input_root),
        "summary_json": str(summary_json_path),
        "db_path": str(resolved_db_path) if resolved_db_path is not None else None,
        "image_count": int(summary["meta"]["image_count"]),
        "face_count": int(summary["meta"]["face_count"]),
        "person_count": int(summary["meta"]["person_count"]),
        "failed_image_count": int(summary["meta"]["failed_image_count"]),
    }


def write_people_review_html_from_summary(
    *,
    summary_json_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    summary_json_path = Path(summary_json_path).expanduser().resolve()
    output_dir = Path(output_dir).expanduser().resolve()
    if not summary_json_path.exists():
        raise ValueError(f"summary.json 不存在: {summary_json_path}")

    summary = json.loads(summary_json_path.read_text(encoding="utf-8"))
    payload = _summary_to_render_payload(summary=summary, output_dir=output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)
    review_html = output_dir / "review.html"
    manifest_json = output_dir / "manifest.json"
    meta_json = output_dir / "review_payload_meta.json"
    review_html.write_text(render_people_review_html(payload), encoding="utf-8")
    manifest_json.write_text(summary_json_path.read_text(encoding="utf-8"), encoding="utf-8")
    meta_json.write_text(json.dumps(summary["meta"], ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "summary_json": str(summary_json_path),
        "output_dir": str(output_dir),
        "review_html": str(review_html),
        "manifest_json": str(manifest_json),
        "review_payload_meta_json": str(meta_json),
        "image_count": int(summary["meta"]["image_count"]),
        "face_count": int(summary["meta"]["face_count"]),
        "person_count": int(summary["meta"]["person_count"]),
        "failed_image_count": int(summary["meta"]["failed_image_count"]),
    }
