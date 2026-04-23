from __future__ import annotations

import argparse
import gc
import hashlib
import html
import json
import re
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
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
EMBEDDING_STORAGE_DTYPE = "float32"
ROOT_SOURCE_KEY = "__root__"
SUPPORTED_EMBEDDING_STORAGE_DTYPES: dict[str, np.dtype[Any]] = {
    "float16": np.dtype(np.float16),
    "float32": np.dtype(np.float32),
}
ANN_BUILD_MIN_ITEMS = 2048
ANN_DEFAULT_QUERY_K = 64
ANN_CANDIDATE_PRUNE_MIN_ITEMS = 64
LARGE_PERSON_REVIEW_MIN_FACE_COUNT = 100
PERSON_REVIEW_HTML_RE = re.compile(r"^review_person_\d{4}\.html$")


@dataclass
class FaceObservation:
    face_id: str
    photo_relpath: str
    crop_relpath: str
    context_relpath: str
    bbox: tuple[int, int, int, int]
    embedding: np.ndarray
    detector_confidence: float
    face_area_ratio: float
    magface_quality: float
    quality_score: float
    embedding_flip: np.ndarray | None = None
    cluster_label: int | None = None
    cluster_probability: float | None = None
    cluster_assignment_source: str | None = None
    quality_gate_excluded: bool = False


def _normalize_embedding_storage_dtype(storage_dtype: str | None) -> str:
    if storage_dtype in SUPPORTED_EMBEDDING_STORAGE_DTYPES:
        return str(storage_dtype)
    return EMBEDDING_STORAGE_DTYPE


def _coerce_embedding_vector(raw_embedding: Any) -> np.ndarray | None:
    if raw_embedding is None:
        return None
    if isinstance(raw_embedding, np.ndarray):
        vector = raw_embedding.astype(np.float32, copy=False)
    elif isinstance(raw_embedding, (list, tuple)) and raw_embedding:
        vector = np.asarray(raw_embedding, dtype=np.float32)
    else:
        return None
    if vector.ndim != 1 or int(vector.shape[0]) <= 0:
        return None
    return vector


def _encode_embedding_blob(embedding: Any, storage_dtype: str = EMBEDDING_STORAGE_DTYPE) -> tuple[bytes, str]:
    dtype_name = _normalize_embedding_storage_dtype(storage_dtype)
    vector = _coerce_embedding_vector(embedding)
    if vector is None:
        raise ValueError("embedding 为空或格式无效")
    storage_dtype_np = SUPPORTED_EMBEDDING_STORAGE_DTYPES[dtype_name]
    return vector.astype(storage_dtype_np, copy=False).tobytes(), dtype_name


def _decode_embedding_blob(
    embedding_blob: bytes | memoryview | None,
    embedding_dtype: str | None,
    embedding_json: str | None,
    as_numpy: bool,
) -> list[float] | np.ndarray | None:
    if embedding_blob is not None:
        dtype_name = _normalize_embedding_storage_dtype(embedding_dtype)
        dtype = SUPPORTED_EMBEDDING_STORAGE_DTYPES[dtype_name]
        vector = np.frombuffer(embedding_blob, dtype=dtype).astype(np.float32, copy=False)
        if as_numpy:
            return vector
        return vector.astype(float).tolist()

    if embedding_json:
        try:
            parsed = json.loads(embedding_json)
        except json.JSONDecodeError:
            return None
        vector = _coerce_embedding_vector(parsed)
        if vector is None:
            return None
        if as_numpy:
            return vector
        return vector.astype(float).tolist()

    return None


def _flip_embedding_cache_path(output_dir: Path) -> Path:
    return output_dir / "cache" / "flip_embeddings.json"


def _load_flip_embeddings_cache(output_dir: Path) -> dict[str, list[float]]:
    path = _flip_embedding_cache_path(output_dir)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}

    result: dict[str, list[float]] = {}
    for face_id, emb in payload.items():
        if not isinstance(face_id, str):
            continue
        if not isinstance(emb, list) or not emb:
            continue
        try:
            result[face_id] = [float(v) for v in emb]
        except (TypeError, ValueError):
            continue
    return result


def _save_flip_embeddings_cache(output_dir: Path, flip_embeddings: dict[str, list[float]]) -> None:
    path = _flip_embedding_cache_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(flip_embeddings, ensure_ascii=False), encoding="utf-8")


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
            embedding_blob BLOB,
            embedding_dtype TEXT,
            magface_quality REAL,
            quality_score REAL,
            cluster_label INTEGER,
            cluster_probability REAL,
            cluster_assignment_source TEXT,
            face_error TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS pipeline_sources (
            source_key TEXT PRIMARY KEY,
            source_relpath TEXT NOT NULL,
            discover_status TEXT NOT NULL DEFAULT 'pending',
            detect_status TEXT NOT NULL DEFAULT 'pending',
            embed_status TEXT NOT NULL DEFAULT 'pending',
            cluster_status TEXT NOT NULL DEFAULT 'pending',
            discover_completed_at TEXT,
            detect_completed_at TEXT,
            embed_completed_at TEXT,
            cluster_completed_at TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS source_images (
            photo_relpath TEXT PRIMARY KEY,
            source_key TEXT NOT NULL,
            detect_status TEXT NOT NULL DEFAULT 'pending',
            detect_error TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS pipeline_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.execute("DROP INDEX IF EXISTS idx_detected_faces_pending")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_detected_faces_pending
        ON detected_faces(embedding_blob, embedding_json, face_error)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_source_images_source_detect_status
        ON source_images(source_key, detect_status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_source_images_detect_status
        ON source_images(detect_status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_pipeline_sources_discover_status
        ON pipeline_sources(discover_status)
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


def upsert_pipeline_source(conn: sqlite3.Connection, source_key: str, source_relpath: str) -> None:
    conn.execute(
        """
        INSERT INTO pipeline_sources(
            source_key, source_relpath, discover_status, detect_status, embed_status, cluster_status, updated_at
        )
        VALUES(?, ?, 'pending', 'pending', 'pending', 'pending', CURRENT_TIMESTAMP)
        ON CONFLICT(source_key) DO UPDATE SET
            source_relpath=excluded.source_relpath,
            updated_at=CURRENT_TIMESTAMP
        """,
        (source_key, source_relpath),
    )


def list_pipeline_sources(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT source_key, source_relpath, discover_status, detect_status, embed_status, cluster_status
        FROM pipeline_sources
        ORDER BY source_key
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _set_source_stage_status(
    conn: sqlite3.Connection,
    source_key: str,
    stage: str,
    status: str,
    error: str | None = None,
) -> None:
    stage = str(stage)
    if stage not in {"discover", "detect", "embed", "cluster"}:
        raise ValueError(f"未知 source stage: {stage}")
    status_col = f"{stage}_status"
    completed_col = f"{stage}_completed_at"
    if status == "done":
        conn.execute(
            f"""
            UPDATE pipeline_sources
            SET {status_col}='done',
                {completed_col}=CURRENT_TIMESTAMP,
                last_error=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE source_key=?
            """,
            (source_key,),
        )
    elif status == "running":
        conn.execute(
            f"""
            UPDATE pipeline_sources
            SET {status_col}='running',
                {completed_col}=NULL,
                last_error=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE source_key=?
            """,
            (source_key,),
        )
    elif status == "error":
        conn.execute(
            f"""
            UPDATE pipeline_sources
            SET {status_col}='error',
                {completed_col}=NULL,
                last_error=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE source_key=?
            """,
            (error or "", source_key),
        )
    else:
        conn.execute(
            f"""
            UPDATE pipeline_sources
            SET {status_col}='pending',
                {completed_col}=NULL,
                last_error=NULL,
                updated_at=CURRENT_TIMESTAMP
            WHERE source_key=?
            """,
            (source_key,),
        )


def _discover_source_roots(source_dir: Path) -> list[tuple[str, str]]:
    discovered: list[tuple[str, str]] = []
    has_root_images = False
    for item in sorted(source_dir.iterdir(), key=lambda p: p.name):
        if item.name.startswith("."):
            continue
        if item.is_dir():
            discovered.append((item.name, item.name))
            continue
        if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES:
            has_root_images = True
    if has_root_images:
        discovered.append((ROOT_SOURCE_KEY, "."))
    return discovered


def _iter_source_image_relpaths(source_dir: Path, source_relpath: str) -> list[str]:
    source_relpath = "." if source_relpath in {"", "."} else source_relpath
    if source_relpath == ".":
        results: list[str] = []
        for item in sorted(source_dir.iterdir(), key=lambda p: p.name):
            if item.name.startswith("."):
                continue
            if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES:
                results.append(item.name)
        return results

    root = (source_dir / source_relpath).resolve()
    if not root.exists() or not root.is_dir():
        return []
    results: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        if path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        results.append((Path(source_relpath) / Path(*rel_parts)).as_posix())
    return sorted(results)


def _bootstrap_sources_if_needed(conn: sqlite3.Connection, source_dir: Path) -> int:
    existing_count = int(conn.execute("SELECT COUNT(*) AS c FROM pipeline_sources").fetchone()["c"])
    if existing_count > 0:
        return 0

    discovered_sources = _discover_source_roots(source_dir)
    for source_key, source_relpath in discovered_sources:
        upsert_pipeline_source(conn, source_key=source_key, source_relpath=source_relpath)
    conn.commit()
    return len(discovered_sources)


def _refresh_discoverable_sources(conn: sqlite3.Connection, source_dir: Path) -> int:
    discovered_sources = _discover_source_roots(source_dir)
    existing_source_keys = {
        str(row["source_key"])
        for row in conn.execute("SELECT source_key FROM pipeline_sources").fetchall()
    }

    for source_key, source_relpath in discovered_sources:
        upsert_pipeline_source(conn, source_key=source_key, source_relpath=source_relpath)
        _set_source_stage_status(conn, source_key=source_key, stage="discover", status="pending")

    conn.commit()
    return len([source_key for source_key, _ in discovered_sources if source_key not in existing_source_keys])


def _register_source_images(conn: sqlite3.Connection, source_key: str, photo_relpaths: list[str]) -> int:
    if not photo_relpaths:
        return 0
    existing_relpaths = {
        str(row["photo_relpath"])
        for row in conn.execute(
            "SELECT photo_relpath FROM source_images WHERE source_key=?",
            (source_key,),
        ).fetchall()
    }
    fresh_relpaths = [path for path in photo_relpaths if path not in existing_relpaths]
    if not fresh_relpaths:
        return 0

    conn.executemany(
        """
        INSERT INTO source_images(photo_relpath, source_key, detect_status, detect_error, updated_at)
        VALUES(?, ?, 'pending', NULL, CURRENT_TIMESTAMP)
        ON CONFLICT(photo_relpath) DO NOTHING
        """,
        [(path, source_key) for path in fresh_relpaths],
    )
    conn.commit()
    return len(fresh_relpaths)


def _list_pending_source_images(conn: sqlite3.Connection) -> list[dict[str, str]]:
    rows = conn.execute(
        """
        SELECT source_key, photo_relpath
        FROM source_images
        WHERE detect_status='pending'
        ORDER BY source_key, photo_relpath
        """
    ).fetchall()
    return [{"source_key": str(row["source_key"]), "photo_relpath": str(row["photo_relpath"])} for row in rows]


def _list_pending_source_images_for_source(
    conn: sqlite3.Connection,
    source_key: str,
    limit: int | None = None,
) -> list[dict[str, str]]:
    query = """
        SELECT source_key, photo_relpath
        FROM source_images
        WHERE source_key=?
          AND detect_status='pending'
        ORDER BY photo_relpath
    """
    params: list[Any] = [source_key]
    if limit is not None:
        query += "\nLIMIT ?"
        params.append(max(1, int(limit)))
    rows = conn.execute(query, tuple(params)).fetchall()
    return [{"source_key": str(row["source_key"]), "photo_relpath": str(row["photo_relpath"])} for row in rows]


def _refresh_source_detect_stage(conn: sqlite3.Connection, source_key: str) -> None:
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN detect_status='pending' THEN 1 ELSE 0 END) AS pending_count,
            SUM(CASE WHEN detect_status='error' THEN 1 ELSE 0 END) AS error_count
        FROM source_images
        WHERE source_key=?
        """,
        (source_key,),
    ).fetchone()
    pending_count = int(row["pending_count"] or 0)
    error_count = int(row["error_count"] or 0)
    if pending_count == 0:
        if error_count > 0:
            err_row = conn.execute(
                """
                SELECT detect_error
                FROM source_images
                WHERE source_key=? AND detect_status='error'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (source_key,),
            ).fetchone()
            _set_source_stage_status(
                conn,
                source_key=source_key,
                stage="detect",
                status="error",
                error=str(err_row["detect_error"]) if err_row and err_row["detect_error"] else "detect 阶段存在失败图片",
            )
        else:
            _set_source_stage_status(conn, source_key=source_key, stage="detect", status="done")
    else:
        _set_source_stage_status(conn, source_key=source_key, stage="detect", status="pending")
    conn.commit()


def _count_source_images(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS c FROM source_images").fetchone()["c"])


def _build_source_stage_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = list_pipeline_sources(conn)
    source_count = len(rows)
    return {
        "source_count": source_count,
        "source_discover_done_count": sum(1 for row in rows if row.get("discover_status") == "done"),
        "source_detect_done_count": sum(1 for row in rows if row.get("detect_status") == "done"),
        "source_embed_done_count": sum(1 for row in rows if row.get("embed_status") == "done"),
        "source_cluster_done_count": sum(1 for row in rows if row.get("cluster_status") == "done"),
    }


def _mark_source_image_detect_result(
    conn: sqlite3.Connection,
    source_key: str,
    photo_relpath: str,
    status: str,
    error: str | None = None,
) -> None:
    if status == "error":
        conn.execute(
            """
            UPDATE source_images
            SET detect_status='error', detect_error=?, updated_at=CURRENT_TIMESTAMP
            WHERE photo_relpath=? AND source_key=?
            """,
            (error or "", photo_relpath, source_key),
        )
    else:
        conn.execute(
            """
            UPDATE source_images
            SET detect_status='done', detect_error=NULL, updated_at=CURRENT_TIMESTAMP
            WHERE photo_relpath=? AND source_key=?
            """,
            (photo_relpath, source_key),
        )
    conn.commit()


def _refresh_all_source_embed_stage(conn: sqlite3.Connection) -> None:
    sources = list_pipeline_sources(conn)
    for source in sources:
        source_key = str(source["source_key"])
        detect_status = str(source.get("detect_status", "pending"))
        if detect_status not in {"done", "error"}:
            _set_source_stage_status(conn, source_key=source_key, stage="embed", status="pending")
            continue

        row = conn.execute(
            """
            SELECT COUNT(*) AS pending_faces
            FROM detected_faces AS face
            INNER JOIN source_images AS src ON src.photo_relpath = face.photo_relpath
            WHERE src.source_key=?
              AND face.embedding_blob IS NULL
              AND face.embedding_json IS NULL
              AND face.face_error IS NULL
            """,
            (source_key,),
        ).fetchone()
        pending_faces = int(row["pending_faces"])
        if pending_faces > 0:
            _set_source_stage_status(conn, source_key=source_key, stage="embed", status="pending")
        else:
            _set_source_stage_status(conn, source_key=source_key, stage="embed", status="done")
    conn.commit()


def _refresh_all_source_cluster_stage(conn: sqlite3.Connection) -> None:
    cluster_pending_exists = bool(
        conn.execute(
            """
            SELECT 1
            FROM detected_faces
            WHERE (embedding_blob IS NOT NULL OR embedding_json IS NOT NULL)
              AND face_error IS NULL
              AND cluster_assignment_source IS NULL
            LIMIT 1
            """
        ).fetchone()
    )
    sources = list_pipeline_sources(conn)
    for source in sources:
        source_key = str(source["source_key"])
        embed_status = str(source.get("embed_status", "pending"))
        if embed_status == "done" and not cluster_pending_exists:
            _set_source_stage_status(conn, source_key=source_key, stage="cluster", status="done")
        else:
            _set_source_stage_status(conn, source_key=source_key, stage="cluster", status="pending")
    conn.commit()


def upsert_detected_face(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO detected_faces(
            face_id, photo_relpath, crop_relpath, context_relpath, preview_relpath,
            aligned_relpath, bbox_json, detector_confidence, face_area_ratio,
            embedding_json, embedding_blob, embedding_dtype, magface_quality, quality_score,
            cluster_label, cluster_probability, cluster_assignment_source, face_error, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
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
            embedding_blob=NULL,
            embedding_dtype=NULL,
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
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
    )
    conn.commit()


def iter_faces_pending_embedding(conn: sqlite3.Connection) -> Iterator[dict[str, Any]]:
    cursor = conn.execute(
        """
        SELECT face_id, photo_relpath, crop_relpath, context_relpath, preview_relpath,
               aligned_relpath, bbox_json, detector_confidence, face_area_ratio
        FROM detected_faces
        WHERE embedding_blob IS NULL AND embedding_json IS NULL AND face_error IS NULL
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
    embedding: list[float] | np.ndarray,
    magface_quality: float,
    quality_score: float,
    commit: bool = True,
) -> None:
    embedding_blob, embedding_dtype = _encode_embedding_blob(embedding, storage_dtype=EMBEDDING_STORAGE_DTYPE)
    conn.execute(
        """
        UPDATE detected_faces
        SET embedding_json=NULL, embedding_blob=?, embedding_dtype=?, magface_quality=?, quality_score=?, face_error=NULL, updated_at=CURRENT_TIMESTAMP
        WHERE face_id=?
        """,
        (
            sqlite3.Binary(embedding_blob),
            embedding_dtype,
            float(magface_quality),
            float(quality_score),
            face_id,
        ),
    )
    if commit:
        conn.commit()


def mark_faces_embedded_batch(
    conn: sqlite3.Connection,
    rows: list[tuple[str, list[float] | np.ndarray, float, float]],
) -> None:
    if not rows:
        return
    payload: list[tuple[bytes, str, float, float, str]] = []
    for face_id, embedding, magface_quality, quality_score in rows:
        embedding_blob, embedding_dtype = _encode_embedding_blob(embedding, storage_dtype=EMBEDDING_STORAGE_DTYPE)
        payload.append(
            (
                bytes(embedding_blob),
                embedding_dtype,
                float(magface_quality),
                float(quality_score),
                str(face_id),
            )
        )
    conn.executemany(
        """
        UPDATE detected_faces
        SET embedding_json=NULL, embedding_blob=?, embedding_dtype=?, magface_quality=?, quality_score=?, face_error=NULL, updated_at=CURRENT_TIMESTAMP
        WHERE face_id=?
        """,
        payload,
    )
    conn.commit()


def mark_face_error(conn: sqlite3.Connection, face_id: str, error: str, commit: bool = True) -> None:
    conn.execute(
        """
        UPDATE detected_faces
        SET face_error=?, updated_at=CURRENT_TIMESTAMP
        WHERE face_id=?
        """,
        (error, face_id),
    )
    if commit:
        conn.commit()


def mark_face_errors_batch(conn: sqlite3.Connection, rows: list[tuple[str, str]]) -> None:
    if not rows:
        return
    conn.executemany(
        """
        UPDATE detected_faces
        SET face_error=?, updated_at=CURRENT_TIMESTAMP
        WHERE face_id=?
        """,
        [(str(error), str(face_id)) for face_id, error in rows],
    )
    conn.commit()


def iter_embedded_faces(conn: sqlite3.Connection, *, as_numpy: bool = False) -> Iterator[dict[str, Any]]:
    cursor = conn.execute(
        """
        SELECT face_id, photo_relpath, crop_relpath, context_relpath, preview_relpath,
               bbox_json, detector_confidence, face_area_ratio,
               embedding_json, embedding_blob, embedding_dtype, magface_quality, quality_score,
               cluster_label, cluster_probability, cluster_assignment_source
        FROM detected_faces
        WHERE (embedding_blob IS NOT NULL OR embedding_json IS NOT NULL) AND face_error IS NULL
        ORDER BY face_id
        """
    )
    for row in cursor:
        embedding = _decode_embedding_blob(
            embedding_blob=row["embedding_blob"],
            embedding_dtype=row["embedding_dtype"],
            embedding_json=row["embedding_json"],
            as_numpy=as_numpy,
        )
        if embedding is None:
            continue
        yield {
            "face_id": row["face_id"],
            "photo_relpath": row["photo_relpath"],
            "crop_relpath": row["crop_relpath"],
            "context_relpath": row["context_relpath"],
            "preview_relpath": row["preview_relpath"],
            "bbox": json.loads(row["bbox_json"]),
            "detector_confidence": float(row["detector_confidence"]),
            "face_area_ratio": float(row["face_area_ratio"]),
            "embedding": embedding,
            "magface_quality": float(row["magface_quality"]),
            "quality_score": float(row["quality_score"]),
            "cluster_label": row["cluster_label"],
            "cluster_probability": row["cluster_probability"],
            "cluster_assignment_source": row["cluster_assignment_source"],
        }


def list_failed_images(conn: sqlite3.Connection) -> list[dict[str, str]]:
    rows = conn.execute(
        """
        SELECT photo_relpath, detect_error
        FROM source_images
        WHERE detect_status='error'
        ORDER BY photo_relpath
        """
    ).fetchall()
    return [{"photo_relpath": row["photo_relpath"], "error": row["detect_error"]} for row in rows]


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
        "SELECT COUNT(*) AS c FROM detected_faces WHERE embedding_blob IS NULL AND embedding_json IS NULL AND face_error IS NULL"
    ).fetchone()
    return int(row["c"])


def count_embedded_faces(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM detected_faces WHERE (embedding_blob IS NOT NULL OR embedding_json IS NOT NULL) AND face_error IS NULL"
    ).fetchone()
    return int(row["c"])


def count_failed_faces(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM detected_faces WHERE face_error IS NOT NULL").fetchone()
    return int(row["c"])


def update_cluster_result(
    conn: sqlite3.Connection,
    face_id: str,
    label: int,
    probability: float | None,
    assignment_source: str | None,
    commit: bool = True,
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
    if commit:
        conn.commit()


def update_cluster_results_batch(
    conn: sqlite3.Connection,
    rows: list[tuple[str, int, float | None, str | None]],
) -> None:
    if not rows:
        return
    conn.executemany(
        """
        UPDATE detected_faces
        SET cluster_label=?, cluster_probability=?, cluster_assignment_source=?, updated_at=CURRENT_TIMESTAMP
        WHERE face_id=?
        """,
        [
            (
                int(label),
                None if probability is None else float(probability),
                assignment_source,
                str(face_id),
            )
            for face_id, label, probability, assignment_source in rows
        ],
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


def merge_embedding_with_optional_flip(
    main_embedding: list[float],
    flip_embedding: list[float] | None,
    flip_weight: float = 1.0,
) -> list[float]:
    main_vec = np.asarray(main_embedding, dtype=np.float32)
    merged = main_vec

    safe_flip_weight = float(flip_weight)
    if flip_embedding is not None and safe_flip_weight > 0:
        flip_vec = np.asarray(flip_embedding, dtype=np.float32)
        if flip_vec.shape != main_vec.shape:
            raise ValueError(
                f"flip embedding 维度不匹配: main={main_vec.shape}, flip={flip_vec.shape}"
            )
        merged = main_vec + safe_flip_weight * flip_vec

    merged_norm = float(np.linalg.norm(merged))
    if merged_norm > 1e-9:
        return (merged / merged_norm).astype(float).tolist()

    main_norm = float(np.linalg.norm(main_vec))
    if main_norm > 1e-9:
        return (main_vec / main_norm).astype(float).tolist()
    return main_vec.astype(float).tolist()


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


def _normalize_embedding(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-9:
        return vector.astype(np.float32)
    return (vector / norm).astype(np.float32)


def _build_cluster_representative(
    cluster: dict[str, Any],
    rep_top_k: int,
    embedding_key: str = "embedding",
) -> np.ndarray | None:
    members = list(cluster.get("members", []))
    if not members:
        return None

    sorted_members = sorted(members, key=lambda row: -(row.get("quality_score") or 0.0))
    top_k = max(1, int(rep_top_k))

    vectors: list[np.ndarray] = []
    weights: list[float] = []
    dim: int | None = None
    for member in sorted_members:
        vector = _coerce_embedding_vector(member.get(embedding_key))
        if vector is None:
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
    cluster_representatives_flip: dict[int, np.ndarray] = {}
    for cluster in normal_clusters:
        representative = _build_cluster_representative(
            cluster,
            rep_top_k=rep_top_k,
            embedding_key="embedding",
        )
        if representative is None:
            representative = None
        representative_flip = _build_cluster_representative(
            cluster,
            rep_top_k=rep_top_k,
            embedding_key="embedding_flip",
        )
        cluster_label = int(cluster.get("cluster_label", -1))
        if representative is not None:
            cluster_representatives[cluster_label] = representative
        if representative_flip is not None:
            cluster_representatives_flip[cluster_label] = representative_flip

    if not cluster_representatives and not cluster_representatives_flip:
        return list(labels), list(probabilities), 0

    person_cluster_labels: list[list[int]] = []
    person_idx_by_cluster_label: dict[int, int] = {}
    for person in persons:
        candidate_labels: list[int] = []
        for cluster_ref in person.get("clusters", []):
            label = int(cluster_ref.get("cluster_label", -1))
            if label in cluster_representatives or label in cluster_representatives_flip:
                candidate_labels.append(label)
        if candidate_labels:
            person_idx = len(person_cluster_labels)
            person_cluster_labels.append(candidate_labels)
            for label in candidate_labels:
                person_idx_by_cluster_label[label] = person_idx

    if not person_cluster_labels:
        return list(labels), list(probabilities), 0

    rep_main_labels = sorted(cluster_representatives.keys())
    rep_flip_labels = sorted(cluster_representatives_flip.keys())
    rep_main_index = (
        _build_vector_search_index(
            vectors=np.stack([cluster_representatives[label] for label in rep_main_labels], axis=0),
            prefer_ann=True,
        )
        if rep_main_labels
        else None
    )
    rep_flip_index = (
        _build_vector_search_index(
            vectors=np.stack([cluster_representatives_flip[label] for label in rep_flip_labels], axis=0),
            prefer_ann=True,
        )
        if rep_flip_labels
        else None
    )
    use_candidate_pruning = max(len(rep_main_labels), len(rep_flip_labels)) >= ANN_CANDIDATE_PRUNE_MIN_ITEMS

    updated_labels = list(labels)
    updated_probabilities = list(probabilities)
    attached_count = 0

    def candidate_cluster_labels_for_face(
        vector_main: np.ndarray,
        vector_flip: np.ndarray | None,
    ) -> list[list[int]]:
        if not use_candidate_pruning:
            return person_cluster_labels

        candidate_person_indices: set[int] = set()
        query_k_main = max(ANN_DEFAULT_QUERY_K, int(rep_top_k) * 8)
        query_k_flip = max(ANN_DEFAULT_QUERY_K, int(rep_top_k) * 8)

        if rep_main_index is not None and rep_main_labels:
            top_k = min(int(query_k_main), len(rep_main_labels))
            main_indices, _ = _query_vector_search_index(rep_main_index, vector_main[None, :], top_k=top_k)
            if main_indices.size > 0:
                for pos in main_indices[0].tolist():
                    cluster_label = int(rep_main_labels[int(pos)])
                    owner = person_idx_by_cluster_label.get(cluster_label)
                    if owner is not None:
                        candidate_person_indices.add(int(owner))

        if rep_flip_index is not None and rep_flip_labels and vector_flip is not None:
            top_k = min(int(query_k_flip), len(rep_flip_labels))
            flip_indices, _ = _query_vector_search_index(rep_flip_index, vector_flip[None, :], top_k=top_k)
            if flip_indices.size > 0:
                for pos in flip_indices[0].tolist():
                    cluster_label = int(rep_flip_labels[int(pos)])
                    owner = person_idx_by_cluster_label.get(cluster_label)
                    if owner is not None:
                        candidate_person_indices.add(int(owner))

        # 需要至少两个候选人物，否则 margin 语义不稳定，回退全量比较。
        if len(candidate_person_indices) < 2:
            return person_cluster_labels
        return [person_cluster_labels[idx] for idx in sorted(candidate_person_indices)]

    def build_person_candidates(
        cluster_labels_by_person: list[list[int]],
        vector_main: np.ndarray,
        vector_flip: np.ndarray | None,
        use_flip_supplement: bool,
    ) -> list[tuple[float, float, int]]:
        person_candidates: list[tuple[float, float, int]] = []
        for cluster_labels in cluster_labels_by_person:
            best_label: int | None = None
            best_sim: float | None = None
            for cluster_label in cluster_labels:
                main_sim: float | None = None
                rep_main = cluster_representatives.get(cluster_label)
                if rep_main is not None:
                    main_sim = float(np.clip(np.dot(rep_main, vector_main), -1.0, 1.0))

                flip_sim: float | None = None
                if use_flip_supplement and vector_flip is not None:
                    rep_flip = cluster_representatives_flip.get(cluster_label)
                    if rep_flip is not None:
                        flip_sim = float(np.clip(np.dot(rep_flip, vector_flip), -1.0, 1.0))

                if main_sim is None and flip_sim is None:
                    continue
                if main_sim is None:
                    current_sim = float(flip_sim)  # type: ignore[arg-type]
                elif flip_sim is None:
                    current_sim = float(main_sim)
                else:
                    current_sim = float(max(main_sim, flip_sim))

                if best_sim is None or current_sim > best_sim:
                    best_sim = current_sim
                    best_label = cluster_label

            if best_label is None or best_sim is None:
                continue
            person_candidates.append((1.0 - best_sim, best_sim, best_label))

        person_candidates.sort(key=lambda item: item[0])
        return person_candidates

    def evaluate_pass(candidates: list[tuple[float, float, int]]) -> tuple[bool, int]:
        if not candidates:
            return False, -1
        best_dist, best_sim, best_label = candidates[0]
        second_sim = candidates[1][1] if len(candidates) >= 2 else -1.0
        margin = best_sim - second_sim
        if best_dist > float(distance_threshold):
            return False, -1
        if margin < float(margin_threshold):
            return False, -1
        return True, int(best_label)

    for idx, (face, label) in enumerate(zip(faces, labels, strict=True)):
        if int(label) != -1:
            continue
        if bool(face.get("quality_gate_excluded", False)):
            continue

        vector = _coerce_embedding_vector(face.get("embedding"))
        if vector is None:
            continue
        vector = _normalize_embedding(vector)

        vector_flip: np.ndarray | None = None
        flip = _coerce_embedding_vector(face.get("embedding_flip"))
        if flip is not None:
            vector_flip = _normalize_embedding(flip)

        candidate_cluster_labels = candidate_cluster_labels_for_face(vector_main=vector, vector_flip=vector_flip)

        main_candidates = build_person_candidates(
            cluster_labels_by_person=candidate_cluster_labels,
            vector_main=vector,
            vector_flip=None,
            use_flip_supplement=False,
        )
        supplement_candidates = build_person_candidates(
            cluster_labels_by_person=candidate_cluster_labels,
            vector_main=vector,
            vector_flip=vector_flip,
            use_flip_supplement=True,
        )

        main_pass, main_label = evaluate_pass(main_candidates)
        supplement_pass, supplement_label = evaluate_pass(supplement_candidates)

        if main_pass:
            final_label = main_label
        elif supplement_pass:
            final_label = supplement_label
        else:
            continue

        updated_labels[idx] = final_label
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


@dataclass
class _VectorSearchIndex:
    vectors: np.ndarray
    hnsw_index: Any | None = None


def _build_vector_search_index(vectors: np.ndarray, prefer_ann: bool = True) -> _VectorSearchIndex:
    safe_vectors = np.asarray(vectors, dtype=np.float32)
    if safe_vectors.ndim != 2 or safe_vectors.shape[0] <= 0:
        return _VectorSearchIndex(vectors=np.zeros((0, 0), dtype=np.float32), hnsw_index=None)

    hnsw_index: Any | None = None
    if prefer_ann and safe_vectors.shape[0] >= ANN_BUILD_MIN_ITEMS:
        try:
            import hnswlib

            dim = int(safe_vectors.shape[1])
            index = hnswlib.Index(space="cosine", dim=dim)
            index.init_index(max_elements=int(safe_vectors.shape[0]), ef_construction=200, M=16)
            index.add_items(safe_vectors, np.arange(int(safe_vectors.shape[0]), dtype=np.int32))
            index.set_ef(max(64, min(512, ANN_DEFAULT_QUERY_K * 4)))
            hnsw_index = index
        except Exception:
            hnsw_index = None

    return _VectorSearchIndex(vectors=safe_vectors, hnsw_index=hnsw_index)


def _query_vector_search_index(
    index: _VectorSearchIndex,
    queries: np.ndarray,
    top_k: int,
) -> tuple[np.ndarray, np.ndarray]:
    if index.vectors.ndim != 2 or index.vectors.shape[0] <= 0:
        return np.zeros((0, 0), dtype=np.int32), np.zeros((0, 0), dtype=np.float32)

    query_matrix = np.asarray(queries, dtype=np.float32)
    if query_matrix.ndim == 1:
        query_matrix = query_matrix[None, :]
    if query_matrix.ndim != 2 or query_matrix.shape[0] <= 0:
        return np.zeros((0, 0), dtype=np.int32), np.zeros((0, 0), dtype=np.float32)

    effective_k = max(1, min(int(top_k), int(index.vectors.shape[0])))
    if index.hnsw_index is not None:
        try:
            labels, distances = index.hnsw_index.knn_query(query_matrix, k=effective_k)
            return labels.astype(np.int32), distances.astype(np.float32)
        except Exception:
            pass

    sims = np.clip(query_matrix @ index.vectors.T, -1.0, 1.0)
    order = np.argpartition(-sims, kth=effective_k - 1, axis=1)[:, :effective_k]
    picked_sims = np.take_along_axis(sims, order, axis=1)
    picked_order = np.argsort(-picked_sims, axis=1)
    sorted_indices = np.take_along_axis(order, picked_order, axis=1).astype(np.int32)
    sorted_sims = np.take_along_axis(picked_sims, picked_order, axis=1)
    sorted_dist = np.maximum(1.0 - sorted_sims, 0.0).astype(np.float32)
    return sorted_indices, sorted_dist


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


def _build_cluster_cannot_link_pairs(clusters: list[dict[str, Any]]) -> set[tuple[int, int]]:
    if len(clusters) <= 1:
        return set()

    photo_to_cluster_indices: dict[str, list[int]] = defaultdict(list)
    for idx, cluster in enumerate(clusters):
        seen_photos: set[str] = set()
        for member in cluster.get("members", []):
            photo_relpath = str(member.get("photo_relpath", ""))
            if photo_relpath:
                seen_photos.add(photo_relpath)
        for photo in seen_photos:
            photo_to_cluster_indices[photo].append(idx)

    cannot_link_pairs: set[tuple[int, int]] = set()
    for indices in photo_to_cluster_indices.values():
        if len(indices) <= 1:
            continue
        sorted_indices = sorted(set(indices))
        for left, right in combinations(sorted_indices, 2):
            cannot_link_pairs.add((int(left), int(right)))
    return cannot_link_pairs


def _build_cluster_cannot_link_mask(clusters: list[dict[str, Any]]) -> np.ndarray:
    size = len(clusters)
    mask = np.zeros((size, size), dtype=bool)
    if size <= 1:
        return mask
    for left, right in _build_cluster_cannot_link_pairs(clusters):
        mask[left, right] = True
        mask[right, left] = True
    return mask


def _cluster_single_linkage_sparse(
    vectors: np.ndarray,
    clusters: list[dict[str, Any]],
    distance_threshold: float,
    knn_k: int,
    enable_same_photo_cannot_link: bool,
) -> list[int]:
    size = int(vectors.shape[0])
    if size <= 1:
        return [0]

    effective_k = max(1, min(int(knn_k), size - 1))
    # 需要 +1 保留 self，随后丢弃 self 项。
    neighbor_indices, _ = _query_vector_search_index(
        _build_vector_search_index(vectors=vectors, prefer_ann=True),
        queries=vectors,
        top_k=effective_k + 1,
    )

    cannot_link_pairs = _build_cluster_cannot_link_pairs(clusters) if enable_same_photo_cannot_link else set()

    parent = list(range(size))
    rank = [0 for _ in range(size)]

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        root_x = find(x)
        root_y = find(y)
        if root_x == root_y:
            return
        if rank[root_x] < rank[root_y]:
            parent[root_x] = root_y
        elif rank[root_x] > rank[root_y]:
            parent[root_y] = root_x
        else:
            parent[root_y] = root_x
            rank[root_x] += 1

    candidate_edges: set[tuple[int, int]] = set()
    for row_idx in range(size):
        row_neighbors = neighbor_indices[row_idx].tolist() if row_idx < neighbor_indices.shape[0] else []
        for col_idx in row_neighbors:
            col = int(col_idx)
            if col == row_idx:
                continue
            left, right = (row_idx, col) if row_idx < col else (col, row_idx)
            candidate_edges.add((left, right))

    threshold = float(distance_threshold)
    for left, right in candidate_edges:
        if (left, right) in cannot_link_pairs:
            continue
        sim = float(np.clip(np.dot(vectors[left], vectors[right]), -1.0, 1.0))
        dist = max(1.0 - sim, 0.0)
        if dist <= threshold:
            union(left, right)

    root_to_label: dict[int, int] = {}
    labels: list[int] = []
    next_label = 0
    for idx in range(size):
        root = find(idx)
        if root not in root_to_label:
            root_to_label[root] = next_label
            next_label += 1
        labels.append(root_to_label[root])
    return labels


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
            if linkage == "single":
                ahc_labels = _cluster_single_linkage_sparse(
                    vectors=vector_matrix,
                    clusters=indexed_clusters,
                    distance_threshold=float(distance_threshold),
                    knn_k=int(knn_k),
                    enable_same_photo_cannot_link=bool(enable_same_photo_cannot_link),
                )
            else:
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
        vector = _coerce_embedding_vector(member.get("embedding"))
        if vector is None:
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

        target_anchor_vectors: dict[int, np.ndarray] = {}
        for owner, matrix in target_vectors.items():
            target_anchor_vectors[int(owner)] = _normalize_embedding(np.mean(matrix, axis=0))
        target_anchor_labels = sorted(target_anchor_vectors.keys())
        target_anchor_index = (
            _build_vector_search_index(
                vectors=np.stack([target_anchor_vectors[label] for label in target_anchor_labels], axis=0),
                prefer_ann=True,
            )
            if target_anchor_labels
            else None
        )
        use_target_candidate_pruning = len(target_anchor_labels) >= ANN_CANDIDATE_PRUNE_MIN_ITEMS

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

            cluster_vectors = _collect_normalized_member_embeddings(clusters_by_label[cluster_label])
            if cluster_vectors is None or cluster_vectors.size <= 0:
                continue

            candidate_targets = [owner for owner in target_person_labels if owner != current_owner and owner in target_vectors]
            if not candidate_targets:
                continue
            if use_target_candidate_pruning and target_anchor_index is not None:
                cluster_anchor = _normalize_embedding(np.mean(cluster_vectors, axis=0))
                query_k = min(max(int(knn_top_n) * 4, ANN_DEFAULT_QUERY_K), len(target_anchor_labels))
                ann_indices, _ = _query_vector_search_index(target_anchor_index, cluster_anchor[None, :], top_k=query_k)
                ann_targets = [
                    int(target_anchor_labels[int(pos)])
                    for pos in ann_indices[0].tolist()
                    if int(target_anchor_labels[int(pos)]) != current_owner and int(target_anchor_labels[int(pos)]) in target_vectors
                ]
                # 至少两个候选目标时再裁剪，避免 margin 语义受损。
                if len(ann_targets) >= 2:
                    candidate_targets = sorted(set(ann_targets))

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
    fallback_full = meta.get("fallback_full", {})
    fallback_conclusion = str(fallback_full.get("conclusion", "unknown"))
    if fallback_conclusion not in {"no", "likely_yes", "unknown"}:
        fallback_conclusion = "unknown"
    fallback_conclusion_text = html.escape(str(fallback_full.get("conclusion_text", "")))
    fallback_reason_text = html.escape(str(fallback_full.get("reason_text", "")))
    fallback_evidence = list(fallback_full.get("evidence", [])) if isinstance(fallback_full, dict) else []
    fallback_panel = ""
    if fallback_conclusion_text or fallback_evidence:
        evidence_items = "".join(f"<li>{html.escape(str(item))}</li>" for item in fallback_evidence)
        fallback_panel = f"""
    <section class=\"panel fallback-panel\">
      <div class=\"fallback-head\">
        <h2>Fallback Full 判定</h2>
        <span class=\"fallback-badge {fallback_conclusion}\">{fallback_conclusion_text}</span>
      </div>
      <p class=\"fallback-reason\">{fallback_reason_text}</p>
      <ul class=\"evidence-list\">
        {evidence_items}
      </ul>
    </section>
        """

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
    .fallback-panel {{
      display: grid;
      gap: 10px;
    }}
    .fallback-head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
    }}
    .fallback-head h2 {{
      margin: 0;
      font-size: 18px;
    }}
    .fallback-badge {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 12px;
      font-weight: 700;
      border: 1px solid transparent;
      white-space: nowrap;
    }}
    .fallback-badge.no {{
      color: #0d5e3f;
      background: #edf9f2;
      border-color: #b8e2c8;
    }}
    .fallback-badge.likely_yes {{
      color: #8b2b06;
      background: #fff3e8;
      border-color: #f1c7ad;
    }}
    .fallback-badge.unknown {{
      color: #5e4b12;
      background: #fff9df;
      border-color: #ead994;
    }}
    .fallback-reason {{
      margin: 0;
      color: var(--text);
      font-size: 14px;
      line-height: 1.5;
    }}
    .evidence-list {{
      margin: 0;
      padding-left: 18px;
      color: var(--sub);
      display: grid;
      gap: 4px;
    }}
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
    {fallback_panel}
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


def _cluster_member_count(cluster: dict[str, Any]) -> int:
    return int(cluster.get("member_count", len(cluster.get("members", []))))


def _person_page_payload(base_payload: dict[str, Any], person: dict[str, Any]) -> dict[str, Any]:
    base_meta = dict(base_payload.get("meta", {}))
    person_clusters = list(person.get("clusters", []))
    person_cluster_count = int(person.get("person_cluster_count", len(person_clusters)))
    person_face_count = int(
        person.get("person_face_count", sum(_cluster_member_count(cluster) for cluster in person_clusters))
    )
    person_noise_count = sum(
        _cluster_member_count(cluster)
        for cluster in person_clusters
        if int(cluster.get("cluster_label", -1)) == -1 or str(cluster.get("cluster_key", "")) == "noise"
    )
    person_cluster_total = sum(
        1 for cluster in person_clusters if int(cluster.get("cluster_label", -1)) != -1 and str(cluster.get("cluster_key", "")) != "noise"
    )
    base_meta.update(
        {
            "person_count": 1,
            "person_cluster_count": person_cluster_count,
            "face_count": person_face_count,
            "cluster_count": person_cluster_total,
            "noise_count": person_noise_count,
        }
    )
    return {
        "meta": base_meta,
        "failed_images": base_payload.get("failed_images", []),
        "failed_faces": base_payload.get("failed_faces", []),
        "persons": [person],
        "clusters": person_clusters,
    }


def _render_large_person_pages_index(pages: list[dict[str, Any]], min_face_count: int) -> str:
    rows = []
    for page in sorted(
        pages,
        key=lambda item: (-int(item.get("person_face_count", 0)), int(item.get("person_label", 0))),
    ):
        person_key = html.escape(str(page.get("person_key", "")))
        html_name = html.escape(str(page.get("html", "")))
        person_face_count = int(page.get("person_face_count", 0))
        person_cluster_count = int(page.get("person_cluster_count", 0))
        rows.append(
            (
                "<tr>"
                f"<td>{person_key}</td>"
                f"<td>{person_face_count}</td>"
                f"<td>{person_cluster_count}</td>"
                f"<td><a href=\"{html_name}\" target=\"_blank\">打开 review</a></td>"
                "</tr>"
            )
        )

    if rows:
        tbody = "".join(rows)
    else:
        tbody = (
            "<tr>"
            "<td colspan=\"4\">当前没有样本数大于阈值的人物。</td>"
            "</tr>"
        )

    return f"""<!DOCTYPE html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"UTF-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
  <title>大人物 Review 索引</title>
  <style>
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
      color: #1a2330;
      background: #f6f8fb;
    }}
    main {{
      max-width: 1080px;
      margin: 0 auto;
      padding: 24px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 28px;
    }}
    p {{
      color: #5a6b82;
      margin: 0 0 20px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: #fff;
      border: 1px solid #d7deea;
      border-radius: 12px;
      overflow: hidden;
    }}
    th, td {{
      padding: 12px 14px;
      border-bottom: 1px solid #e3e9f3;
      text-align: left;
    }}
    th {{
      background: #edf3ff;
      font-weight: 700;
    }}
    tr:last-child td {{
      border-bottom: none;
    }}
    a {{
      color: #1f5eff;
      text-decoration: none;
    }}
  </style>
</head>
<body>
  <main>
    <h1>大人物 Review 索引</h1>
    <p>仅收录样本数大于 {int(min_face_count)} 的 person。</p>
    <table>
      <thead>
        <tr>
          <th>Person</th>
          <th>样本数</th>
          <th>微簇数</th>
          <th>Review</th>
        </tr>
      </thead>
      <tbody>{tbody}</tbody>
    </table>
  </main>
</body>
</html>"""


def _cleanup_generated_person_review_pages(output_dir: Path) -> None:
    for path in output_dir.glob("review_person_*.html"):
        if PERSON_REVIEW_HTML_RE.fullmatch(path.name):
            path.unlink(missing_ok=True)


def write_person_review_pages(output_dir: Path, payload: dict[str, Any]) -> list[dict[str, Any]]:
    persons = [
        person
        for person in list(payload.get("persons", []))
        if int(person.get("person_face_count", 0)) > int(LARGE_PERSON_REVIEW_MIN_FACE_COUNT)
    ]
    pages: list[dict[str, Any]] = []
    _cleanup_generated_person_review_pages(output_dir)

    for idx, person in enumerate(persons):
        try:
            person_label = int(person.get("person_label", idx))
        except (TypeError, ValueError):
            person_label = idx
        person_key = str(person.get("person_key", f"person_{person_label}"))
        person_html = f"review_person_{person_label:04d}.html"
        person_payload = _person_page_payload(base_payload=payload, person=person)
        (output_dir / person_html).write_text(render_review_html(person_payload), encoding="utf-8")
        pages.append(
            {
                "person_label": person_label,
                "person_key": person_key,
                "person_face_count": int(person.get("person_face_count", 0)),
                "person_cluster_count": int(person.get("person_cluster_count", len(person.get("clusters", [])))),
                "html": person_html,
            }
        )

    person_pages_payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(pages),
        "pages": pages,
    }
    (output_dir / "review_person_pages.json").write_text(
        json.dumps(person_pages_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    large_person_pages = [
        page for page in pages if int(page.get("person_face_count", 0)) > int(LARGE_PERSON_REVIEW_MIN_FACE_COUNT)
    ]
    large_pages_payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "min_face_count": int(LARGE_PERSON_REVIEW_MIN_FACE_COUNT),
        "count": len(large_person_pages),
        "pages": large_person_pages,
    }
    (output_dir / "review_person_pages_over_100.json").write_text(
        json.dumps(large_pages_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "review_person_pages_over_100.html").write_text(
        _render_large_person_pages_index(
            pages=large_person_pages,
            min_face_count=int(LARGE_PERSON_REVIEW_MIN_FACE_COUNT),
        ),
        encoding="utf-8",
    )
    return pages


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


def _run_detection_items(
    source_dir: Path,
    output_dir: Path,
    insightface_root: Path,
    detector_model_name: str,
    det_size: int,
    preview_max_side: int,
    items: list[dict[str, str]],
    detect_restart_interval: int,
) -> int:
    if not items:
        return 0

    dirs = _ensure_dirs(output_dir)
    conn = open_pipeline_db(dirs["db"])
    safe_restart_interval = max(1, int(detect_restart_interval))
    detector: FaceAnalysis | None = None
    last_restart_processed_count = 0
    processed_count = 0
    total_items = len(items)
    try:
        for idx, item in enumerate(items, start=1):
            source_key = str(item["source_key"])
            relpath = str(item["photo_relpath"])
            image_path = source_dir / relpath

            if detector is None or (processed_count - last_restart_processed_count) >= safe_restart_interval:
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
                last_restart_processed_count = processed_count

            print(f"[det {idx}/{total_items}] 处理 {relpath}")

            rgb_image: Image.Image | None = None
            rgb_arr: np.ndarray | None = None
            bgr_arr: np.ndarray | None = None
            faces = None
            crop_img: Image.Image | None = None
            context_img: Image.Image | None = None
            aligned_bgr: np.ndarray | None = None
            try:
                if not image_path.exists():
                    raise FileNotFoundError(f"图片不存在: {image_path}")
                rgb_image = _load_rgb_image(image_path)
                rgb_arr = np.asarray(rgb_image)
                bgr_arr = cv2.cvtColor(rgb_arr, cv2.COLOR_RGB2BGR)
                height, width = bgr_arr.shape[:2]

                photo_key = hashlib.sha1(relpath.encode("utf-8")).hexdigest()[:16]

                faces = detector.get(bgr_arr)
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
                _mark_source_image_detect_result(conn, source_key=source_key, photo_relpath=relpath, status="done")
                processed_count += 1
            except Exception as exc:  # pragma: no cover
                _mark_source_image_detect_result(
                    conn, source_key=source_key, photo_relpath=relpath, status="error", error=str(exc)
                )
                processed_count += 1
            finally:
                del rgb_image, rgb_arr, bgr_arr, faces, crop_img, context_img, aligned_bgr
                if idx % 20 == 0:
                    gc.collect()
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
            _refresh_source_detect_stage(conn, source_key=source_key)
        return processed_count
    finally:
        if detector is not None:
            del detector
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        conn.close()


def _run_detection_worker_batch(
    source_dir: Path,
    output_dir: Path,
    insightface_root: Path,
    detector_model_name: str,
    det_size: int,
    preview_max_side: int,
    source_key: str,
    batch_size: int,
    detect_restart_interval: int,
) -> int:
    dirs = _ensure_dirs(output_dir)
    conn = open_pipeline_db(dirs["db"])
    try:
        items = _list_pending_source_images_for_source(conn, source_key=source_key, limit=batch_size)
    finally:
        conn.close()
    if not items:
        return 0
    print(f"detect worker：source={source_key} batch={len(items)}")
    return _run_detection_items(
        source_dir=source_dir,
        output_dir=output_dir,
        insightface_root=insightface_root,
        detector_model_name=detector_model_name,
        det_size=det_size,
        preview_max_side=preview_max_side,
        items=items,
        detect_restart_interval=detect_restart_interval,
    )


def _run_detection_subprocess_worker(
    *,
    source_dir: Path,
    output_dir: Path,
    insightface_root: Path,
    detector_model_name: str,
    det_size: int,
    preview_max_side: int,
    source_key: str,
    batch_size: int,
    detect_restart_interval: int,
) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    command = [
        sys.executable,
        "-m",
        "hikbox_pictures.face_review_pipeline",
        "--detect-worker",
        "--source",
        str(source_dir),
        "--output",
        str(output_dir),
        "--insightface-root",
        str(insightface_root),
        "--detector-model-name",
        str(detector_model_name),
        "--det-size",
        str(int(det_size)),
        "--preview-max-side",
        str(int(preview_max_side)),
        "--detect-restart-interval",
        str(int(detect_restart_interval)),
        "--detect-worker-source-key",
        str(source_key),
        "--detect-worker-batch-size",
        str(max(1, int(batch_size))),
    ]
    subprocess.run(command, check=True, cwd=str(repo_root))
    return max(1, int(batch_size))


def _cluster_with_hdbscan(
    embeddings: list[list[float]] | np.ndarray,
    min_cluster_size: int,
    min_samples: int,
) -> tuple[list[int], list[float]]:
    import hdbscan

    if isinstance(embeddings, np.ndarray):
        vectors = np.asarray(embeddings, dtype=np.float32)
    else:
        if not embeddings:
            return [], []
        vectors = np.asarray(embeddings, dtype=np.float32)

    if vectors.ndim != 2 or int(vectors.shape[0]) <= 0:
        return [], []
    if int(vectors.shape[0]) < max(2, min_cluster_size, min_samples):
        return [-1 for _ in range(int(vectors.shape[0]))], [0.0 for _ in range(int(vectors.shape[0]))]

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


def _ensure_dirs(output_dir: Path) -> dict[str, Path]:
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
    detect_restart_interval: int = 300,
    refresh_discover: bool = False,
) -> dict[str, Any]:
    dirs = _ensure_dirs(output_dir)
    conn = open_pipeline_db(dirs["db"])
    _bootstrap_sources_if_needed(conn, source_dir)
    if refresh_discover:
        _refresh_discoverable_sources(conn, source_dir)
    sources = list_pipeline_sources(conn)

    set_meta(conn, "source", str(source_dir))
    set_meta(conn, "detector_model_name", detector_model_name)
    set_meta(conn, "det_size", det_size)
    set_meta(conn, "preview_max_side", preview_max_side)
    set_meta(conn, "last_detection_at", datetime.now().isoformat(timespec="seconds"))
    set_meta(conn, "source_scan_mode", "source_staged_db_resume")
    set_meta(conn, "source_count", len(sources))
    set_meta(conn, "source_refresh_requested", bool(refresh_discover))

    print("阶段 detect：source discover -> detect pending 队列")
    discover_source_count = 0
    new_discovered_count = 0
    for source in sources:
        if str(source.get("discover_status", "pending")) == "done":
            continue
        discover_source_count += 1
        source_key = str(source["source_key"])
        source_relpath = str(source["source_relpath"])
        _set_source_stage_status(conn, source_key=source_key, stage="discover", status="running")
        conn.commit()
        relpaths = _iter_source_image_relpaths(source_dir=source_dir, source_relpath=source_relpath)
        new_discovered_count += _register_source_images(conn, source_key=source_key, photo_relpaths=relpaths)
        _set_source_stage_status(conn, source_key=source_key, stage="discover", status="done")
        _refresh_source_detect_stage(conn, source_key=source_key)
        conn.commit()

    for source in list_pipeline_sources(conn):
        _refresh_source_detect_stage(conn, source_key=str(source["source_key"]))

    _refresh_all_source_embed_stage(conn)
    _refresh_all_source_cluster_stage(conn)

    discovered_image_count = _count_source_images(conn)
    set_meta(conn, "discovered_image_count", discovered_image_count)

    pending_items = _list_pending_source_images(conn)
    total_pending = len(pending_items)
    already_processed_count = max(discovered_image_count - total_pending, 0)
    print(
        f"detect discover：source_discovered={discover_source_count} "
        f"total={discovered_image_count} "
        f"new={new_discovered_count} "
        f"already_done={already_processed_count} "
        f"pending={total_pending}"
    )

    if total_pending <= 0:
        source_stage_counts = _build_source_stage_counts(conn)
        summary = {
            "image_count": discovered_image_count,
            "discovered_image_count": discovered_image_count,
            "new_discovered_image_count": new_discovered_count,
            "skipped_image_count": already_processed_count,
            "pending_image_count": 0,
            "processed_image_count": 0,
            "remaining_image_count": 0,
            "detected_face_count": count_all_faces(conn),
            "pending_face_count": count_pending_faces(conn),
            "failed_image_count": len(list_failed_images(conn)),
            "db_path": str(dirs["db"]),
            **source_stage_counts,
        }
        conn.close()
        return summary

    safe_restart_interval = max(1, int(detect_restart_interval))
    processed_count = 0

    pending_source_keys = sorted({str(item["source_key"]) for item in pending_items})
    for source_key in pending_source_keys:
        _set_source_stage_status(conn, source_key=source_key, stage="detect", status="running")
    conn.commit()

    for source_key in pending_source_keys:
        while True:
            batch_items = _list_pending_source_images_for_source(conn, source_key=source_key, limit=safe_restart_interval)
            if not batch_items:
                _refresh_source_detect_stage(conn, source_key=source_key)
                break
            batch_size = len(batch_items)
            print(
                f"阶段 detect：启动子进程处理批次 source={source_key} "
                f"batch={batch_size} "
                f"总体进度={processed_count}/{total_pending}"
            )
            processed_count += _run_detection_subprocess_worker(
                source_dir=source_dir,
                output_dir=output_dir,
                insightface_root=insightface_root,
                detector_model_name=detector_model_name,
                det_size=det_size,
                preview_max_side=preview_max_side,
                source_key=source_key,
                batch_size=batch_size,
                detect_restart_interval=safe_restart_interval,
            )
            _refresh_source_detect_stage(conn, source_key=source_key)

    _refresh_all_source_embed_stage(conn)
    _refresh_all_source_cluster_stage(conn)
    remaining_pending = len(_list_pending_source_images(conn))
    source_stage_counts = _build_source_stage_counts(conn)
    summary = {
        "image_count": discovered_image_count,
        "discovered_image_count": discovered_image_count,
        "new_discovered_image_count": new_discovered_count,
        "skipped_image_count": already_processed_count,
        "pending_image_count": total_pending,
        "processed_image_count": processed_count,
        "remaining_image_count": remaining_pending,
        "detected_face_count": count_all_faces(conn),
        "pending_face_count": count_pending_faces(conn),
        "failed_image_count": len(list_failed_images(conn)),
        "db_path": str(dirs["db"]),
        **source_stage_counts,
    }
    conn.close()
    return summary


def run_embedding_stage(
    output_dir: Path,
    magface_checkpoint: Path,
    enable_flip_embedding: bool = False,
    flip_weight: float = 1.0,
) -> dict[str, Any]:
    dirs = _ensure_dirs(output_dir)
    conn = open_pipeline_db(dirs["db"])
    _refresh_all_source_embed_stage(conn)
    _refresh_all_source_cluster_stage(conn)

    pending_count = count_pending_faces(conn)
    print(f"阶段 embed：MagFace embedding（待处理 {pending_count}）")

    if pending_count == 0:
        source_stage_counts = _build_source_stage_counts(conn)
        summary = {
            "pending_face_count": 0,
            "embedded_face_count": count_embedded_faces(conn),
            "failed_face_count": count_failed_faces(conn),
            "db_path": str(dirs["db"]),
            **source_stage_counts,
        }
        conn.close()
        return summary

    running_source_rows = conn.execute(
        """
        SELECT DISTINCT src.source_key
        FROM detected_faces AS face
        INNER JOIN source_images AS src ON src.photo_relpath = face.photo_relpath
        WHERE face.embedding_blob IS NULL
          AND face.embedding_json IS NULL
          AND face.face_error IS NULL
        ORDER BY src.source_key
        """
    ).fetchall()
    for row in running_source_rows:
        _set_source_stage_status(conn, source_key=str(row["source_key"]), stage="embed", status="running")
    conn.commit()

    embedder = MagFaceEmbedder(checkpoint_path=magface_checkpoint)
    safe_flip_weight = float(flip_weight)
    enable_flip_cache = bool(enable_flip_embedding) and safe_flip_weight > 0
    flip_embeddings: dict[str, list[float]] = {}
    pending_rows = iter_faces_pending_embedding(conn)
    embedded_batch: list[tuple[str, list[float] | np.ndarray, float, float]] = []
    error_batch: list[tuple[str, str]] = []
    batch_size = 256
    for idx, row in enumerate(pending_rows, start=1):
        face_id = str(row.get("face_id", ""))
        try:
            aligned_path = output_dir / str(row["aligned_relpath"])
            aligned_bgr = cv2.imread(str(aligned_path), cv2.IMREAD_COLOR)
            if aligned_bgr is None:
                raise FileNotFoundError(f"aligned 文件不存在或无法读取: {aligned_path}")

            embedding_main, magface_quality = embedder.embed(aligned_bgr)
            if enable_flip_cache:
                aligned_bgr_flip = cv2.flip(aligned_bgr, 1)
                embedding_flip, _ = embedder.embed(aligned_bgr_flip)
                flip_embeddings[face_id] = embedding_flip
            det_conf = float(row["detector_confidence"])
            area_ratio = float(row["face_area_ratio"])
            quality_score = float(magface_quality * max(0.05, det_conf) * np.sqrt(max(area_ratio, 1e-9)))

            embedded_batch.append(
                (
                    face_id,
                    embedding_main,
                    magface_quality,
                    quality_score,
                )
            )
        except Exception as exc:  # pragma: no cover
            error_batch.append((face_id, str(exc)))

        if len(embedded_batch) >= batch_size:
            mark_faces_embedded_batch(conn, embedded_batch)
            embedded_batch = []
        if len(error_batch) >= batch_size:
            mark_face_errors_batch(conn, error_batch)
            error_batch = []

        if idx % 200 == 0 or idx == pending_count:
            print(f"[emb {idx}/{pending_count}]")

    if embedded_batch:
        mark_faces_embedded_batch(conn, embedded_batch)
    if error_batch:
        mark_face_errors_batch(conn, error_batch)

    del embedder
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    flip_cache_path = _flip_embedding_cache_path(output_dir)
    if enable_flip_cache:
        _save_flip_embeddings_cache(output_dir, flip_embeddings)
    elif flip_cache_path.exists():
        flip_cache_path.unlink()

    set_meta(conn, "magface_checkpoint", str(magface_checkpoint))
    set_meta(conn, "embedding_flip_enabled", bool(enable_flip_cache))
    set_meta(conn, "embedding_flip_weight", safe_flip_weight)
    set_meta(conn, "last_embedding_at", datetime.now().isoformat(timespec="seconds"))
    _refresh_all_source_embed_stage(conn)
    _refresh_all_source_cluster_stage(conn)
    source_stage_counts = _build_source_stage_counts(conn)

    summary = {
        "pending_face_count": pending_count,
        "embedded_face_count": count_embedded_faces(conn),
        "failed_face_count": count_failed_faces(conn),
        "db_path": str(dirs["db"]),
        **source_stage_counts,
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
        if obs.embedding_flip is not None:
            row["embedding_flip"] = obs.embedding_flip
    return row


def _load_existing_manifest(output_dir: Path) -> dict[str, Any] | None:
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


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
    person_consensus_distance_threshold: float | None = 0.24,
    person_consensus_margin_threshold: float = 0.04,
    person_consensus_rep_top_k: int = 3,
    low_quality_micro_cluster_max_size: int = 3,
    low_quality_micro_cluster_top2_weight: float = 0.5,
    low_quality_micro_cluster_min_quality_evidence: float | None = 0.72,
    face_min_quality_for_assignment: float | None = 0.25,
    person_cluster_recall_distance_threshold: float | None = 0.32,
    person_cluster_recall_margin_threshold: float = 0.04,
    person_cluster_recall_top_n: int = 5,
    person_cluster_recall_min_votes: int = 3,
    person_cluster_recall_source_max_cluster_size: int = 20,
    person_cluster_recall_source_max_person_faces: int = 8,
    person_cluster_recall_target_min_person_faces: int = 40,
    person_cluster_recall_max_rounds: int = 2,
) -> dict[str, Any]:
    dirs = _ensure_dirs(output_dir)
    conn = open_pipeline_db(dirs["db"])
    _refresh_all_source_embed_stage(conn)
    _refresh_all_source_cluster_stage(conn)
    sources = list_pipeline_sources(conn)
    pending_cluster_sources = [
        str(source["source_key"])
        for source in sources
        if str(source.get("embed_status", "pending")) == "done"
        and str(source.get("cluster_status", "pending")) == "pending"
    ]

    existing_payload = _load_existing_manifest(output_dir)
    if not pending_cluster_sources and existing_payload is not None:
        source_stage_counts = _build_source_stage_counts(conn)
        image_count = int(get_meta(conn, "discovered_image_count", 0) or 0)
        if image_count <= 0:
            image_count = _count_source_images(conn)
        existing_payload["failed_images"] = list_failed_images(conn)
        existing_payload["failed_faces"] = list_failed_faces(conn)
        existing_payload.setdefault("meta", {})
        existing_payload["meta"].update(
            {
                "source": str(source_dir),
                "image_count": image_count,
                "detected_face_count": count_all_faces(conn),
                "db_path": str(dirs["db"]),
                **source_stage_counts,
            }
        )
        person_pages = write_person_review_pages(output_dir=output_dir, payload=existing_payload)
        existing_payload["meta"]["person_review_page_count"] = len(person_pages)
        existing_payload["meta"]["person_review_pages_manifest"] = "review_person_pages.json"
        existing_payload["meta"]["large_person_review_min_face_count"] = int(LARGE_PERSON_REVIEW_MIN_FACE_COUNT)
        existing_payload["meta"]["large_person_review_page_count"] = len(person_pages)
        existing_payload["meta"]["large_person_review_pages_manifest"] = "review_person_pages_over_100.json"
        existing_payload["meta"]["large_person_review_index_html"] = "review_person_pages_over_100.html"
        (output_dir / "manifest.json").write_text(
            json.dumps(existing_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (output_dir / "review.html").write_text(render_review_html(existing_payload), encoding="utf-8")
        conn.close()
        return existing_payload

    for source in sources:
        source_key = str(source["source_key"])
        embed_status = str(source.get("embed_status", "pending"))
        cluster_status = str(source.get("cluster_status", "pending"))
        if embed_status == "done" and cluster_status == "pending":
            _set_source_stage_status(conn, source_key=source_key, stage="cluster", status="running")
    conn.commit()

    failed_images = list_failed_images(conn)
    failed_faces = list_failed_faces(conn)
    flip_embeddings_by_face = _load_flip_embeddings_cache(output_dir)

    observations: list[FaceObservation] = []
    embedding_vectors: list[np.ndarray] = []
    for row in iter_embedded_faces(conn, as_numpy=True):
        embedding_main = _coerce_embedding_vector(row.get("embedding"))
        if embedding_main is None:
            continue
        bbox_values = [int(v) for v in row["bbox"]]
        face_id = str(row["face_id"])
        embedding_flip = _coerce_embedding_vector(flip_embeddings_by_face.get(face_id))
        observations.append(
            FaceObservation(
                face_id=face_id,
                photo_relpath=str(row["photo_relpath"]),
                crop_relpath=str(row["crop_relpath"]),
                context_relpath=str(row["context_relpath"]),
                bbox=(bbox_values[0], bbox_values[1], bbox_values[2], bbox_values[3]),
                embedding=embedding_main,
                embedding_flip=embedding_flip,
                detector_confidence=float(row["detector_confidence"]),
                face_area_ratio=float(row["face_area_ratio"]),
                magface_quality=float(row["magface_quality"]),
                quality_score=float(row["quality_score"]),
                cluster_assignment_source=row.get("cluster_assignment_source"),
            )
        )
        embedding_vectors.append(embedding_main)

    if embedding_vectors:
        embedding_matrix = np.stack(embedding_vectors, axis=0)
        labels, probabilities = _cluster_with_hdbscan(
            embedding_matrix,
            min_cluster_size=min_cluster_size,
            min_samples=min_samples,
        )
        del embedding_matrix
    else:
        labels, probabilities = [], []

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

    cluster_updates_batch: list[tuple[str, int, float | None, str | None]] = []
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
        cluster_updates_batch.append((obs.face_id, int(label), probability, assignment_source))
        if len(cluster_updates_batch) >= 512:
            update_cluster_results_batch(conn, cluster_updates_batch)
            cluster_updates_batch = []
    if cluster_updates_batch:
        update_cluster_results_batch(conn, cluster_updates_batch)

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
        row.pop("embedding_flip", None)

    image_count = int(get_meta(conn, "discovered_image_count", 0) or 0)
    if image_count <= 0:
        image_count = _count_source_images(conn)

    _refresh_all_source_cluster_stage(conn)
    source_stage_counts = _build_source_stage_counts(conn)
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
            **source_stage_counts,
        },
        "failed_images": failed_images,
        "failed_faces": failed_faces,
        "persons": persons,
        "clusters": grouped_clusters,
        "person_cluster_recall_events": person_cluster_recall_events,
    }

    person_pages = write_person_review_pages(output_dir=output_dir, payload=payload)
    payload["meta"]["person_review_page_count"] = len(person_pages)
    payload["meta"]["person_review_pages_manifest"] = "review_person_pages.json"
    payload["meta"]["large_person_review_min_face_count"] = int(LARGE_PERSON_REVIEW_MIN_FACE_COUNT)
    payload["meta"]["large_person_review_page_count"] = len(
        [page for page in person_pages if int(page.get("person_face_count", 0)) > int(LARGE_PERSON_REVIEW_MIN_FACE_COUNT)]
    )
    payload["meta"]["large_person_review_pages_manifest"] = "review_person_pages_over_100.json"
    payload["meta"]["large_person_review_index_html"] = "review_person_pages_over_100.html"

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
    embedding_enable_flip: bool,
    embedding_flip_weight: float,
    detect_restart_interval: int,
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
    refresh_discover: bool = False,
) -> dict[str, Any]:
    source_dir = source_dir.resolve()
    output_dir = output_dir.resolve()

    if not source_dir.exists():
        raise FileNotFoundError(f"图库目录不存在: {source_dir}")

    print("主流程：detect -> embed -> cluster（基于 DB 续跑）")
    detect_summary = run_detection_stage(
        source_dir=source_dir,
        output_dir=output_dir,
        insightface_root=insightface_root,
        detector_model_name=detector_model_name,
        det_size=det_size,
        preview_max_side=preview_max_side,
        detect_restart_interval=detect_restart_interval,
        refresh_discover=refresh_discover,
    )
    embed_summary = run_embedding_stage(
        output_dir=output_dir,
        magface_checkpoint=magface_checkpoint,
        enable_flip_embedding=embedding_enable_flip,
        flip_weight=embedding_flip_weight,
    )
    payload = run_cluster_stage(
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
    payload.setdefault("meta", {})
    payload["meta"]["detect_summary"] = detect_summary
    payload["meta"]["embed_summary"] = embed_summary
    return payload


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
        "--embedding-enable-flip",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="embed 阶段是否计算水平翻转 embedding；默认开启，可用 --no-embedding-enable-flip 关闭",
    )
    parser.add_argument(
        "--embedding-flip-weight",
        type=float,
        default=1.0,
        help="flip 补充证据开关权重；<=0 视为禁用 flip 补充（当前建议保持 1.0）",
    )
    parser.add_argument(
        "--detect-restart-interval",
        type=int,
        default=300,
        help="detect 阶段每处理 N 张图后重启一次 detector，抑制长跑内存增长",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="显式重新执行 source discover；仅重扫文件系统并为新图片入队，不重置已完成 detect",
    )
    parser.add_argument("--min-cluster-size", type=int, default=2, help="HDBSCAN min_cluster_size")
    parser.add_argument("--min-samples", type=int, default=1, help="HDBSCAN min_samples")
    parser.add_argument("--person-merge-threshold", type=float, default=0.26, help="二阶段 AHC 合并阈值（余弦距离）")
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
        default=0.24,
        help="基于 person-consensus 的一阶段噪声回挂最大余弦距离；默认 0.24，传 <=0 可关闭",
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
        default=0.72,
        help="低质量微簇回退阈值；默认 0.72（证据分低于阈值的微簇整体回退到 noise，传 <=0 可关闭）",
    )
    parser.add_argument(
        "--face-min-quality-for-assignment",
        type=float,
        default=0.25,
        help="face 级质量硬排除阈值；默认 0.25，低于阈值的样本标记为 low_quality_ignored，传 <=0 可关闭",
    )
    parser.add_argument(
        "--person-cluster-recall-distance-threshold",
        type=float,
        default=0.32,
        help="非-noise 微簇归属召回阈值（top-k 平均余弦距离）；默认 0.32，传 <=0 可关闭",
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
        default=20,
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
    parser.add_argument("--detect-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--detect-worker-source-key", type=str, default="", help=argparse.SUPPRESS)
    parser.add_argument("--detect-worker-batch-size", type=int, default=0, help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.detect_worker:
        if not args.detect_worker_source_key:
            raise ValueError("detect worker 缺少 source_key")
        _run_detection_worker_batch(
            source_dir=args.source,
            output_dir=args.output,
            insightface_root=args.insightface_root,
            detector_model_name=args.detector_model_name,
            det_size=args.det_size,
            preview_max_side=args.preview_max_side,
            source_key=args.detect_worker_source_key,
            batch_size=max(1, int(args.detect_worker_batch_size)),
            detect_restart_interval=max(1, int(args.detect_restart_interval)),
        )
        return 0
    payload = run_pipeline(
        source_dir=args.source,
        output_dir=args.output,
        magface_checkpoint=args.magface_checkpoint,
        insightface_root=args.insightface_root,
        detector_model_name=args.detector_model_name,
        det_size=args.det_size,
        embedding_enable_flip=args.embedding_enable_flip,
        embedding_flip_weight=args.embedding_flip_weight,
        detect_restart_interval=args.detect_restart_interval,
        refresh_discover=args.refresh,
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
    )

    meta = payload.get("meta", {})

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
    person_pages_manifest = meta.get("person_review_pages_manifest")
    if isinstance(person_pages_manifest, str) and person_pages_manifest:
        print(f"Person Pages: {args.output / person_pages_manifest}")
    large_person_review_index_html = meta.get("large_person_review_index_html")
    if isinstance(large_person_review_index_html, str) and large_person_review_index_html:
        print(f"Large Person Review: {args.output / large_person_review_index_html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
