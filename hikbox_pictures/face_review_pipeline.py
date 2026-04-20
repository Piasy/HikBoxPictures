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
from collections import Counter, defaultdict
from dataclasses import dataclass
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
DEFAULT_DETECT_MAX_IMAGES_PER_RUN_IN_ALL_STAGE = 120


@dataclass
class FaceObservation:
    face_id: str
    photo_relpath: str
    crop_relpath: str
    context_relpath: str
    bbox: tuple[int, int, int, int]
    embedding: list[float]
    detector_confidence: float
    face_area_ratio: float
    magface_quality: float
    quality_score: float
    cluster_label: int | None = None
    cluster_probability: float | None = None
    cluster_assignment_source: str | None = None
    quality_gate_excluded: bool = False


def _ensure_detected_faces_schema(conn: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(detected_faces)").fetchall()
    }
    if "cluster_assignment_source" not in columns:
        conn.execute("ALTER TABLE detected_faces ADD COLUMN cluster_assignment_source TEXT")
        conn.commit()

    conn.execute(
        """
        UPDATE detected_faces
        SET cluster_assignment_source = CASE
            WHEN cluster_label IS NULL THEN NULL
            WHEN cluster_label = -1 THEN 'noise'
            WHEN cluster_probability IS NULL THEN 'person_consensus'
            ELSE 'hdbscan'
        END
        WHERE cluster_assignment_source IS NULL
        """
    )
    conn.commit()


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
            cluster_assignment_source TEXT,
            face_error TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS failed_images (
            photo_relpath TEXT PRIMARY KEY,
            error TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS processed_images (
            photo_relpath TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            face_count INTEGER NOT NULL DEFAULT 0,
            error TEXT,
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
    _ensure_detected_faces_schema(conn)
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
    conn.execute("DELETE FROM processed_images")
    conn.commit()


def upsert_processed_image(
    conn: sqlite3.Connection,
    photo_relpath: str,
    status: str,
    face_count: int = 0,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO processed_images(photo_relpath, status, face_count, error, updated_at)
        VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(photo_relpath) DO UPDATE SET
            status=excluded.status,
            face_count=excluded.face_count,
            error=excluded.error,
            updated_at=CURRENT_TIMESTAMP
        """,
        (photo_relpath, status, int(face_count), error),
    )
    conn.commit()


def upsert_detected_face(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO detected_faces(
            face_id, photo_relpath, crop_relpath, context_relpath, preview_relpath,
            aligned_relpath, bbox_json, detector_confidence, face_area_ratio,
            embedding_json, magface_quality, quality_score,
            cluster_label, cluster_probability, cluster_assignment_source, face_error, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, CURRENT_TIMESTAMP)
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
            cluster_assignment_source=NULL,
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
               cluster_label, cluster_probability, cluster_assignment_source
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
            "cluster_assignment_source": row["cluster_assignment_source"],
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


def update_cluster_result(
    conn: sqlite3.Connection,
    face_id: str,
    label: int,
    probability: float | None,
    assignment_source: str | None,
) -> None:
    conn.execute(
        """
        UPDATE detected_faces
        SET cluster_label=?, cluster_probability=?, cluster_assignment_source=?, updated_at=CURRENT_TIMESTAMP
        WHERE face_id=?
        """,
        (
            int(label),
            None if probability is None else float(probability),
            assignment_source,
            face_id,
        ),
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


def compute_detect_workset_stats(
    total_images: int,
    max_images: int | None,
    processed_count: int,
    detect_max_images_per_run: int | None = None,
) -> tuple[int, int, int]:
    effective_total = int(total_images)
    if max_images is not None and int(max_images) > 0:
        effective_total = min(effective_total, int(max_images))
    effective_total = max(effective_total, 0)

    effective_processed = max(int(processed_count), 0)
    remaining = max(effective_total - effective_processed, 0)

    if detect_max_images_per_run is not None and int(detect_max_images_per_run) > 0:
        batch_size = int(detect_max_images_per_run)
    else:
        batch_size = DEFAULT_DETECT_MAX_IMAGES_PER_RUN_IN_ALL_STAGE

    return effective_total, remaining, batch_size


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


def _normalize_embedding(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-9:
        return vector.astype(np.float32)
    return (vector / norm).astype(np.float32)


def _build_cluster_representative(cluster: dict[str, Any], rep_top_k: int) -> np.ndarray | None:
    members = list(cluster.get("members", []))
    if not members:
        return None

    sorted_members = sorted(members, key=lambda row: -(row.get("quality_score") or 0.0))
    top_k = max(1, int(rep_top_k))

    vectors: list[np.ndarray] = []
    weights: list[float] = []
    dim: int | None = None
    for member in sorted_members:
        raw_embedding = member.get("embedding")
        if not isinstance(raw_embedding, list) or not raw_embedding:
            continue
        vector = np.asarray(raw_embedding, dtype=np.float32)
        if vector.ndim != 1:
            continue
        if dim is None:
            dim = int(vector.shape[0])
        if int(vector.shape[0]) != dim:
            continue
        vectors.append(_normalize_embedding(vector))
        weights.append(max(float(member.get("quality_score") or 0.0), 1e-3))
        if len(vectors) >= top_k:
            break

    if not vectors:
        return None

    vectors_np = np.stack(vectors, axis=0)
    weights_np = np.asarray(weights, dtype=np.float32)
    weighted_sum = np.sum(vectors_np * weights_np[:, None], axis=0)
    return _normalize_embedding(weighted_sum)


def attach_noise_faces_to_person_consensus(
    faces: list[dict[str, Any]],
    labels: list[int],
    probabilities: list[float | None],
    persons: list[dict[str, Any]],
    rep_top_k: int,
    distance_threshold: float,
    margin_threshold: float,
) -> tuple[list[int], list[float | None], int]:
    if len(faces) != len(labels) or len(faces) != len(probabilities):
        raise ValueError("faces、labels、probabilities 数量不一致")

    if distance_threshold <= 0:
        return list(labels), list(probabilities), 0

    if not persons:
        return list(labels), list(probabilities), 0

    grouped_clusters = group_faces_by_cluster(faces=faces, labels=labels)
    normal_clusters = [cluster for cluster in grouped_clusters if int(cluster.get("cluster_label", -1)) != -1]
    if not normal_clusters:
        return list(labels), list(probabilities), 0

    cluster_representatives: dict[int, np.ndarray] = {}
    for cluster in normal_clusters:
        representative = _build_cluster_representative(cluster, rep_top_k=rep_top_k)
        if representative is None:
            continue
        cluster_representatives[int(cluster.get("cluster_label", -1))] = representative

    if not cluster_representatives:
        return list(labels), list(probabilities), 0

    person_cluster_labels: list[list[int]] = []
    for person in persons:
        candidate_labels: list[int] = []
        for cluster_ref in person.get("clusters", []):
            label = int(cluster_ref.get("cluster_label", -1))
            if label in cluster_representatives:
                candidate_labels.append(label)
        if candidate_labels:
            person_cluster_labels.append(candidate_labels)

    if not person_cluster_labels:
        return list(labels), list(probabilities), 0

    updated_labels = list(labels)
    updated_probabilities = list(probabilities)
    attached_count = 0

    for idx, (face, label) in enumerate(zip(faces, labels, strict=True)):
        if int(label) != -1:
            continue
        if bool(face.get("quality_gate_excluded", False)):
            continue

        raw_embedding = face.get("embedding")
        if not isinstance(raw_embedding, list) or not raw_embedding:
            continue

        vector = np.asarray(raw_embedding, dtype=np.float32)
        if vector.ndim != 1:
            continue
        vector = _normalize_embedding(vector)

        person_candidates: list[tuple[float, float, int]] = []
        for cluster_labels in person_cluster_labels:
            best_label: int | None = None
            best_sim: float | None = None
            for cluster_label in cluster_labels:
                representative = cluster_representatives[cluster_label]
                sim = float(np.clip(np.dot(representative, vector), -1.0, 1.0))
                if best_sim is None or sim > best_sim:
                    best_sim = sim
                    best_label = cluster_label
            if best_label is None or best_sim is None:
                continue
            person_candidates.append((1.0 - best_sim, best_sim, best_label))

        if not person_candidates:
            continue
        person_candidates.sort(key=lambda item: item[0])

        best_dist, best_sim, best_label = person_candidates[0]
        second_sim = person_candidates[1][1] if len(person_candidates) >= 2 else -1.0
        margin = best_sim - second_sim

        if best_dist > float(distance_threshold):
            continue
        if margin < float(margin_threshold):
            continue

        updated_labels[idx] = best_label
        # 这里的回挂置信不是 HDBSCAN 原生 probability，review 中留空避免误解。
        updated_probabilities[idx] = None
        attached_count += 1

    return updated_labels, updated_probabilities, attached_count


def exclude_low_quality_faces_from_assignment(
    faces: list[dict[str, Any]],
    labels: list[int],
    probabilities: list[float | None],
    min_quality_score: float | None,
) -> tuple[list[int], list[float | None], list[bool], int]:
    if len(faces) != len(labels) or len(faces) != len(probabilities):
        raise ValueError("faces、labels、probabilities 数量不一致")

    if min_quality_score is None or float(min_quality_score) <= 0:
        return list(labels), list(probabilities), [False for _ in faces], 0

    updated_labels = list(labels)
    updated_probabilities = list(probabilities)
    excluded_flags = [False for _ in faces]
    excluded_count = 0
    threshold = float(min_quality_score)

    for idx, face in enumerate(faces):
        quality_score = float(face.get("quality_score") or 0.0)
        if quality_score >= threshold:
            continue
        excluded_flags[idx] = True
        excluded_count += 1
        updated_labels[idx] = -1
        updated_probabilities[idx] = 0.0

    return updated_labels, updated_probabilities, excluded_flags, excluded_count


def demote_low_quality_micro_clusters(
    faces: list[dict[str, Any]],
    labels: list[int],
    probabilities: list[float | None],
    max_cluster_size: int,
    top2_weight: float,
    min_quality_evidence: float,
) -> tuple[list[int], list[float | None], int, int]:
    if len(faces) != len(labels) or len(faces) != len(probabilities):
        raise ValueError("faces、labels、probabilities 数量不一致")

    if int(max_cluster_size) <= 0 or float(min_quality_evidence) <= 0:
        return list(labels), list(probabilities), 0, 0

    safe_top2_weight = max(0.0, float(top2_weight))
    updated_labels = list(labels)
    updated_probabilities = list(probabilities)
    demoted_cluster_count = 0
    demoted_face_count = 0

    cluster_to_indices: dict[int, list[int]] = defaultdict(list)
    for idx, label in enumerate(labels):
        cluster_label = int(label)
        if cluster_label == -1:
            continue
        cluster_to_indices[cluster_label].append(idx)

    for indices in cluster_to_indices.values():
        if len(indices) > int(max_cluster_size):
            continue

        quality_values = sorted(
            [float(faces[idx].get("quality_score") or 0.0) for idx in indices],
            reverse=True,
        )
        if not quality_values:
            continue

        top1 = quality_values[0]
        top2 = quality_values[1] if len(quality_values) >= 2 else 0.0
        quality_evidence = top1 + safe_top2_weight * top2
        if quality_evidence >= float(min_quality_evidence):
            continue

        demoted_cluster_count += 1
        demoted_face_count += len(indices)
        for idx in indices:
            updated_labels[idx] = -1
            updated_probabilities[idx] = 0.0

    return updated_labels, updated_probabilities, demoted_cluster_count, demoted_face_count


def _pairwise_cosine_distance(vectors: np.ndarray) -> np.ndarray:
    sims = np.clip(vectors @ vectors.T, -1.0, 1.0)
    dist = 1.0 - sims
    dist = np.maximum(dist, 0.0)
    np.fill_diagonal(dist, 0.0)
    return dist.astype(np.float32)


def _build_cluster_knn_mask(dist_matrix: np.ndarray, knn_k: int) -> np.ndarray:
    size = int(dist_matrix.shape[0])
    mask = np.zeros((size, size), dtype=bool)
    if size <= 0:
        return mask

    np.fill_diagonal(mask, True)
    if size == 1:
        return mask

    effective_k = max(1, min(int(knn_k), size - 1))
    for row_idx in range(size):
        order = np.argsort(dist_matrix[row_idx]).tolist()
        neighbors = [col for col in order if col != row_idx][:effective_k]
        for col_idx in neighbors:
            mask[row_idx, col_idx] = True

    return np.logical_or(mask, mask.T)


def _build_cluster_cannot_link_mask(clusters: list[dict[str, Any]]) -> np.ndarray:
    size = len(clusters)
    mask = np.zeros((size, size), dtype=bool)
    if size <= 1:
        return mask

    photo_sets: list[set[str]] = []
    for cluster in clusters:
        photos: set[str] = set()
        for member in cluster.get("members", []):
            photo_relpath = str(member.get("photo_relpath", ""))
            if photo_relpath:
                photos.add(photo_relpath)
        photo_sets.append(photos)

    for row_idx in range(size):
        row_photos = photo_sets[row_idx]
        if not row_photos:
            continue
        for col_idx in range(row_idx + 1, size):
            if row_photos.intersection(photo_sets[col_idx]):
                mask[row_idx, col_idx] = True
                mask[col_idx, row_idx] = True

    return mask


def _cluster_by_ahc_distance_matrix(
    dist_matrix: np.ndarray,
    distance_threshold: float,
    linkage: str,
) -> list[int]:
    if dist_matrix.shape[0] <= 1:
        return [0]

    from sklearn.cluster import AgglomerativeClustering

    if linkage not in {"average", "single", "complete"}:
        raise ValueError(f"不支持的人物合并 linkage: {linkage}")

    # sklearn 在不同版本里参数名从 affinity 演进到 metric，这里做兼容。
    try:
        model = AgglomerativeClustering(
            n_clusters=None,
            metric="precomputed",
            linkage=linkage,
            distance_threshold=float(distance_threshold),
        )
    except TypeError:
        model = AgglomerativeClustering(
            n_clusters=None,
            affinity="precomputed",
            linkage=linkage,
            distance_threshold=float(distance_threshold),
        )
    return model.fit_predict(dist_matrix).astype(int).tolist()


def _build_person_uuid(cluster_refs: list[dict[str, Any]]) -> str:
    face_ids: list[str] = []
    for cluster in cluster_refs:
        for member in cluster.get("members", []):
            face_id = str(member.get("face_id", ""))
            if face_id:
                face_ids.append(face_id)

    if face_ids:
        seed = "|".join(sorted(face_ids))
    else:
        cluster_labels = [str(int(cluster.get("cluster_label", -1))) for cluster in cluster_refs]
        seed = "|".join(sorted(cluster_labels))
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
    return f"person_{digest}"


def _materialize_persons_from_cluster_buckets(buckets: dict[int, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    merged_persons: list[dict[str, Any]] = []
    for source_label, person_clusters in buckets.items():
        sorted_clusters = sorted(
            person_clusters,
            key=lambda row: (-len(row.get("members", [])), int(row.get("cluster_label", 0))),
        )
        cluster_refs: list[dict[str, Any]] = []
        person_face_count = 0
        for cluster in sorted_clusters:
            cluster_members = sorted(
                list(cluster.get("members", [])),
                key=lambda row: -(row.get("quality_score") or 0.0),
            )
            person_face_count += len(cluster_members)
            cluster_refs.append(
                {
                    "cluster_key": str(cluster.get("cluster_key", "")),
                    "cluster_label": int(cluster.get("cluster_label", -1)),
                    "member_count": len(cluster_members),
                    "members": cluster_members,
                }
            )

        if not cluster_refs:
            continue

        merged_persons.append(
            {
                "source_person_label": int(source_label),
                "clusters": cluster_refs,
                "person_face_count": person_face_count,
                "person_cluster_count": len(sorted_clusters),
                "min_cluster_label": min(item["cluster_label"] for item in cluster_refs),
            }
        )

    merged_persons.sort(
        key=lambda row: (
            -int(row.get("person_face_count", 0)),
            -int(row.get("person_cluster_count", 0)),
            int(row.get("min_cluster_label", 0)),
        )
    )

    for idx, person in enumerate(merged_persons):
        person["person_label"] = idx
        person["person_key"] = f"person_{idx}"
        person["person_uuid"] = _build_person_uuid(list(person.get("clusters", [])))
        person.pop("min_cluster_label", None)
        person.pop("source_person_label", None)

    return merged_persons


def merge_clusters_to_persons(
    clusters: list[dict[str, Any]],
    distance_threshold: float,
    rep_top_k: int,
    knn_k: int,
    linkage: str = "average",
    enable_same_photo_cannot_link: bool = False,
) -> list[dict[str, Any]]:
    normal_clusters = [cluster for cluster in clusters if int(cluster.get("cluster_label", -1)) != -1]
    if not normal_clusters:
        return []

    indexed_clusters: list[dict[str, Any]] = []
    representatives: list[np.ndarray] = []
    fallback_clusters: list[dict[str, Any]] = []
    for cluster in normal_clusters:
        representative = _build_cluster_representative(cluster, rep_top_k=rep_top_k)
        if representative is None:
            fallback_clusters.append(cluster)
            continue
        indexed_clusters.append(cluster)
        representatives.append(representative)

    if indexed_clusters:
        if len(indexed_clusters) == 1:
            ahc_labels = [0]
        else:
            vector_matrix = np.stack(representatives, axis=0)
            dist_matrix = _pairwise_cosine_distance(vector_matrix)
            mask = _build_cluster_knn_mask(dist_matrix, knn_k=knn_k)
            large_distance = max(float(distance_threshold) + 1.0, 2.0)
            constrained_dist = dist_matrix.copy()
            constrained_dist[~mask] = large_distance
            if enable_same_photo_cannot_link:
                cannot_link_mask = _build_cluster_cannot_link_mask(indexed_clusters)
                constrained_dist[cannot_link_mask] = large_distance
            np.fill_diagonal(constrained_dist, 0.0)
            ahc_labels = _cluster_by_ahc_distance_matrix(
                constrained_dist,
                distance_threshold=distance_threshold,
                linkage=linkage,
            )
    else:
        ahc_labels = []

    buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for cluster, label in zip(indexed_clusters, ahc_labels, strict=True):
        buckets[int(label)].append(cluster)

    next_label = (max(buckets.keys()) + 1) if buckets else 0
    for cluster in fallback_clusters:
        buckets[next_label].append(cluster)
        next_label += 1

    return _materialize_persons_from_cluster_buckets(buckets)


def _collect_normalized_member_embeddings(cluster: dict[str, Any]) -> np.ndarray | None:
    vectors: list[np.ndarray] = []
    dim: int | None = None
    for member in cluster.get("members", []):
        raw_embedding = member.get("embedding")
        if not isinstance(raw_embedding, list) or not raw_embedding:
            continue
        vector = np.asarray(raw_embedding, dtype=np.float32)
        if vector.ndim != 1:
            continue
        if dim is None:
            dim = int(vector.shape[0])
        if int(vector.shape[0]) != dim:
            continue
        vectors.append(_normalize_embedding(vector))
    if not vectors:
        return None
    return np.stack(vectors, axis=0)


def _person_face_vector_matrix(person: dict[str, Any]) -> np.ndarray | None:
    vectors: list[np.ndarray] = []
    dim: int | None = None
    for cluster in person.get("clusters", []):
        matrix = _collect_normalized_member_embeddings(cluster)
        if matrix is None or matrix.size <= 0:
            continue
        if dim is None:
            dim = int(matrix.shape[1])
        if int(matrix.shape[1]) != dim:
            continue
        vectors.append(matrix)
    if not vectors:
        return None
    return np.concatenate(vectors, axis=0)


def attach_micro_clusters_to_existing_persons(
    persons: list[dict[str, Any]],
    source_max_cluster_size: int,
    source_max_person_face_count: int,
    target_min_person_face_count: int,
    knn_top_n: int,
    min_votes: int,
    distance_threshold: float,
    margin_threshold: float,
    max_rounds: int = 2,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    if not persons:
        return [], [], 0

    if (
        int(source_max_cluster_size) <= 0
        or int(source_max_person_face_count) <= 0
        or int(target_min_person_face_count) <= 0
        or int(knn_top_n) <= 0
        or int(min_votes) <= 0
        or float(distance_threshold) <= 0
        or int(max_rounds) <= 0
    ):
        return list(persons), [], 0

    clusters_by_label: dict[int, dict[str, Any]] = {}
    owner_by_cluster: dict[int, int] = {}
    for person in persons:
        person_label = int(person.get("person_label", -1))
        for cluster in person.get("clusters", []):
            cluster_label = int(cluster.get("cluster_label", -1))
            if cluster_label == -1:
                continue
            clusters_by_label[cluster_label] = cluster
            owner_by_cluster[cluster_label] = person_label

    if not clusters_by_label:
        return list(persons), [], 0

    moved_count = 0
    events: list[dict[str, Any]] = []

    for round_idx in range(1, int(max_rounds) + 1):
        buckets_by_owner: dict[int, list[int]] = defaultdict(list)
        for cluster_label, owner in owner_by_cluster.items():
            buckets_by_owner[int(owner)].append(cluster_label)

        person_face_count: dict[int, int] = {}
        for owner, cluster_labels in buckets_by_owner.items():
            person_face_count[int(owner)] = sum(len(clusters_by_label[label].get("members", [])) for label in cluster_labels)

        target_person_labels = [
            owner for owner, count in person_face_count.items() if int(count) >= int(target_min_person_face_count)
        ]
        if not target_person_labels:
            break

        target_vectors: dict[int, np.ndarray] = {}
        for owner in target_person_labels:
            person_payload = {
                "clusters": [clusters_by_label[label] for label in buckets_by_owner.get(owner, [])],
            }
            matrix = _person_face_vector_matrix(person_payload)
            if matrix is not None and matrix.size > 0:
                target_vectors[int(owner)] = matrix
        if not target_vectors:
            break

        candidate_cluster_labels: list[int] = []
        for cluster_label, owner in owner_by_cluster.items():
            cluster_size = len(clusters_by_label[cluster_label].get("members", []))
            owner_face_count = int(person_face_count.get(owner, 0))

            if owner_face_count >= int(target_min_person_face_count) and owner_face_count > int(source_max_person_face_count):
                continue
            if cluster_size > int(source_max_cluster_size) and owner_face_count > int(source_max_person_face_count):
                continue
            candidate_cluster_labels.append(cluster_label)

        candidate_cluster_labels.sort(
            key=lambda label: (
                -max(float(member.get("quality_score") or 0.0) for member in clusters_by_label[label].get("members", [{}])),
                label,
            )
        )

        moved_in_round = 0
        for cluster_label in candidate_cluster_labels:
            current_owner = int(owner_by_cluster.get(cluster_label, -1))
            if current_owner == -1:
                continue

            candidate_targets = [owner for owner in target_person_labels if owner != current_owner and owner in target_vectors]
            if not candidate_targets:
                continue

            cluster_vectors = _collect_normalized_member_embeddings(clusters_by_label[cluster_label])
            if cluster_vectors is None or cluster_vectors.size <= 0:
                continue

            person_scores: list[tuple[int, float, float, int]] = []
            global_pairs: list[tuple[float, int]] = []
            for target_owner in candidate_targets:
                matrix = target_vectors[target_owner]
                sims = np.clip(cluster_vectors @ matrix.T, -1.0, 1.0)
                dist = np.maximum(1.0 - sims, 0.0)
                flat = np.sort(dist.reshape(-1))
                if flat.size <= 0:
                    continue

                score_k = max(1, min(int(min_votes), int(flat.size)))
                score_mean = float(np.mean(flat[:score_k]))
                best_dist = float(flat[0])
                person_scores.append((int(target_owner), score_mean, best_dist, score_k))

                pair_k = max(1, min(int(knn_top_n), int(flat.size)))
                for value in flat[:pair_k]:
                    global_pairs.append((float(value), int(target_owner)))

            if not person_scores:
                continue
            person_scores.sort(key=lambda item: item[1])
            best_owner, best_score, best_dist, best_score_k = person_scores[0]
            second_score = float(person_scores[1][1]) if len(person_scores) >= 2 else float("inf")
            margin = second_score - best_score if second_score != float("inf") else float("inf")

            global_pairs.sort(key=lambda item: item[0])
            top_global_pairs = global_pairs[: max(1, int(knn_top_n))]
            votes = Counter(label for _, label in top_global_pairs)
            best_votes = int(votes.get(best_owner, 0))

            if best_votes < int(min_votes):
                continue
            if best_score > float(distance_threshold):
                continue
            if margin < float(margin_threshold):
                continue

            owner_by_cluster[cluster_label] = int(best_owner)
            moved_count += 1
            moved_in_round += 1
            events.append(
                {
                    "round": int(round_idx),
                    "cluster_label": int(cluster_label),
                    "cluster_size": len(clusters_by_label[cluster_label].get("members", [])),
                    "from_person_label_before_reindex": int(current_owner),
                    "to_person_label_before_reindex": int(best_owner),
                    "best_topk_mean_distance": float(best_score),
                    "best_top1_distance": float(best_dist),
                    "best_score_k": int(best_score_k),
                    "second_best_topk_mean_distance": None if second_score == float("inf") else float(second_score),
                    "margin": None if margin == float("inf") else float(margin),
                    "best_votes": int(best_votes),
                    "knn_top_n": int(knn_top_n),
                    "min_votes": int(min_votes),
                }
            )

        if moved_in_round <= 0:
            break

    rebuilt_buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for cluster_label, owner in owner_by_cluster.items():
        rebuilt_buckets[int(owner)].append(clusters_by_label[cluster_label])
    return _materialize_persons_from_cluster_buckets(rebuilt_buckets), events, moved_count


def _render_face_cards(members: list[dict[str, Any]]) -> str:
    cards: list[str] = []
    for member in members:
        face_id = html.escape(str(member.get("face_id", "")))
        crop_relpath = html.escape(str(member.get("crop_relpath", "")))
        context_relpath = html.escape(str(member.get("context_relpath", "")))
        quality_score = float(member.get("quality_score", 0.0))
        magface_quality = float(member.get("magface_quality", 0.0))
        prob = member.get("cluster_probability")
        prob_text = "-" if prob is None else f"{float(prob):.3f}"

        cards.append(
            f"""
            <article class=\"face-card\">
              <header>
                <strong>{face_id}</strong>
                <span>Q={quality_score:.3f} · M={magface_quality:.2f} · P={prob_text}</span>
              </header>
              <div class=\"thumb-grid\">
                <a href=\"{crop_relpath}\" target=\"_blank\"><img src=\"{crop_relpath}\" alt=\"crop {face_id}\"></a>
                <a href=\"{context_relpath}\" target=\"_blank\"><img src=\"{context_relpath}\" alt=\"context {face_id}\"></a>
              </div>
            </article>
            """
        )
    return "".join(cards)


def render_review_html(payload: dict[str, Any]) -> str:
    meta = payload.get("meta", {})
    persons = sorted(
        payload.get("persons", []),
        key=lambda person: int(person.get("person_face_count", 0)),
        reverse=True,
    )
    clusters = sorted(
        payload.get("clusters", []),
        key=lambda cluster: len(cluster.get("members", [])),
        reverse=True,
    )

    person_blocks: list[str] = []
    for person in persons:
        person_key = html.escape(str(person.get("person_key", "")))
        cluster_refs = list(person.get("clusters", []))
        person_cluster_count = int(person.get("person_cluster_count", len(cluster_refs)))
        person_face_count = int(
            person.get(
                "person_face_count",
                sum(int(cluster.get("member_count", len(cluster.get("members", [])))) for cluster in cluster_refs),
            )
        )
        cluster_chips = "".join(
            [
                (
                    f"<span class=\"cluster-chip\">"
                    f"{html.escape(str(cluster.get('cluster_key', '')))}"
                    f" · {int(cluster.get('member_count', 0))}"
                    "</span>"
                )
                for cluster in cluster_refs
            ]
        )

        nested_cluster_blocks: list[str] = []
        for cluster in cluster_refs:
            cluster_key = html.escape(str(cluster.get("cluster_key", "")))
            cluster_label = cluster.get("cluster_label")
            cluster_members = list(cluster.get("members", []))
            cluster_size = int(cluster.get("member_count", len(cluster_members)))
            nested_cluster_blocks.append(
                f"""
                <details class=\"person-cluster panel-subitem\" open>
                  <summary class=\"subitem-title\">
                    <h4>{cluster_key}</h4>
                    <span class=\"item-meta\">label={cluster_label} · members={cluster_size}</span>
                  </summary>
                  <div class=\"subitem-body\">
                    <div class=\"face-grid\">
                      {_render_face_cards(cluster_members)}
                    </div>
                  </div>
                </details>
                """
            )

        person_blocks.append(
            f"""
            <details class=\"person panel-item\" data-person-key=\"{person_key}\">
              <summary class=\"item-title\">
                <h3>{person_key}</h3>
                <span class=\"item-meta\">clusters={person_cluster_count} · members={person_face_count}</span>
              </summary>
              <div class=\"item-body\">
                <div class=\"person-actions\">
                  <button type=\"button\" class=\"person-cluster-toggle\" data-person-cluster-toggle>展开全部 cluster</button>
                </div>
                <div class=\"cluster-chip-list\">{cluster_chips}</div>
                <div class=\"subitem-list\">
                  {''.join(nested_cluster_blocks)}
                </div>
              </div>
            </details>
            """
        )

    cluster_blocks: list[str] = []
    for cluster in clusters:
        members = list(cluster.get("members", []))
        cluster_key = html.escape(str(cluster.get("cluster_key", "")))
        cluster_label = cluster.get("cluster_label")
        cluster_size = len(members)
        cluster_blocks.append(
            f"""
            <details class=\"cluster panel-item\">
              <summary class=\"item-title\">
                <h3>{cluster_key}</h3>
                <span class=\"item-meta\">label={cluster_label} · members={cluster_size}</span>
              </summary>
              <div class=\"item-body\">
                <div class=\"face-grid\">
                  {_render_face_cards(members)}
                </div>
              </div>
            </details>
            """
        )

    model = html.escape(str(meta.get("model", "MagFace")))
    clusterer = html.escape(str(meta.get("clusterer", "HDBSCAN")))
    person_clusterer = html.escape(str(meta.get("person_clusterer", "AHC")))
    person_linkage = html.escape(str(meta.get("person_linkage", "average")))
    person_same_photo_cannot_link = bool(meta.get("person_enable_same_photo_cannot_link", False))
    source = html.escape(str(meta.get("source", "")))
    image_count = int(meta.get("image_count", 0))
    face_count = int(meta.get("face_count", 0))
    cluster_count = int(meta.get("cluster_count", 0))
    noise_count = int(meta.get("noise_count", 0))
    person_count = int(meta.get("person_count", len(persons)))

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
    details.panel-block {{
      padding: 0;
      overflow: hidden;
    }}
    .stage-title {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 12px;
      margin: 0;
      padding: 12px 14px;
      cursor: pointer;
      user-select: none;
    }}
    .stage-title h2 {{ margin: 0; font-size: 18px; }}
    .stage-meta {{ color: var(--sub); font-size: 13px; }}
    .item-title {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 12px;
      margin: 0;
      padding: 12px 14px;
      cursor: pointer;
      user-select: none;
    }}
    .item-title h3 {{ margin: 0; font-size: 16px; }}
    .item-meta {{ color: var(--sub); font-size: 13px; }}
    details > summary::-webkit-details-marker {{ display: none; }}
    details > summary::after {{
      content: "展开";
      margin-left: 10px;
      color: var(--brand);
      font-size: 12px;
      font-weight: 600;
    }}
    details[open] > summary {{
      border-bottom: 1px dashed var(--line);
      margin-bottom: 12px;
    }}
    details[open] > summary::after {{ content: "收起"; }}
    .stage-body {{
      padding: 0 10px 10px;
    }}
    .panel-item {{
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #fff;
      margin: 0 0 10px;
      box-shadow: 0 3px 10px rgba(35, 60, 130, 0.04);
    }}
    .item-body {{
      padding: 0 14px 14px;
    }}
    .subitem-list {{
      display: grid;
      gap: 10px;
    }}
    .panel-subitem {{
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #fbfdff;
      overflow: hidden;
    }}
    .subitem-title {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 10px;
      margin: 0;
      padding: 10px 12px;
      cursor: pointer;
      user-select: none;
    }}
    .subitem-title h4 {{
      margin: 0;
      font-size: 14px;
      font-weight: 700;
    }}
    .subitem-body {{
      padding: 0 12px 12px;
    }}
    .person-actions {{
      display: flex;
      justify-content: flex-end;
      margin-bottom: 8px;
    }}
    .stage-actions {{
      display: flex;
      justify-content: flex-end;
      margin-bottom: 10px;
    }}
    .person-toggle-all,
    .person-cluster-toggle {{
      border: 1px solid #c7d7fb;
      background: #eef4ff;
      color: #21448f;
      border-radius: 8px;
      padding: 6px 10px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
    }}
    .person-toggle-all:hover,
    .person-cluster-toggle:hover {{
      background: #e3edff;
    }}
    .cluster-chip-list {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-bottom: 10px;
    }}
    .cluster-chip {{
      font-size: 12px;
      color: #2f3f57;
      background: #edf3ff;
      border: 1px solid #cfddfb;
      border-radius: 999px;
      padding: 4px 8px;
    }}
    .face-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(330px, 1fr));
      gap: 10px;
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
      grid-template-columns: repeat(2, minmax(0, 1fr));
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
        <div><dt>人物合并</dt><dd>{person_clusterer} · {person_linkage}</dd></div>
        <div><dt>同图冲突约束</dt><dd>{"开启" if person_same_photo_cannot_link else "关闭"}</dd></div>
        <div><dt>图片数</dt><dd>{image_count}</dd></div>
        <div><dt>人脸数</dt><dd>{face_count}</dd></div>
        <div><dt>簇数</dt><dd>{cluster_count}</dd></div>
        <div><dt>人物数</dt><dd>{person_count}</dd></div>
        <div><dt>噪声数</dt><dd>{noise_count}</dd></div>
      </dl>
    </section>
    <details class=\"panel panel-block\" open>
      <summary class=\"stage-title\">
        <h2>第二阶段 人物聚合（{person_clusterer}）</h2>
        <span class=\"stage-meta\">persons={person_count}</span>
      </summary>
      <div class=\"stage-body\">
        <div class=\"stage-actions\">
          <button type=\"button\" class=\"person-toggle-all\" data-person-toggle-all>展开全部 person</button>
        </div>
        {''.join(person_blocks)}
      </div>
    </details>
    <details class=\"panel panel-block\">
      <summary class=\"stage-title\">
        <h2>第一阶段 微簇（{clusterer}）</h2>
        <span class=\"stage-meta\">clusters={cluster_count} · noise={noise_count}</span>
      </summary>
      <div class=\"stage-body\">
        {''.join(cluster_blocks)}
      </div>
    </details>
  </main>
  <script>
    (() => {{
      const updatePersonClusterButtonText = (button, clusterDetailsList) => {{
        if (clusterDetailsList.length === 0) {{
          button.disabled = true;
          button.textContent = "无可展开 cluster";
          return;
        }}
        const allOpen = clusterDetailsList.every((node) => node.open);
        button.textContent = allOpen ? "折叠全部 cluster" : "展开全部 cluster";
      }};

      const personDetailsList = Array.from(document.querySelectorAll("details.person"));
      const personToggleAllButton = document.querySelector("[data-person-toggle-all]");
      const updatePersonToggleAllButtonText = () => {{
        if (!personToggleAllButton) {{
          return;
        }}
        if (personDetailsList.length === 0) {{
          personToggleAllButton.disabled = true;
          personToggleAllButton.textContent = "无可展开 person";
          return;
        }}
        const allOpen = personDetailsList.every((node) => node.open);
        personToggleAllButton.textContent = allOpen ? "折叠全部 person" : "展开全部 person";
      }};

      if (personToggleAllButton) {{
        personToggleAllButton.addEventListener("click", () => {{
          const allOpen = personDetailsList.every((node) => node.open);
          personDetailsList.forEach((node) => {{
            node.open = !allOpen;
          }});
          updatePersonToggleAllButtonText();
        }});
      }}

      personDetailsList.forEach((personDetails) => {{
        personDetails.addEventListener("toggle", updatePersonToggleAllButtonText);
      }});
      updatePersonToggleAllButtonText();

      document.querySelectorAll("[data-person-cluster-toggle]").forEach((button) => {{
        const personDetails = button.closest("details.person");
        if (!personDetails) {{
          return;
        }}
        const clusterDetailsList = Array.from(personDetails.querySelectorAll("details.person-cluster"));
        updatePersonClusterButtonText(button, clusterDetailsList);

        personDetails.addEventListener("toggle", () => {{
          if (!personDetails.open) {{
            return;
          }}
          clusterDetailsList.forEach((node) => {{
            node.open = true;
          }});
          updatePersonClusterButtonText(button, clusterDetailsList);
        }});

        button.addEventListener("click", () => {{
          const allOpen = clusterDetailsList.every((node) => node.open);
          clusterDetailsList.forEach((node) => {{
            node.open = !allOpen;
          }});
          updatePersonClusterButtonText(button, clusterDetailsList);
        }});

        clusterDetailsList.forEach((node) => {{
          node.addEventListener("toggle", () => {{
            updatePersonClusterButtonText(button, clusterDetailsList);
          }});
        }});
      }});
    }})();
  </script>
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


def _make_context(image: Image.Image, bbox: tuple[int, int, int, int], max_side: int) -> Image.Image:
    width, height = image.size
    scale = min(1.0, float(max_side) / float(max(width, height)))
    if scale >= 1.0:
        canvas = image.copy()
    else:
        canvas = image.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.Resampling.LANCZOS)

    px1 = int(bbox[0] * scale)
    py1 = int(bbox[1] * scale)
    px2 = int(bbox[2] * scale)
    py2 = int(bbox[3] * scale)

    draw = ImageDraw.Draw(canvas)
    draw.rectangle((px1, py1, px2, py2), outline="#ff3b30", width=3)
    return canvas


def _init_detection_model(insightface_root: Path, detector_model_name: str, det_size: int) -> FaceAnalysis:
    detector = FaceAnalysis(name=detector_model_name, root=str(insightface_root), allowed_modules=["detection"])
    detector.prepare(ctx_id=-1, det_size=(det_size, det_size))
    return detector


def _cluster_with_hdbscan(
    embeddings: list[list[float]],
    min_cluster_size: int,
    min_samples: int,
) -> tuple[list[int], list[float]]:
    import hdbscan

    if not embeddings:
        return [], []
    if len(embeddings) < max(2, min_cluster_size, min_samples):
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
    (output_dir / "assets" / "aligned").mkdir(parents=True, exist_ok=True)
    (output_dir / "cache").mkdir(parents=True, exist_ok=True)

    return {
        "crop": output_dir / "assets" / "crops",
        "context": output_dir / "assets" / "context",
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
    detect_restart_interval: int = 300,
    detect_skip_existing: bool = True,
    detect_max_images_per_run: int | None = None,
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
    safe_restart_interval = max(1, int(detect_restart_interval))
    safe_max_images_per_run = (
        max(1, int(detect_max_images_per_run))
        if detect_max_images_per_run is not None and int(detect_max_images_per_run) > 0
        else None
    )
    effective_restart_interval = safe_restart_interval
    if safe_max_images_per_run is not None:
        # 分批模式会通过子进程重启来回收内存，避免在同一进程内反复重建 detector。
        effective_restart_interval = max(effective_restart_interval, safe_max_images_per_run + 1)
    detector: FaceAnalysis | None = None
    last_restart_idx = 0

    processed_relpaths: set[str] = set()
    if detect_skip_existing:
        processed_relpaths.update(
            str(row["photo_relpath"])
            for row in conn.execute("SELECT photo_relpath FROM processed_images").fetchall()
        )
        # 兼容旧输出目录：尚未有 processed_images 表时，仍可利用已有检测结果做跳过。
        processed_relpaths.update(
            str(row["photo_relpath"])
            for row in conn.execute("SELECT DISTINCT photo_relpath FROM detected_faces").fetchall()
        )
        processed_relpaths.update(str(row["photo_relpath"]) for row in conn.execute("SELECT photo_relpath FROM failed_images").fetchall())

    skipped_count = 0
    processed_count = 0

    total = len(image_paths)
    for idx, image_path in enumerate(image_paths, start=1):
        relpath = image_path.relative_to(source_dir).as_posix()
        if detect_skip_existing and relpath in processed_relpaths:
            skipped_count += 1
            print(f"[det {idx}/{total}] 跳过已处理 {relpath}")
            continue

        if detector is None or (idx - last_restart_idx) >= effective_restart_interval:
            if detector is not None:
                del detector
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            detector = _init_detection_model(
                insightface_root=insightface_root,
                detector_model_name=detector_model_name,
                det_size=det_size,
            )
            last_restart_idx = idx

        print(f"[det {idx}/{total}] 处理 {relpath}")

        rgb_image: Image.Image | None = None
        rgb_arr: np.ndarray | None = None
        bgr_arr: np.ndarray | None = None
        faces = None
        crop_img: Image.Image | None = None
        context_img: Image.Image | None = None
        aligned_bgr: np.ndarray | None = None
        try:
            rgb_image = _load_rgb_image(image_path)
            rgb_arr = np.asarray(rgb_image)
            bgr_arr = cv2.cvtColor(rgb_arr, cv2.COLOR_RGB2BGR)
            height, width = bgr_arr.shape[:2]

            photo_key = hashlib.sha1(relpath.encode("utf-8")).hexdigest()[:16]

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
                crop_img = None

                context_img = _make_context(image=rgb_image, bbox=bbox, max_side=preview_max_side)
                context_img.save(dirs["context"] / context_name, format="JPEG", quality=88)
                context_img = None

                aligned_bgr = face_align.norm_crop(bgr_arr, face.kps, image_size=112)
                cv2.imwrite(str(dirs["aligned"] / aligned_name), aligned_bgr)
                aligned_bgr = None

                upsert_detected_face(
                    conn,
                    {
                        "face_id": face_id,
                        "photo_relpath": relpath,
                        "crop_relpath": f"assets/crops/{crop_name}",
                        "context_relpath": f"assets/context/{context_name}",
                        "preview_relpath": "",
                        "aligned_relpath": aligned_relpath,
                        "bbox": [x1, y1, x2, y2],
                        "detector_confidence": det_conf,
                        "face_area_ratio": area_ratio,
                    },
                )
            upsert_processed_image(conn, relpath, status="ok", face_count=len(faces))
            processed_relpaths.add(relpath)
            processed_count += 1
        except Exception as exc:  # pragma: no cover
            upsert_failed_image(conn, relpath, str(exc))
            upsert_processed_image(conn, relpath, status="error", face_count=0, error=str(exc))
            processed_relpaths.add(relpath)
            processed_count += 1
        finally:
            del rgb_image, rgb_arr, bgr_arr, faces, crop_img, context_img, aligned_bgr
            if idx % 20 == 0:
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()

        if safe_max_images_per_run is not None and processed_count >= safe_max_images_per_run:
            print(f"[det] 达到单次处理上限 {safe_max_images_per_run}，提前结束本轮 detect")
            break

    if detector is not None:
        del detector
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    summary = {
        "image_count": len(image_paths),
        "skipped_image_count": skipped_count,
        "processed_image_count": processed_count,
        "remaining_image_count": len([path for path in image_paths if path.relative_to(source_dir).as_posix() not in processed_relpaths]),
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


def _observation_to_face_row(obs: FaceObservation, include_embedding: bool = True) -> dict[str, Any]:
    row: dict[str, Any] = {
        "face_id": obs.face_id,
        "photo_relpath": obs.photo_relpath,
        "crop_relpath": obs.crop_relpath,
        "context_relpath": obs.context_relpath,
        "bbox": list(obs.bbox),
        "detector_confidence": obs.detector_confidence,
        "face_area_ratio": obs.face_area_ratio,
        "magface_quality": obs.magface_quality,
        "quality_score": obs.quality_score,
        "cluster_label": obs.cluster_label,
        "cluster_probability": obs.cluster_probability,
        "cluster_assignment_source": obs.cluster_assignment_source,
        "quality_gate_excluded": obs.quality_gate_excluded,
    }
    if include_embedding:
        row["embedding"] = obs.embedding
    return row


def run_cluster_stage(
    source_dir: Path,
    output_dir: Path,
    detector_model_name: str,
    det_size: int,
    min_cluster_size: int,
    min_samples: int,
    person_merge_threshold: float,
    person_rep_top_k: int,
    person_knn_k: int,
    person_linkage: str,
    person_enable_same_photo_cannot_link: bool,
    preview_max_side: int,
    magface_checkpoint: Path,
    person_consensus_distance_threshold: float | None = None,
    person_consensus_margin_threshold: float = 0.04,
    person_consensus_rep_top_k: int = 3,
    low_quality_micro_cluster_max_size: int = 3,
    low_quality_micro_cluster_top2_weight: float = 0.5,
    low_quality_micro_cluster_min_quality_evidence: float | None = None,
    face_min_quality_for_assignment: float | None = None,
    person_cluster_recall_distance_threshold: float | None = None,
    person_cluster_recall_margin_threshold: float = 0.04,
    person_cluster_recall_top_n: int = 5,
    person_cluster_recall_min_votes: int = 3,
    person_cluster_recall_source_max_cluster_size: int = 3,
    person_cluster_recall_source_max_person_faces: int = 8,
    person_cluster_recall_target_min_person_faces: int = 40,
    person_cluster_recall_max_rounds: int = 2,
) -> dict[str, Any]:
    dirs = _ensure_dirs(output_dir, reset_output=False)
    conn = open_pipeline_db(dirs["db"])

    failed_images = list_failed_images(conn)
    failed_faces = list_failed_faces(conn)

    observations: list[FaceObservation] = []
    for row in iter_embedded_faces(conn):
        bbox_values = [int(v) for v in row["bbox"]]
        observations.append(
            FaceObservation(
                face_id=str(row["face_id"]),
                photo_relpath=str(row["photo_relpath"]),
                crop_relpath=str(row["crop_relpath"]),
                context_relpath=str(row["context_relpath"]),
                bbox=(bbox_values[0], bbox_values[1], bbox_values[2], bbox_values[3]),
                embedding=list(row["embedding"]),
                detector_confidence=float(row["detector_confidence"]),
                face_area_ratio=float(row["face_area_ratio"]),
                magface_quality=float(row["magface_quality"]),
                quality_score=float(row["quality_score"]),
                cluster_assignment_source=row.get("cluster_assignment_source"),
            )
        )

    labels, probabilities = _cluster_with_hdbscan(
        [list(obs.embedding) for obs in observations],
        min_cluster_size=min_cluster_size,
        min_samples=min_samples,
    )

    face_quality_excluded_flags = [False for _ in observations]
    face_quality_excluded_count = 0
    labels, probabilities, face_quality_excluded_flags, face_quality_excluded_count = exclude_low_quality_faces_from_assignment(
        faces=[{"quality_score": obs.quality_score} for obs in observations],
        labels=labels,
        probabilities=probabilities,
        min_quality_score=face_min_quality_for_assignment,
    )
    for obs, excluded in zip(observations, face_quality_excluded_flags, strict=True):
        obs.quality_gate_excluded = bool(excluded)

    low_quality_micro_cluster_demoted_cluster_count = 0
    low_quality_micro_cluster_demoted_face_count = 0
    if low_quality_micro_cluster_min_quality_evidence is not None and low_quality_micro_cluster_min_quality_evidence > 0:
        quality_faces = [{"quality_score": obs.quality_score} for obs in observations]
        labels, probabilities, low_quality_micro_cluster_demoted_cluster_count, low_quality_micro_cluster_demoted_face_count = (
            demote_low_quality_micro_clusters(
                faces=quality_faces,
                labels=labels,
                probabilities=probabilities,
                max_cluster_size=int(low_quality_micro_cluster_max_size),
                top2_weight=float(low_quality_micro_cluster_top2_weight),
                min_quality_evidence=float(low_quality_micro_cluster_min_quality_evidence),
            )
        )

    person_consensus_attach_count = 0
    if person_consensus_distance_threshold is not None and person_consensus_distance_threshold > 0:
        preliminary_faces = []
        for obs, label, probability in zip(observations, labels, probabilities, strict=True):
            row = _observation_to_face_row(obs, include_embedding=True)
            row["cluster_label"] = int(label)
            row["cluster_probability"] = None if probability is None else float(probability)
            preliminary_faces.append(row)
        preliminary_clusters = group_faces_by_cluster(
            faces=preliminary_faces,
            labels=[int(label) for label in labels],
        )
        for cluster in preliminary_clusters:
            cluster["members"].sort(key=lambda row: -(row.get("quality_score") or 0.0))

        preliminary_persons = merge_clusters_to_persons(
            clusters=preliminary_clusters,
            distance_threshold=person_merge_threshold,
            rep_top_k=person_rep_top_k,
            knn_k=person_knn_k,
            linkage=person_linkage,
            enable_same_photo_cannot_link=person_enable_same_photo_cannot_link,
        )

        attach_faces = [_observation_to_face_row(obs, include_embedding=True) for obs in observations]
        labels, probabilities, person_consensus_attach_count = attach_noise_faces_to_person_consensus(
            faces=attach_faces,
            labels=labels,
            probabilities=probabilities,
            persons=preliminary_persons,
            rep_top_k=person_consensus_rep_top_k,
            distance_threshold=float(person_consensus_distance_threshold),
            margin_threshold=float(person_consensus_margin_threshold),
        )

    for obs, label, probability in zip(observations, labels, probabilities, strict=True):
        obs.cluster_label = int(label)
        obs.cluster_probability = None if probability is None else float(probability)
        if int(label) == -1:
            assignment_source = "low_quality_ignored" if obs.quality_gate_excluded else "noise"
        elif probability is None:
            assignment_source = "person_consensus"
        else:
            assignment_source = "hdbscan"
        obs.cluster_assignment_source = assignment_source
        update_cluster_result(conn, obs.face_id, int(label), probability, assignment_source)

    face_rows = [_observation_to_face_row(obs, include_embedding=True) for obs in observations]
    face_rows.sort(key=lambda row: (1 if row.get("cluster_label") == -1 else 0, -(row.get("quality_score") or 0.0)))

    grouped_clusters = group_faces_by_cluster(
        faces=face_rows,
        labels=[int(row.get("cluster_label", -1)) for row in face_rows],
    )

    for cluster in grouped_clusters:
        cluster["members"].sort(key=lambda row: -(row.get("quality_score") or 0.0))

    persons = merge_clusters_to_persons(
        clusters=grouped_clusters,
        distance_threshold=person_merge_threshold,
        rep_top_k=person_rep_top_k,
        knn_k=person_knn_k,
        linkage=person_linkage,
        enable_same_photo_cannot_link=person_enable_same_photo_cannot_link,
    )
    person_cluster_recall_events: list[dict[str, Any]] = []
    person_cluster_recall_attach_count = 0
    if person_cluster_recall_distance_threshold is not None and person_cluster_recall_distance_threshold > 0:
        persons, person_cluster_recall_events, person_cluster_recall_attach_count = attach_micro_clusters_to_existing_persons(
            persons=persons,
            source_max_cluster_size=int(person_cluster_recall_source_max_cluster_size),
            source_max_person_face_count=int(person_cluster_recall_source_max_person_faces),
            target_min_person_face_count=int(person_cluster_recall_target_min_person_faces),
            knn_top_n=int(person_cluster_recall_top_n),
            min_votes=int(person_cluster_recall_min_votes),
            distance_threshold=float(person_cluster_recall_distance_threshold),
            margin_threshold=float(person_cluster_recall_margin_threshold),
            max_rounds=int(person_cluster_recall_max_rounds),
        )
    person_cluster_recall_round_count = (
        max((int(event.get("round", 0)) for event in person_cluster_recall_events), default=0)
        if person_cluster_recall_events
        else 0
    )
    # embedding 仅用于二阶段聚合，不写入最终清单，避免 manifest 体积膨胀。
    for row in face_rows:
        row.pop("embedding", None)

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
            "person_consensus_distance_threshold": person_consensus_distance_threshold,
            "person_consensus_margin_threshold": person_consensus_margin_threshold,
            "person_consensus_rep_top_k": person_consensus_rep_top_k,
            "person_consensus_attach_count": person_consensus_attach_count,
            "low_quality_micro_cluster_max_size": low_quality_micro_cluster_max_size,
            "low_quality_micro_cluster_top2_weight": low_quality_micro_cluster_top2_weight,
            "low_quality_micro_cluster_min_quality_evidence": low_quality_micro_cluster_min_quality_evidence,
            "low_quality_micro_cluster_demoted_cluster_count": low_quality_micro_cluster_demoted_cluster_count,
            "low_quality_micro_cluster_demoted_face_count": low_quality_micro_cluster_demoted_face_count,
            "face_min_quality_for_assignment": face_min_quality_for_assignment,
            "face_quality_excluded_count": face_quality_excluded_count,
            "person_cluster_recall_distance_threshold": person_cluster_recall_distance_threshold,
            "person_cluster_recall_margin_threshold": person_cluster_recall_margin_threshold,
            "person_cluster_recall_top_n": person_cluster_recall_top_n,
            "person_cluster_recall_min_votes": person_cluster_recall_min_votes,
            "person_cluster_recall_source_max_cluster_size": person_cluster_recall_source_max_cluster_size,
            "person_cluster_recall_source_max_person_faces": person_cluster_recall_source_max_person_faces,
            "person_cluster_recall_target_min_person_faces": person_cluster_recall_target_min_person_faces,
            "person_cluster_recall_max_rounds": person_cluster_recall_max_rounds,
            "person_cluster_recall_attach_count": person_cluster_recall_attach_count,
            "person_cluster_recall_round_count": person_cluster_recall_round_count,
            "person_count": len(persons),
            "person_clusterer": "AHC",
            "person_linkage": person_linkage,
            "person_merge_threshold": person_merge_threshold,
            "person_rep_top_k": person_rep_top_k,
            "person_knn_k": person_knn_k,
            "person_enable_same_photo_cannot_link": person_enable_same_photo_cannot_link,
            "preview_max_side": preview_max_side,
            "magface_checkpoint": str(magface_checkpoint),
            "db_path": str(dirs["db"]),
        },
        "failed_images": failed_images,
        "failed_faces": failed_faces,
        "persons": persons,
        "clusters": grouped_clusters,
        "person_cluster_recall_events": person_cluster_recall_events,
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
    detect_restart_interval: int,
    detect_skip_existing: bool,
    detect_max_images_per_run: int | None,
    min_cluster_size: int,
    min_samples: int,
    person_merge_threshold: float,
    person_rep_top_k: int,
    person_knn_k: int,
    person_linkage: str,
    person_enable_same_photo_cannot_link: bool,
    preview_max_side: int,
    person_consensus_distance_threshold: float | None,
    person_consensus_margin_threshold: float,
    person_consensus_rep_top_k: int,
    low_quality_micro_cluster_max_size: int,
    low_quality_micro_cluster_top2_weight: float,
    low_quality_micro_cluster_min_quality_evidence: float | None,
    face_min_quality_for_assignment: float | None,
    person_cluster_recall_distance_threshold: float | None,
    person_cluster_recall_margin_threshold: float,
    person_cluster_recall_top_n: int,
    person_cluster_recall_min_votes: int,
    person_cluster_recall_source_max_cluster_size: int,
    person_cluster_recall_source_max_person_faces: int,
    person_cluster_recall_target_min_person_faces: int,
    person_cluster_recall_max_rounds: int,
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
            detect_restart_interval=detect_restart_interval,
            detect_skip_existing=detect_skip_existing,
            detect_max_images_per_run=detect_max_images_per_run,
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
            person_merge_threshold=person_merge_threshold,
            person_rep_top_k=person_rep_top_k,
            person_knn_k=person_knn_k,
            person_linkage=person_linkage,
            person_enable_same_photo_cannot_link=person_enable_same_photo_cannot_link,
            preview_max_side=preview_max_side,
            magface_checkpoint=magface_checkpoint,
            person_consensus_distance_threshold=person_consensus_distance_threshold,
            person_consensus_margin_threshold=person_consensus_margin_threshold,
            person_consensus_rep_top_k=person_consensus_rep_top_k,
            low_quality_micro_cluster_max_size=low_quality_micro_cluster_max_size,
            low_quality_micro_cluster_top2_weight=low_quality_micro_cluster_top2_weight,
            low_quality_micro_cluster_min_quality_evidence=low_quality_micro_cluster_min_quality_evidence,
            face_min_quality_for_assignment=face_min_quality_for_assignment,
            person_cluster_recall_distance_threshold=person_cluster_recall_distance_threshold,
            person_cluster_recall_margin_threshold=person_cluster_recall_margin_threshold,
            person_cluster_recall_top_n=person_cluster_recall_top_n,
            person_cluster_recall_min_votes=person_cluster_recall_min_votes,
            person_cluster_recall_source_max_cluster_size=person_cluster_recall_source_max_cluster_size,
            person_cluster_recall_source_max_person_faces=person_cluster_recall_source_max_person_faces,
            person_cluster_recall_target_min_person_faces=person_cluster_recall_target_min_person_faces,
            person_cluster_recall_max_rounds=person_cluster_recall_max_rounds,
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
        "--detect-restart-interval",
        str(detect_restart_interval),
        "--min-cluster-size",
        str(min_cluster_size),
        "--min-samples",
        str(min_samples),
        "--person-merge-threshold",
        str(person_merge_threshold),
        "--person-rep-top-k",
        str(person_rep_top_k),
        "--person-knn-k",
        str(person_knn_k),
        "--person-linkage",
        person_linkage,
        "--preview-max-side",
        str(preview_max_side),
    ]
    if person_consensus_distance_threshold is not None:
        base_cmd.extend(
            [
                "--person-consensus-distance-threshold",
                str(person_consensus_distance_threshold),
                "--person-consensus-margin-threshold",
                str(person_consensus_margin_threshold),
                "--person-consensus-rep-top-k",
                str(person_consensus_rep_top_k),
            ]
        )
    if low_quality_micro_cluster_min_quality_evidence is not None:
        base_cmd.extend(
            [
                "--low-quality-micro-cluster-max-size",
                str(low_quality_micro_cluster_max_size),
                "--low-quality-micro-cluster-top2-weight",
                str(low_quality_micro_cluster_top2_weight),
                "--low-quality-micro-cluster-min-quality-evidence",
                str(low_quality_micro_cluster_min_quality_evidence),
            ]
        )
    if face_min_quality_for_assignment is not None:
        base_cmd.extend(
            [
                "--face-min-quality-for-assignment",
                str(face_min_quality_for_assignment),
            ]
        )
    if person_cluster_recall_distance_threshold is not None:
        base_cmd.extend(
            [
                "--person-cluster-recall-distance-threshold",
                str(person_cluster_recall_distance_threshold),
                "--person-cluster-recall-margin-threshold",
                str(person_cluster_recall_margin_threshold),
                "--person-cluster-recall-top-n",
                str(person_cluster_recall_top_n),
                "--person-cluster-recall-min-votes",
                str(person_cluster_recall_min_votes),
                "--person-cluster-recall-source-max-cluster-size",
                str(person_cluster_recall_source_max_cluster_size),
                "--person-cluster-recall-source-max-person-faces",
                str(person_cluster_recall_source_max_person_faces),
                "--person-cluster-recall-target-min-person-faces",
                str(person_cluster_recall_target_min_person_faces),
                "--person-cluster-recall-max-rounds",
                str(person_cluster_recall_max_rounds),
            ]
        )
    if person_enable_same_photo_cannot_link:
        base_cmd.append("--person-enable-same-photo-cannot-link")
    if max_images is not None:
        base_cmd.extend(["--max-images", str(max_images)])
    if not detect_skip_existing:
        base_cmd.append("--detect-no-skip-existing")
    detect_cmd = base_cmd + ["--stage", "detect"]
    if reset_output:
        detect_cmd.append("--reset-output")

    total_image_count = len(iter_image_files(source_dir))
    expected_total, _, detect_batch_size = compute_detect_workset_stats(
        total_images=total_image_count,
        max_images=max_images,
        processed_count=0,
        detect_max_images_per_run=detect_max_images_per_run,
    )
    if detect_max_images_per_run is None:
        print(f"all 模式：未指定 detect 单次处理上限，使用默认分批大小 {detect_batch_size}")

    detect_cmd.extend(["--detect-max-images-per-run", str(int(detect_batch_size))])
    while True:
        subprocess.run(detect_cmd, check=True)
        db_conn = open_pipeline_db(output_dir / "cache" / "pipeline.db")
        processed_count = int(
            db_conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM (
                    SELECT photo_relpath FROM processed_images
                    UNION
                    SELECT DISTINCT photo_relpath FROM detected_faces
                    UNION
                    SELECT photo_relpath FROM failed_images
                ) AS merged
                """
            ).fetchone()["c"]
        )
        db_conn.close()

        expected_total, remaining, _ = compute_detect_workset_stats(
            total_images=total_image_count,
            max_images=max_images,
            processed_count=processed_count,
            detect_max_images_per_run=detect_batch_size,
        )
        print(f"detect 分批进度：processed={processed_count} / total={expected_total} / remaining={remaining}")
        if remaining <= 0:
            break
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
    parser.add_argument(
        "--detect-restart-interval",
        type=int,
        default=300,
        help="detect 阶段每处理 N 张图后重启一次 detector，抑制长跑内存增长",
    )
    parser.add_argument(
        "--detect-no-skip-existing",
        action="store_true",
        help="detect 阶段不跳过已有检测结果（默认会跳过 detected_faces/failed_images 中已处理图片）",
    )
    parser.add_argument(
        "--detect-max-images-per-run",
        type=int,
        default=None,
        help=(
            "detect 阶段单进程最多处理的图片数；"
            "stage=all 时不传会使用默认分批大小，stage=detect 时不传则单次跑完"
        ),
    )
    parser.add_argument("--min-cluster-size", type=int, default=3, help="HDBSCAN min_cluster_size")
    parser.add_argument("--min-samples", type=int, default=2, help="HDBSCAN min_samples")
    parser.add_argument("--person-merge-threshold", type=float, default=0.24, help="二阶段 AHC 合并阈值（余弦距离）")
    parser.add_argument("--person-rep-top-k", type=int, default=3, help="每个微簇用于构建代表向量的高质量样本数")
    parser.add_argument("--person-knn-k", type=int, default=8, help="二阶段只在每个微簇的前 K 近邻上尝试合并")
    parser.add_argument(
        "--person-linkage",
        type=str,
        choices=["average", "single", "complete"],
        default="single",
        help="二阶段 AHC linkage 策略",
    )
    parser.add_argument(
        "--person-enable-same-photo-cannot-link",
        action="store_true",
        help="开启同图冲突硬约束：同一张图出现过的人脸簇禁止在二阶段合并（默认关闭）",
    )
    parser.add_argument("--preview-max-side", type=int, default=480, help="预览图最长边")
    parser.add_argument(
        "--person-consensus-distance-threshold",
        type=float,
        default=None,
        help="基于 person-consensus 的一阶段噪声回挂最大余弦距离；不传则关闭",
    )
    parser.add_argument(
        "--person-consensus-margin-threshold",
        type=float,
        default=0.04,
        help="基于 person-consensus 的一阶段噪声回挂时 top1-top2 最小相似度间隔",
    )
    parser.add_argument(
        "--person-consensus-rep-top-k",
        type=int,
        default=3,
        help="person-consensus 回挂构建微簇代表向量时使用的高质量样本数",
    )
    parser.add_argument(
        "--low-quality-micro-cluster-max-size",
        type=int,
        default=3,
        help="低质量微簇回退规则的最大簇大小（仅对 <= 该值的微簇生效）",
    )
    parser.add_argument(
        "--low-quality-micro-cluster-top2-weight",
        type=float,
        default=0.5,
        help="低质量微簇回退规则中第二高质量样本权重（证据分=top1+weight*top2）",
    )
    parser.add_argument(
        "--low-quality-micro-cluster-min-quality-evidence",
        type=float,
        default=None,
        help="低质量微簇回退阈值；不传则关闭（证据分低于阈值的微簇整体回退到 noise）",
    )
    parser.add_argument(
        "--face-min-quality-for-assignment",
        type=float,
        default=None,
        help="face 级质量硬排除阈值；低于该阈值的样本直接标记 low_quality_ignored，不参与自动归属",
    )
    parser.add_argument(
        "--person-cluster-recall-distance-threshold",
        type=float,
        default=None,
        help="非-noise 微簇归属召回阈值（top-k 平均余弦距离）；不传则关闭该通道",
    )
    parser.add_argument(
        "--person-cluster-recall-margin-threshold",
        type=float,
        default=0.04,
        help="非-noise 微簇归属召回时最佳/次佳候选间最小距离差距",
    )
    parser.add_argument(
        "--person-cluster-recall-top-n",
        type=int,
        default=5,
        help="非-noise 微簇归属召回投票时采用的全局最近邻数",
    )
    parser.add_argument(
        "--person-cluster-recall-min-votes",
        type=int,
        default=3,
        help="非-noise 微簇归属召回最少票数",
    )
    parser.add_argument(
        "--person-cluster-recall-source-max-cluster-size",
        type=int,
        default=3,
        help="非-noise 微簇归属召回中，允许移动的最大微簇大小",
    )
    parser.add_argument(
        "--person-cluster-recall-source-max-person-faces",
        type=int,
        default=8,
        help="非-noise 微簇归属召回中，允许作为来源的人物最大样本数",
    )
    parser.add_argument(
        "--person-cluster-recall-target-min-person-faces",
        type=int,
        default=40,
        help="非-noise 微簇归属召回中，允许作为目标人物的最小样本数",
    )
    parser.add_argument(
        "--person-cluster-recall-max-rounds",
        type=int,
        default=2,
        help="非-noise 微簇归属召回最大迭代轮数",
    )
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
        detect_restart_interval=args.detect_restart_interval,
        detect_skip_existing=not args.detect_no_skip_existing,
        detect_max_images_per_run=args.detect_max_images_per_run,
        min_cluster_size=args.min_cluster_size,
        min_samples=args.min_samples,
        person_merge_threshold=args.person_merge_threshold,
        person_rep_top_k=args.person_rep_top_k,
        person_knn_k=args.person_knn_k,
        person_linkage=args.person_linkage,
        person_enable_same_photo_cannot_link=args.person_enable_same_photo_cannot_link,
        preview_max_side=args.preview_max_side,
        person_consensus_distance_threshold=args.person_consensus_distance_threshold,
        person_consensus_margin_threshold=args.person_consensus_margin_threshold,
        person_consensus_rep_top_k=args.person_consensus_rep_top_k,
        low_quality_micro_cluster_max_size=args.low_quality_micro_cluster_max_size,
        low_quality_micro_cluster_top2_weight=args.low_quality_micro_cluster_top2_weight,
        low_quality_micro_cluster_min_quality_evidence=args.low_quality_micro_cluster_min_quality_evidence,
        face_min_quality_for_assignment=args.face_min_quality_for_assignment,
        person_cluster_recall_distance_threshold=args.person_cluster_recall_distance_threshold,
        person_cluster_recall_margin_threshold=args.person_cluster_recall_margin_threshold,
        person_cluster_recall_top_n=args.person_cluster_recall_top_n,
        person_cluster_recall_min_votes=args.person_cluster_recall_min_votes,
        person_cluster_recall_source_max_cluster_size=args.person_cluster_recall_source_max_cluster_size,
        person_cluster_recall_source_max_person_faces=args.person_cluster_recall_source_max_person_faces,
        person_cluster_recall_target_min_person_faces=args.person_cluster_recall_target_min_person_faces,
        person_cluster_recall_max_rounds=args.person_cluster_recall_max_rounds,
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
        f"persons={meta.get('person_count')} "
        f"clusters={meta.get('cluster_count')} "
        f"noise={meta.get('noise_count')}"
    )
    print(f"HTML: {args.output / 'review.html'}")
    print(f"JSON: {args.output / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
