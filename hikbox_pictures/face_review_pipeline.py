from __future__ import annotations

import argparse
import gc
import hashlib
import html
import json
import shutil
import sqlite3
import subprocess
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import cv2
import numpy as np
import torch
from insightface.app import FaceAnalysis
from insightface.utils import face_align
from PIL import Image, ImageDraw, ImageOps
from pillow_heif import register_heif_opener

from ._magface_iresnet import iresnet100

register_heif_opener()

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".heif"}
MAGFACE_GOOGLE_DRIVE_ID = "1Bd87admxOZvbIOAyTkGEntsEz3fyMt7H"


@dataclass
class FaceObservation:
    face_id: str
    photo_relpath: str
    crop_relpath: str
    context_relpath: str
    preview_relpath: str
    bbox: tuple[int, int, int, int]
    detector_confidence: float
    face_area_ratio: float
    magface_quality: float
    quality_score: float
    cluster_label: int | None = None
    cluster_probability: float | None = None


def open_pipeline_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS detected_faces (
            face_id TEXT PRIMARY KEY,
            photo_relpath TEXT NOT NULL,
            crop_relpath TEXT NOT NULL,
            context_relpath TEXT NOT NULL,
            preview_relpath TEXT NOT NULL,
            aligned_relpath TEXT NOT NULL,
            bbox_json TEXT NOT NULL,
            detector_confidence REAL NOT NULL,
            face_area_ratio REAL NOT NULL,
            embedding_json TEXT,
            magface_quality REAL,
            quality_score REAL,
            cluster_label INTEGER,
            cluster_probability REAL,
            face_error TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS failed_images (
            photo_relpath TEXT PRIMARY KEY,
            error TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS pipeline_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_detected_faces_pending
        ON detected_faces(embedding_json, face_error);
        """
    )
    conn.commit()
    return conn


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        """
        INSERT INTO pipeline_meta(key, value, updated_at)
        VALUES(?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,
            updated_at=CURRENT_TIMESTAMP
        """,
        (key, json.dumps(value, ensure_ascii=False)),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM pipeline_meta WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    return json.loads(row["value"])


def reset_pipeline_state(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM detected_faces")
    conn.execute("DELETE FROM failed_images")
    conn.commit()


def upsert_detected_face(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO detected_faces(
            face_id, photo_relpath, crop_relpath, context_relpath, preview_relpath,
            aligned_relpath, bbox_json, detector_confidence, face_area_ratio,
            embedding_json, magface_quality, quality_score,
            cluster_label, cluster_probability, face_error, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, CURRENT_TIMESTAMP)
        ON CONFLICT(face_id) DO UPDATE SET
            photo_relpath=excluded.photo_relpath,
            crop_relpath=excluded.crop_relpath,
            context_relpath=excluded.context_relpath,
            preview_relpath=excluded.preview_relpath,
            aligned_relpath=excluded.aligned_relpath,
            bbox_json=excluded.bbox_json,
            detector_confidence=excluded.detector_confidence,
            face_area_ratio=excluded.face_area_ratio,
            embedding_json=NULL,
            magface_quality=NULL,
            quality_score=NULL,
            cluster_label=NULL,
            cluster_probability=NULL,
            face_error=NULL,
            updated_at=CURRENT_TIMESTAMP
        """,
        (
            row["face_id"],
            row["photo_relpath"],
            row["crop_relpath"],
            row["context_relpath"],
            row["preview_relpath"],
            row["aligned_relpath"],
            json.dumps(row["bbox"], ensure_ascii=False),
            float(row["detector_confidence"]),
            float(row["face_area_ratio"]),
        ),
    )
    conn.commit()


def iter_faces_pending_embedding(conn: sqlite3.Connection) -> Iterator[dict[str, Any]]:
    cursor = conn.execute(
        """
        SELECT face_id, photo_relpath, crop_relpath, context_relpath, preview_relpath,
               aligned_relpath, bbox_json, detector_confidence, face_area_ratio
        FROM detected_faces
        WHERE embedding_json IS NULL AND face_error IS NULL
        ORDER BY face_id
        """
    )
    for row in cursor:
        yield {
            "face_id": row["face_id"],
            "photo_relpath": row["photo_relpath"],
            "crop_relpath": row["crop_relpath"],
            "context_relpath": row["context_relpath"],
            "preview_relpath": row["preview_relpath"],
            "aligned_relpath": row["aligned_relpath"],
            "bbox": json.loads(row["bbox_json"]),
            "detector_confidence": float(row["detector_confidence"]),
            "face_area_ratio": float(row["face_area_ratio"]),
        }


def mark_face_embedded(
    conn: sqlite3.Connection,
    face_id: str,
    embedding: list[float],
    magface_quality: float,
    quality_score: float,
) -> None:
    conn.execute(
        """
        UPDATE detected_faces
        SET embedding_json=?, magface_quality=?, quality_score=?, face_error=NULL, updated_at=CURRENT_TIMESTAMP
        WHERE face_id=?
        """,
        (json.dumps(embedding, ensure_ascii=False), float(magface_quality), float(quality_score), face_id),
    )
    conn.commit()


def mark_face_error(conn: sqlite3.Connection, face_id: str, error: str) -> None:
    conn.execute(
        """
        UPDATE detected_faces
        SET face_error=?, updated_at=CURRENT_TIMESTAMP
        WHERE face_id=?
        """,
        (error, face_id),
    )
    conn.commit()


def iter_embedded_faces(conn: sqlite3.Connection) -> Iterator[dict[str, Any]]:
    cursor = conn.execute(
        """
        SELECT face_id, photo_relpath, crop_relpath, context_relpath, preview_relpath,
               bbox_json, detector_confidence, face_area_ratio,
               embedding_json, magface_quality, quality_score,
               cluster_label, cluster_probability
        FROM detected_faces
        WHERE embedding_json IS NOT NULL AND face_error IS NULL
        ORDER BY face_id
        """
    )
    for row in cursor:
        yield {
            "face_id": row["face_id"],
            "photo_relpath": row["photo_relpath"],
            "crop_relpath": row["crop_relpath"],
            "context_relpath": row["context_relpath"],
            "preview_relpath": row["preview_relpath"],
            "bbox": json.loads(row["bbox_json"]),
            "detector_confidence": float(row["detector_confidence"]),
            "face_area_ratio": float(row["face_area_ratio"]),
            "embedding": json.loads(row["embedding_json"]),
            "magface_quality": float(row["magface_quality"]),
            "quality_score": float(row["quality_score"]),
            "cluster_label": row["cluster_label"],
            "cluster_probability": row["cluster_probability"],
        }


def upsert_failed_image(conn: sqlite3.Connection, photo_relpath: str, error: str) -> None:
    conn.execute(
        """
        INSERT INTO failed_images(photo_relpath, error, updated_at)
        VALUES(?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(photo_relpath) DO UPDATE SET
            error=excluded.error,
            updated_at=CURRENT_TIMESTAMP
        """,
        (photo_relpath, error),
    )
    conn.commit()


def clear_failed_image(conn: sqlite3.Connection, photo_relpath: str) -> None:
    conn.execute("DELETE FROM failed_images WHERE photo_relpath=?", (photo_relpath,))
    conn.commit()


def list_failed_images(conn: sqlite3.Connection) -> list[dict[str, str]]:
    rows = conn.execute("SELECT photo_relpath, error FROM failed_images ORDER BY photo_relpath").fetchall()
    return [{"photo_relpath": row["photo_relpath"], "error": row["error"]} for row in rows]


def list_failed_faces(conn: sqlite3.Connection) -> list[dict[str, str]]:
    rows = conn.execute(
        "SELECT face_id, face_error FROM detected_faces WHERE face_error IS NOT NULL ORDER BY face_id"
    ).fetchall()
    return [{"face_id": row["face_id"], "error": row["face_error"]} for row in rows]


def count_all_faces(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM detected_faces").fetchone()
    return int(row["c"])


def count_pending_faces(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM detected_faces WHERE embedding_json IS NULL AND face_error IS NULL"
    ).fetchone()
    return int(row["c"])


def update_cluster_result(conn: sqlite3.Connection, face_id: str, label: int, probability: float) -> None:
    conn.execute(
        """
        UPDATE detected_faces
        SET cluster_label=?, cluster_probability=?, updated_at=CURRENT_TIMESTAMP
        WHERE face_id=?
        """,
        (int(label), float(probability), face_id),
    )
    conn.commit()


class MagFaceEmbedder:
    """MagFace embedding 推理器（官方 iResNet100 checkpoint）。"""

    def __init__(self, checkpoint_path: Path, device: str = "cpu") -> None:
        self.device = torch.device(device)
        self.model = iresnet100(num_classes=512)

        if not checkpoint_path.exists():
            self._download_checkpoint(checkpoint_path)
        checkpoint = torch.load(str(checkpoint_path), map_location=self.device)

        state_dict = checkpoint.get("state_dict", checkpoint)
        cleaned_state_dict = self._clean_state_dict(state_dict)
        missing, unexpected = self.model.load_state_dict(cleaned_state_dict, strict=False)
        if len(cleaned_state_dict) < 800:
            raise RuntimeError("MagFace checkpoint 加载字段过少，可能不是有效权重文件")
        if unexpected:
            print(f"[warn] MagFace unexpected keys: {len(unexpected)}")
        if missing:
            print(f"[warn] MagFace missing keys: {len(missing)}")

        self.model.eval()
        self.model.to(self.device)

    @staticmethod
    def _download_checkpoint(checkpoint_path: Path) -> None:
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import gdown
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "未安装 gdown，且 MagFace 权重不存在。请先安装 gdown 或手动下载权重。"
            ) from exc
        print("MagFace checkpoint 不存在，开始自动下载...")
        gdown.download(id=MAGFACE_GOOGLE_DRIVE_ID, output=str(checkpoint_path), quiet=False)

    def _clean_state_dict(self, state_dict: dict[str, torch.Tensor]) -> dict[str, torch.Tensor]:
        model_state_dict = self.model.state_dict()
        cleaned: dict[str, torch.Tensor] = {}

        for key, value in state_dict.items():
            candidates = [
                key,
                key.removeprefix("features.module."),
                key.removeprefix("module.features."),
                key.removeprefix("features."),
                ".".join(key.split(".")[2:]) if key.startswith("features.module.") else key,
            ]
            for candidate in candidates:
                if candidate in model_state_dict and tuple(model_state_dict[candidate].shape) == tuple(value.shape):
                    cleaned[candidate] = value
                    break

        return cleaned

    def embed(self, aligned_face_bgr_112: np.ndarray) -> tuple[list[float], float]:
        tensor = torch.from_numpy(np.ascontiguousarray(aligned_face_bgr_112.transpose(2, 0, 1)))
        tensor = tensor.float().div(255.0).unsqueeze(0).to(self.device)

        with torch.no_grad():
            embedding = self.model(tensor).detach().cpu().numpy()[0]

        magface_quality = float(np.linalg.norm(embedding))
        norm = float(np.linalg.norm(embedding))
        if norm <= 1e-9:
            normalized = embedding
        else:
            normalized = embedding / norm
        return normalized.astype(float).tolist(), magface_quality


def iter_image_files(root: Path) -> list[Path]:
    root = root.resolve()
    candidates: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        if path.suffix.lower() in IMAGE_SUFFIXES:
            candidates.append(path)
    return sorted(candidates, key=lambda item: item.relative_to(root).as_posix())


def group_faces_by_cluster(faces: list[dict[str, Any]], labels: list[int]) -> list[dict[str, Any]]:
    if len(faces) != len(labels):
        raise ValueError("faces 与 labels 数量不一致")

    buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for face, label in zip(faces, labels, strict=True):
        buckets[int(label)].append(face)

    normal_labels = [label for label in buckets if label != -1]
    normal_labels.sort(key=lambda label: (-len(buckets[label]), label))

    grouped: list[dict[str, Any]] = []
    for label in normal_labels:
        grouped.append(
            {
                "cluster_key": f"cluster_{label}",
                "cluster_label": label,
                "members": buckets[label],
            }
        )

    if -1 in buckets:
        grouped.append(
            {
                "cluster_key": "noise",
                "cluster_label": -1,
                "members": buckets[-1],
            }
        )

    return grouped


def render_review_html(payload: dict[str, Any]) -> str:
    meta = payload.get("meta", {})
    clusters = sorted(
        payload.get("clusters", []),
        key=lambda cluster: len(cluster.get("members", [])),
        reverse=True,
    )

    blocks: list[str] = []
    for cluster in clusters:
        members = cluster.get("members", [])
        member_cards: list[str] = []
        for member in members:
            face_id = html.escape(str(member.get("face_id", "")))
            crop_relpath = html.escape(str(member.get("crop_relpath", "")))
            context_relpath = html.escape(str(member.get("context_relpath", "")))
            preview_relpath = html.escape(str(member.get("preview_relpath", "")))
            quality_score = float(member.get("quality_score", 0.0))
            magface_quality = float(member.get("magface_quality", 0.0))
            prob = member.get("cluster_probability")
            prob_text = "-" if prob is None else f"{float(prob):.3f}"

            member_cards.append(
                f"""
                <article class=\"face-card\">
                  <header>
                    <strong>{face_id}</strong>
                    <span>Q={quality_score:.3f} · M={magface_quality:.2f} · P={prob_text}</span>
                  </header>
                  <div class=\"thumb-grid\">
                    <a href=\"{crop_relpath}\" target=\"_blank\"><img src=\"{crop_relpath}\" alt=\"crop {face_id}\"></a>
                    <a href=\"{context_relpath}\" target=\"_blank\"><img src=\"{context_relpath}\" alt=\"context {face_id}\"></a>
                    <a href=\"{preview_relpath}\" target=\"_blank\"><img src=\"{preview_relpath}\" alt=\"preview {face_id}\"></a>
                  </div>
                </article>
                """
            )

        cluster_key = html.escape(str(cluster.get("cluster_key", "")))
        cluster_label = cluster.get("cluster_label")
        cluster_size = len(members)
        blocks.append(
            f"""
            <details class=\"cluster panel\">
              <summary class=\"cluster-title\">
                <h3>{cluster_key}</h3>
                <span class=\"cluster-meta\">label={cluster_label} · members={cluster_size}</span>
              </summary>
              <div class=\"face-grid\">
                {''.join(member_cards)}
              </div>
            </details>
            """
        )

    model = html.escape(str(meta.get("model", "MagFace")))
    clusterer = html.escape(str(meta.get("clusterer", "HDBSCAN")))
    source = html.escape(str(meta.get("source", "")))
    image_count = int(meta.get("image_count", 0))
    face_count = int(meta.get("face_count", 0))
    cluster_count = int(meta.get("cluster_count", 0))
    noise_count = int(meta.get("noise_count", 0))

    return f"""<!DOCTYPE html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"UTF-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
  <title>MagFace + HDBSCAN 人脸归类 Review</title>
  <style>
    :root {{
      --bg: #f6f8fb;
      --panel: #ffffff;
      --line: #d7deea;
      --text: #1a2330;
      --sub: #5a6b82;
      --brand: #1f5eff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
      color: var(--text);
      background: radial-gradient(circle at top right, #e8f0ff, var(--bg));
    }}
    .topbar {{
      position: sticky;
      top: 0;
      z-index: 10;
      border-bottom: 1px solid var(--line);
      background: rgba(255,255,255,0.9);
      backdrop-filter: blur(8px);
      padding: 14px 24px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
    }}
    .topbar h1 {{ margin: 0; font-size: 20px; }}
    .topbar .meta {{ color: var(--sub); font-size: 13px; }}
    .content {{ padding: 24px; max-width: 1600px; margin: 0 auto; }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 14px;
      box-shadow: 0 8px 20px rgba(35, 60, 130, 0.06);
      margin-bottom: 16px;
    }}
    .summary-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 10px;
    }}
    .summary-grid div {{
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px;
      background: #fcfdff;
    }}
    .summary-grid dt {{ margin: 0 0 6px; color: var(--sub); font-size: 12px; }}
    .summary-grid dd {{ margin: 0; font-weight: 600; }}
    .cluster-title {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 12px;
      margin: 0;
      padding: 12px 14px;
      cursor: pointer;
      user-select: none;
    }}
    .cluster-title h3 {{ margin: 0; }}
    .cluster-meta {{ color: var(--sub); font-size: 13px; }}
    details.cluster > summary::-webkit-details-marker {{ display: none; }}
    details.cluster > summary::after {{
      content: "展开";
      margin-left: 10px;
      color: var(--brand);
      font-size: 12px;
      font-weight: 600;
    }}
    details.cluster[open] > summary {{
      border-bottom: 1px dashed var(--line);
      margin-bottom: 12px;
    }}
    details.cluster[open] > summary::after {{ content: "收起"; }}
    .face-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(330px, 1fr));
      gap: 10px;
      padding: 0 14px 14px;
    }}
    .face-card {{
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 8px;
      background: #fff;
    }}
    .face-card header {{
      display: flex;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 8px;
      font-size: 12px;
    }}
    .thumb-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 6px;
    }}
    .thumb-grid a {{
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      display: block;
      background: #f2f5fb;
    }}
    .thumb-grid img {{
      display: block;
      width: 100%;
      height: 150px;
      object-fit: cover;
    }}
    @media (max-width: 768px) {{
      .topbar {{ padding: 12px; align-items: flex-start; flex-direction: column; }}
      .content {{ padding: 12px; }}
      .face-grid {{ grid-template-columns: 1fr; }}
      .thumb-grid img {{ height: 120px; }}
    }}
  </style>
</head>
<body>
  <header class=\"topbar\">
    <h1>MagFace + HDBSCAN 人物归类 Review</h1>
    <div class=\"meta\">{source}</div>
  </header>
  <main class=\"content\">
    <section class=\"panel\">
      <dl class=\"summary-grid\">
        <div><dt>Embedding 模型</dt><dd>{model}</dd></div>
        <div><dt>聚类算法</dt><dd>{clusterer}</dd></div>
        <div><dt>图片数</dt><dd>{image_count}</dd></div>
        <div><dt>人脸数</dt><dd>{face_count}</dd></div>
        <div><dt>簇数</dt><dd>{cluster_count}</dd></div>
        <div><dt>噪声数</dt><dd>{noise_count}</dd></div>
      </dl>
    </section>
    {''.join(blocks)}
  </main>
</body>
</html>
"""


def _safe_bbox(bbox: np.ndarray, width: int, height: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = [int(v) for v in bbox.tolist()]
    x1 = max(0, min(x1, width - 1))
    y1 = max(0, min(y1, height - 1))
    x2 = max(x1 + 1, min(x2, width))
    y2 = max(y1 + 1, min(y2, height))
    return x1, y1, x2, y2


def _load_rgb_image(path: Path) -> Image.Image:
    with Image.open(path) as image:
        normalized = ImageOps.exif_transpose(image)
        return normalized.convert("RGB")


def _make_preview(image: Image.Image, max_side: int) -> tuple[Image.Image, float]:
    width, height = image.size
    scale = min(1.0, float(max_side) / float(max(width, height)))
    if scale >= 1.0:
        return image.copy(), 1.0
    resized = image.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.Resampling.LANCZOS)
    return resized, scale


def _make_crop(image: Image.Image, bbox: tuple[int, int, int, int], pad_ratio: float = 0.25) -> Image.Image:
    x1, y1, x2, y2 = bbox
    width, height = image.size
    bw = x2 - x1
    bh = y2 - y1
    pad_w = int(bw * pad_ratio)
    pad_h = int(bh * pad_ratio)
    cx1 = max(0, x1 - pad_w)
    cy1 = max(0, y1 - pad_h)
    cx2 = min(width, x2 + pad_w)
    cy2 = min(height, y2 + pad_h)
    crop = image.crop((cx1, cy1, cx2, cy2))
    return ImageOps.fit(crop, (256, 256), Image.Resampling.LANCZOS)


def _make_context(preview: Image.Image, bbox: tuple[int, int, int, int], scale: float) -> Image.Image:
    px1 = int(bbox[0] * scale)
    py1 = int(bbox[1] * scale)
    px2 = int(bbox[2] * scale)
    py2 = int(bbox[3] * scale)

    canvas = preview.copy()
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((px1, py1, px2, py2), outline="#ff3b30", width=3)
    return canvas


def _cluster_with_hdbscan(
    embeddings: list[list[float]],
    min_cluster_size: int,
    min_samples: int,
) -> tuple[list[int], list[float]]:
    import hdbscan

    if not embeddings:
        return [], []
    if len(embeddings) < max(2, min_cluster_size):
        return [-1 for _ in embeddings], [0.0 for _ in embeddings]

    vectors = np.asarray(embeddings, dtype=np.float32)
    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=max(2, int(min_cluster_size)),
        min_samples=max(1, int(min_samples)),
        metric="euclidean",
        cluster_selection_method="eom",
        prediction_data=False,
    )
    labels = clusterer.fit_predict(vectors).tolist()
    probabilities = clusterer.probabilities_.astype(float).tolist()
    return labels, probabilities


def _ensure_dirs(output_dir: Path, reset_output: bool) -> dict[str, Path]:
    if reset_output and output_dir.exists():
        shutil.rmtree(output_dir)

    (output_dir / "assets" / "crops").mkdir(parents=True, exist_ok=True)
    (output_dir / "assets" / "context").mkdir(parents=True, exist_ok=True)
    (output_dir / "assets" / "preview").mkdir(parents=True, exist_ok=True)
    (output_dir / "assets" / "aligned").mkdir(parents=True, exist_ok=True)
    (output_dir / "cache").mkdir(parents=True, exist_ok=True)

    return {
        "crop": output_dir / "assets" / "crops",
        "context": output_dir / "assets" / "context",
        "preview": output_dir / "assets" / "preview",
        "aligned": output_dir / "assets" / "aligned",
        "db": output_dir / "cache" / "pipeline.db",
    }


def run_detection_stage(
    source_dir: Path,
    output_dir: Path,
    insightface_root: Path,
    detector_model_name: str,
    det_size: int,
    preview_max_side: int,
    max_images: int | None,
    reset_output: bool,
) -> dict[str, Any]:
    dirs = _ensure_dirs(output_dir, reset_output=reset_output)
    conn = open_pipeline_db(dirs["db"])
    if reset_output:
        reset_pipeline_state(conn)

    image_paths = iter_image_files(source_dir)
    if max_images is not None and max_images > 0:
        image_paths = image_paths[:max_images]

    set_meta(conn, "source", str(source_dir))
    set_meta(conn, "detector_model_name", detector_model_name)
    set_meta(conn, "det_size", det_size)
    set_meta(conn, "preview_max_side", preview_max_side)
    set_meta(conn, "max_images", max_images)
    set_meta(conn, "last_detection_at", datetime.now().isoformat(timespec="seconds"))

    print("阶段 detect：检测 + 预处理")
    detector = FaceAnalysis(name=detector_model_name, root=str(insightface_root), allowed_modules=["detection"])
    detector.prepare(ctx_id=-1, det_size=(det_size, det_size))

    total = len(image_paths)
    for idx, image_path in enumerate(image_paths, start=1):
        relpath = image_path.relative_to(source_dir).as_posix()
        print(f"[det {idx}/{total}] 处理 {relpath}")

        try:
            rgb_image = _load_rgb_image(image_path)
            rgb_arr = np.asarray(rgb_image)
            bgr_arr = cv2.cvtColor(rgb_arr, cv2.COLOR_RGB2BGR)
            height, width = bgr_arr.shape[:2]

            preview, preview_scale = _make_preview(rgb_image, max_side=preview_max_side)
            photo_key = hashlib.sha1(relpath.encode("utf-8")).hexdigest()[:16]
            preview_name = f"{photo_key}.jpg"
            preview_relpath = f"assets/preview/{preview_name}"
            preview.save(dirs["preview"] / preview_name, format="JPEG", quality=88)

            faces = detector.get(bgr_arr)
            clear_failed_image(conn, relpath)
            for face_idx, face in enumerate(faces):
                if getattr(face, "kps", None) is None:
                    continue

                bbox = _safe_bbox(face.bbox, width=width, height=height)
                x1, y1, x2, y2 = bbox
                area_ratio = float((x2 - x1) * (y2 - y1) / max(1, width * height))
                det_conf = float(getattr(face, "det_score", 0.0))

                face_id = f"{photo_key}_{face_idx:03d}"
                crop_name = f"{face_id}.jpg"
                context_name = f"{face_id}.jpg"
                aligned_name = f"{face_id}.png"
                aligned_relpath = f"assets/aligned/{aligned_name}"

                crop_img = _make_crop(image=rgb_image, bbox=bbox)
                crop_img.save(dirs["crop"] / crop_name, format="JPEG", quality=92)

                context_img = _make_context(preview=preview, bbox=bbox, scale=preview_scale)
                context_img.save(dirs["context"] / context_name, format="JPEG", quality=88)

                aligned_bgr = face_align.norm_crop(bgr_arr, face.kps, image_size=112)
                cv2.imwrite(str(dirs["aligned"] / aligned_name), aligned_bgr)

                upsert_detected_face(
                    conn,
                    {
                        "face_id": face_id,
                        "photo_relpath": relpath,
                        "crop_relpath": f"assets/crops/{crop_name}",
                        "context_relpath": f"assets/context/{context_name}",
                        "preview_relpath": preview_relpath,
                        "aligned_relpath": aligned_relpath,
                        "bbox": [x1, y1, x2, y2],
                        "detector_confidence": det_conf,
                        "face_area_ratio": area_ratio,
                    },
                )
        except Exception as exc:  # pragma: no cover
            upsert_failed_image(conn, relpath, str(exc))

    del detector
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    summary = {
        "image_count": len(image_paths),
        "detected_face_count": count_all_faces(conn),
        "pending_face_count": count_pending_faces(conn),
        "failed_image_count": len(list_failed_images(conn)),
        "db_path": str(dirs["db"]),
    }
    conn.close()
    return summary


def run_embedding_stage(
    output_dir: Path,
    magface_checkpoint: Path,
) -> dict[str, Any]:
    dirs = _ensure_dirs(output_dir, reset_output=False)
    conn = open_pipeline_db(dirs["db"])

    pending_rows = list(iter_faces_pending_embedding(conn))
    pending_count = len(pending_rows)
    print(f"阶段 embed：MagFace embedding（待处理 {pending_count}）")

    if pending_count == 0:
        summary = {
            "pending_face_count": 0,
            "embedded_face_count": len(list(iter_embedded_faces(conn))),
            "failed_face_count": len(list_failed_faces(conn)),
            "db_path": str(dirs["db"]),
        }
        conn.close()
        return summary

    embedder = MagFaceEmbedder(checkpoint_path=magface_checkpoint)
    for idx, row in enumerate(pending_rows, start=1):
        try:
            aligned_path = output_dir / str(row["aligned_relpath"])
            aligned_bgr = cv2.imread(str(aligned_path), cv2.IMREAD_COLOR)
            if aligned_bgr is None:
                raise FileNotFoundError(f"aligned 文件不存在或无法读取: {aligned_path}")

            embedding, magface_quality = embedder.embed(aligned_bgr)
            det_conf = float(row["detector_confidence"])
            area_ratio = float(row["face_area_ratio"])
            quality_score = float(magface_quality * max(0.05, det_conf) * np.sqrt(max(area_ratio, 1e-9)))

            mark_face_embedded(
                conn,
                face_id=str(row["face_id"]),
                embedding=embedding,
                magface_quality=magface_quality,
                quality_score=quality_score,
            )
        except Exception as exc:  # pragma: no cover
            mark_face_error(conn, str(row.get("face_id", "")), str(exc))

        if idx % 200 == 0 or idx == pending_count:
            print(f"[emb {idx}/{pending_count}]")

    del embedder
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    set_meta(conn, "magface_checkpoint", str(magface_checkpoint))
    set_meta(conn, "last_embedding_at", datetime.now().isoformat(timespec="seconds"))

    summary = {
        "pending_face_count": pending_count,
        "embedded_face_count": len(list(iter_embedded_faces(conn))),
        "failed_face_count": len(list_failed_faces(conn)),
        "db_path": str(dirs["db"]),
    }
    conn.close()
    return summary


def run_cluster_stage(
    source_dir: Path,
    output_dir: Path,
    detector_model_name: str,
    det_size: int,
    min_cluster_size: int,
    min_samples: int,
    preview_max_side: int,
    magface_checkpoint: Path,
) -> dict[str, Any]:
    dirs = _ensure_dirs(output_dir, reset_output=False)
    conn = open_pipeline_db(dirs["db"])

    failed_images = list_failed_images(conn)
    failed_faces = list_failed_faces(conn)

    observations: list[FaceObservation] = []
    embeddings: list[list[float]] = []
    for row in iter_embedded_faces(conn):
        bbox_values = [int(v) for v in row["bbox"]]
        embeddings.append(list(row["embedding"]))
        observations.append(
            FaceObservation(
                face_id=str(row["face_id"]),
                photo_relpath=str(row["photo_relpath"]),
                crop_relpath=str(row["crop_relpath"]),
                context_relpath=str(row["context_relpath"]),
                preview_relpath=str(row["preview_relpath"]),
                bbox=(bbox_values[0], bbox_values[1], bbox_values[2], bbox_values[3]),
                detector_confidence=float(row["detector_confidence"]),
                face_area_ratio=float(row["face_area_ratio"]),
                magface_quality=float(row["magface_quality"]),
                quality_score=float(row["quality_score"]),
            )
        )

    labels, probabilities = _cluster_with_hdbscan(
        embeddings,
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
    )
    del embeddings

    for obs, label, probability in zip(observations, labels, probabilities, strict=True):
        obs.cluster_label = int(label)
        obs.cluster_probability = float(probability)
        update_cluster_result(conn, obs.face_id, int(label), float(probability))

    face_rows = [{**asdict(obs), "bbox": list(obs.bbox)} for obs in observations]
    face_rows.sort(key=lambda row: (1 if row.get("cluster_label") == -1 else 0, -(row.get("quality_score") or 0.0)))

    grouped_clusters = group_faces_by_cluster(
        faces=face_rows,
        labels=[int(row.get("cluster_label", -1)) for row in face_rows],
    )

    for cluster in grouped_clusters:
        cluster["members"].sort(key=lambda row: -(row.get("quality_score") or 0.0))

    image_count = int(get_meta(conn, "max_images", 0) or 0)
    if image_count <= 0:
        image_count = len(iter_image_files(source_dir))

    payload: dict[str, Any] = {
        "meta": {
            "source": str(source_dir),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "model": "MagFace(iResNet100)",
            "clusterer": "HDBSCAN",
            "detector": f"insightface:{detector_model_name}",
            "pipeline_mode": "sqlite_staged",
            "det_size": det_size,
            "image_count": image_count,
            "face_count": len(face_rows),
            "detected_face_count": count_all_faces(conn),
            "cluster_count": len([c for c in grouped_clusters if c["cluster_key"] != "noise"]),
            "noise_count": len(next((c["members"] for c in grouped_clusters if c["cluster_key"] == "noise"), [])),
            "min_cluster_size": min_cluster_size,
            "min_samples": min_samples,
            "preview_max_side": preview_max_side,
            "magface_checkpoint": str(magface_checkpoint),
            "db_path": str(dirs["db"]),
        },
        "failed_images": failed_images,
        "failed_faces": failed_faces,
        "clusters": grouped_clusters,
    }

    (output_dir / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "review.html").write_text(render_review_html(payload), encoding="utf-8")

    conn.close()
    return payload


def run_pipeline(
    source_dir: Path,
    output_dir: Path,
    magface_checkpoint: Path,
    insightface_root: Path,
    detector_model_name: str,
    det_size: int,
    min_cluster_size: int,
    min_samples: int,
    preview_max_side: int,
    max_images: int | None,
    stage: str,
    reset_output: bool,
) -> dict[str, Any]:
    source_dir = source_dir.resolve()
    output_dir = output_dir.resolve()

    if not source_dir.exists():
        raise FileNotFoundError(f"图库目录不存在: {source_dir}")

    if stage == "detect":
        summary = run_detection_stage(
            source_dir=source_dir,
            output_dir=output_dir,
            insightface_root=insightface_root,
            detector_model_name=detector_model_name,
            det_size=det_size,
            preview_max_side=preview_max_side,
            max_images=max_images,
            reset_output=reset_output,
        )
        return {"meta": {"stage": "detect", **summary}}

    if stage == "embed":
        summary = run_embedding_stage(
            output_dir=output_dir,
            magface_checkpoint=magface_checkpoint,
        )
        return {"meta": {"stage": "embed", **summary}}

    if stage == "cluster":
        return run_cluster_stage(
            source_dir=source_dir,
            output_dir=output_dir,
            detector_model_name=detector_model_name,
            det_size=det_size,
            min_cluster_size=min_cluster_size,
            min_samples=min_samples,
            preview_max_side=preview_max_side,
            magface_checkpoint=magface_checkpoint,
        )

    # all 阶段通过子进程串行执行，确保每个阶段释放内存。
    print("all 模式：将按 detect -> embed -> cluster 三个子进程执行")
    base_cmd = [
        sys.executable,
        "-m",
        "hikbox_pictures.face_review_pipeline",
        "--source",
        str(source_dir),
        "--output",
        str(output_dir),
        "--magface-checkpoint",
        str(magface_checkpoint),
        "--insightface-root",
        str(insightface_root),
        "--detector-model-name",
        detector_model_name,
        "--det-size",
        str(det_size),
        "--min-cluster-size",
        str(min_cluster_size),
        "--min-samples",
        str(min_samples),
        "--preview-max-side",
        str(preview_max_side),
    ]
    if max_images is not None:
        base_cmd.extend(["--max-images", str(max_images)])

    detect_cmd = base_cmd + ["--stage", "detect"]
    if reset_output:
        detect_cmd.append("--reset-output")
    subprocess.run(detect_cmd, check=True)
    subprocess.run(base_cmd + ["--stage", "embed"], check=True)
    subprocess.run(base_cmd + ["--stage", "cluster"], check=True)

    manifest = output_dir / "manifest.json"
    if not manifest.exists():
        raise RuntimeError("all 模式执行后未找到 manifest.json")
    return json.loads(manifest.read_text(encoding="utf-8"))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MagFace + HDBSCAN 人脸归类并生成本地 review 页面")
    parser.add_argument("--source", type=Path, default=Path(".hikbox"), help="图库根目录")
    parser.add_argument("--output", type=Path, default=Path(".tmp/magface_hdbscan_review"), help="输出目录")
    parser.add_argument(
        "--magface-checkpoint",
        type=Path,
        default=Path(".cache/magface/magface_iresnet100_ms1mv2.pth"),
        help="MagFace iResNet100 权重路径",
    )
    parser.add_argument("--insightface-root", type=Path, default=Path(".cache/insightface"), help="insightface 模型缓存目录")
    parser.add_argument("--detector-model-name", type=str, default="buffalo_l", help="insightface detector model")
    parser.add_argument("--det-size", type=int, default=640, help="检测分辨率")
    parser.add_argument("--min-cluster-size", type=int, default=3, help="HDBSCAN min_cluster_size")
    parser.add_argument("--min-samples", type=int, default=2, help="HDBSCAN min_samples")
    parser.add_argument("--preview-max-side", type=int, default=480, help="预览图最长边")
    parser.add_argument("--max-images", type=int, default=None, help="仅处理前 N 张图片（调试）")
    parser.add_argument("--stage", choices=["all", "detect", "embed", "cluster"], default="all", help="分阶段执行")
    parser.add_argument("--reset-output", action="store_true", help="执行 detect 时先清空输出目录与数据库")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    payload = run_pipeline(
        source_dir=args.source,
        output_dir=args.output,
        magface_checkpoint=args.magface_checkpoint,
        insightface_root=args.insightface_root,
        detector_model_name=args.detector_model_name,
        det_size=args.det_size,
        min_cluster_size=args.min_cluster_size,
        min_samples=args.min_samples,
        preview_max_side=args.preview_max_side,
        max_images=args.max_images,
        stage=args.stage,
        reset_output=args.reset_output,
    )

    meta = payload.get("meta", {})
    if args.stage in {"detect", "embed"}:
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        return 0

    print(
        "完成："
        f"images={meta.get('image_count')} "
        f"faces={meta.get('face_count')} "
        f"clusters={meta.get('cluster_count')} "
        f"noise={meta.get('noise_count')}"
    )
    print(f"HTML: {args.output / 'review.html'}")
    print(f"JSON: {args.output / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
