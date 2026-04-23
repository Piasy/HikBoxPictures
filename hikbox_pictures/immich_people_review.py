"""基于 Immich 风格识别结果导出人物原图 review 页面。"""

from __future__ import annotations

from copy import deepcopy
import json
import os
from collections import Counter, defaultdict
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

from PIL import Image
from PIL import ImageDraw

from hikbox_pictures.immich_face_single_file import FaceDetectionBackend
from hikbox_pictures.immich_face_single_file import ImmichLikeFaceEngine

SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic"}


def _discover_images(input_root: Path) -> list[Path]:
    return sorted(
        [
            path
            for path in input_root.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS
        ],
        key=lambda path: path.name.lower(),
    )


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
) -> dict[str, dict[str, Any]]:
    artifact_root = summary_json_path.parent / "artifacts"
    crop_dir = artifact_root / "crops"
    context_dir = artifact_root / "context"
    crop_dir.mkdir(parents=True, exist_ok=True)
    context_dir.mkdir(parents=True, exist_ok=True)

    image_cache: dict[str, Image.Image] = {}
    artifact_by_face_id: dict[str, dict[str, Any]] = {}
    try:
        for face in engine.faces.values():
            asset = engine.assets[face.asset_id]
            image_key = str(asset.image_path.resolve())
            image = image_cache.get(image_key)
            if image is None:
                image = Image.open(asset.image_path).convert("RGB")
                image_cache[image_key] = image
            width, height = image.size
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
            crop_image.save(crop_path, format="JPEG", quality=90)

            context_path = (context_dir / f"{face.id}.jpg").resolve()
            context_image, scale = _resize_to_480p(image)
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
        for image in image_cache.values():
            image.close()
    return artifact_by_face_id


def _run_people_review(
    *,
    input_root: Path,
    backend: FaceDetectionBackend,
    min_score: float,
    max_distance: float,
    min_faces: int,
) -> tuple[ImmichLikeFaceEngine, list[Path], list[dict[str, Any]], list[dict[str, Any]], list[Any], list[Any]]:
    engine = ImmichLikeFaceEngine(
        backend=backend,
        min_score=min_score,
        max_distance=max_distance,
        min_faces=min_faces,
    )
    image_paths = _discover_images(input_root)
    asset_rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for index, image_path in enumerate(image_paths, start=1):
        asset_id = f"asset-{index:04d}"
        try:
            engine.add_asset(asset_id=asset_id, image_path=image_path)
            detect_result = engine.detect_asset_faces(asset_id)
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
    return engine, image_paths, asset_rows, errors, first_pass_results, second_pass_results


def _build_summary(
    *,
    input_root: Path,
    summary_json_path: Path,
    engine: ImmichLikeFaceEngine,
    image_paths: list[Path],
    asset_rows: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    first_pass_results: list[Any],
    second_pass_results: list[Any],
    min_score: float,
    max_distance: float,
    min_faces: int,
) -> dict[str, Any]:
    artifact_by_face_id = _generate_face_artifacts(summary_json_path=summary_json_path, engine=engine)
    person_assets: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    person_face_counts: Counter[str] = Counter()
    unassigned_assets: dict[str, dict[str, Any]] = {}

    for face in engine.faces.values():
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
        "face_count": len(engine.faces),
        "person_count": len(persons),
        "unassigned_face_count": sum(1 for face in engine.faces.values() if not face.person_id),
        "asset_with_faces_count": sum(1 for row in asset_rows if int(row["face_count"]) > 0),
        "failed_image_count": len(errors),
        "model": "insightface/buffalo_l",
        "max_distance": max_distance,
        "min_faces": min_faces,
        "min_score": min_score,
        "recognition_first_pass": dict(sorted(Counter(result.status for result in first_pass_results).items())),
        "recognition_second_pass": dict(sorted(Counter(result.status for result in second_pass_results).items())),
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
    min_score: float = 0.7,
    max_distance: float = 0.5,
    min_faces: int = 3,
) -> dict[str, Any]:
    input_root = Path(input_root).expanduser().resolve()
    summary_json_path = Path(summary_json_path).expanduser().resolve()
    if not input_root.exists():
        raise ValueError(f"输入目录不存在: {input_root}")
    if not input_root.is_dir():
        raise ValueError(f"输入路径不是目录: {input_root}")

    summary_json_path.parent.mkdir(parents=True, exist_ok=True)
    engine, image_paths, asset_rows, errors, first_pass_results, second_pass_results = _run_people_review(
        input_root=input_root,
        backend=backend,
        min_score=min_score,
        max_distance=max_distance,
        min_faces=min_faces,
    )
    summary = _build_summary(
        input_root=input_root,
        summary_json_path=summary_json_path,
        engine=engine,
        image_paths=image_paths,
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
