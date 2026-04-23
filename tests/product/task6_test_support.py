from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
from PIL import Image

from hikbox_pictures.product.config import WorkspaceLayout, initialize_workspace
from hikbox_pictures.product.scan.session_service import ScanSessionRepository
from hikbox_pictures.product.source.repository import SourceRepository
from hikbox_pictures.product.source.service import SourceService


def create_task6_workspace(tmp_path: Path) -> tuple[WorkspaceLayout, int, Path]:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True, exist_ok=True)

    Image.new("RGB", (320, 220), color=(210, 210, 210)).save(source_root / "img_a.jpg")
    Image.new("RGB", (320, 220), color=(211, 211, 211)).save(source_root / "img_b.jpg")

    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    source = SourceService(SourceRepository(layout.library_db)).add_source(str(source_root), label="src")
    session = ScanSessionRepository(layout.library_db).create_session(
        run_kind="scan_full",
        status="running",
        triggered_by="manual_cli",
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        for name, fingerprint in (("img_a.jpg", "fp-a"), ("img_b.jpg", "fp-b")):
            conn.execute(
                """
                INSERT INTO photo_asset(
                  library_source_id, primary_path, primary_fingerprint, fingerprint_algo, file_size, mtime_ns, asset_status,
                  created_at, updated_at
                ) VALUES (?, ?, ?, 'sha256', ?, ?, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (source.id, name, fingerprint, 100, 200),
            )
        conn.execute(
            """
            INSERT INTO scan_session_source(
              scan_session_id, library_source_id, stage_status_json, processed_assets, failed_assets, updated_at
            ) VALUES (?, ?, ?, 2, 0, CURRENT_TIMESTAMP)
            """,
            (
                session.id,
                source.id,
                json.dumps(
                    {
                        "discover": "done",
                        "metadata": "done",
                        "detect": "done",
                        "embed": "pending",
                        "cluster": "pending",
                        "assignment": "pending",
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    runtime_root = tmp_path / "runtime"
    return layout, session.id, runtime_root


def seed_face_observations(
    library_db: Path,
    runtime_root: Path,
    specs: list[dict[str, object]],
) -> list[int]:
    aligned_dir = runtime_root / "artifacts" / "aligned"
    aligned_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(library_db)
    try:
        asset_rows = conn.execute("SELECT id FROM photo_asset ORDER BY id ASC").fetchall()
        asset_ids = [int(row[0]) for row in asset_rows]
        observation_ids: list[int] = []
        for idx, spec in enumerate(specs, start=1):
            asset_index = int(spec.get("asset_index", 0))
            relpath = f"artifacts/aligned/f{idx}.png"
            color = tuple(int(v) for v in spec.get("color", (180, 180, 180)))
            Image.new("RGB", (112, 112), color=color).save(runtime_root / relpath)
            cursor = conn.execute(
                """
                INSERT INTO face_observation(
                  photo_asset_id, face_index, crop_relpath, aligned_relpath, context_relpath,
                  bbox_x1, bbox_y1, bbox_x2, bbox_y2,
                  detector_confidence, face_area_ratio, magface_quality, quality_score,
                  active, inactive_reason, pending_reassign, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    asset_ids[asset_index],
                    idx,
                    f"artifacts/crops/f{idx}.jpg",
                    relpath,
                    f"artifacts/context/f{idx}.jpg",
                    10.0,
                    10.0,
                    80.0,
                    80.0,
                    float(spec.get("detector_confidence", 0.92)),
                    float(spec.get("face_area_ratio", 0.25)),
                    float(spec.get("magface_quality", 1.2)),
                    float(spec.get("quality_score", 0.6)),
                    int(bool(spec.get("pending_reassign", False))),
                ),
            )
            observation_ids.append(int(cursor.lastrowid))
        conn.commit()
        return observation_ids
    finally:
        conn.close()


def upsert_face_embeddings(
    embedding_db: Path,
    *,
    face_observation_id: int,
    main: list[float],
    flip: list[float] | None = None,
) -> None:
    conn = sqlite3.connect(embedding_db)
    try:
        _insert_embedding(conn, face_observation_id=face_observation_id, variant="main", vector=main)
        if flip is not None:
            _insert_embedding(conn, face_observation_id=face_observation_id, variant="flip", vector=flip)
        conn.commit()
    finally:
        conn.close()


def embedding_from_seed(seed: int) -> list[float]:
    rng = np.random.default_rng(seed)
    vector = rng.normal(size=512).astype(np.float32)
    norm = float(np.linalg.norm(vector))
    if norm > 1e-9:
        vector = vector / norm
    return vector.astype(float).tolist()


def blend_embeddings(left: list[float], right: list[float], *, weight: float) -> list[float]:
    vector = (np.asarray(left, dtype=np.float32) * (1.0 - weight)) + (np.asarray(right, dtype=np.float32) * weight)
    norm = float(np.linalg.norm(vector))
    if norm > 1e-9:
        vector = vector / norm
    return vector.astype(float).tolist()


def fake_embedding_calculator_from_map(embedding_map: dict[str, tuple[list[float], list[float] | None, float]]):
    def _calculator(aligned_path: Path) -> tuple[list[float], list[float] | None, float]:
        key = aligned_path.name
        if key not in embedding_map:
            raise KeyError(f"未配置测试 embedding: {key}")
        return embedding_map[key]

    return _calculator


def _insert_embedding(conn: sqlite3.Connection, *, face_observation_id: int, variant: str, vector: list[float]) -> None:
    safe = np.asarray(vector, dtype=np.float32)
    conn.execute(
        """
        INSERT INTO face_embedding(
          face_observation_id, feature_type, model_key, variant, dim, dtype, vector_blob, created_at
        ) VALUES (?, 'face', 'magface_iresnet100_ms1mv2', ?, 512, 'float32', ?, CURRENT_TIMESTAMP)
        ON CONFLICT(face_observation_id, feature_type, model_key, variant)
        DO UPDATE SET
          vector_blob=excluded.vector_blob,
          created_at=CURRENT_TIMESTAMP
        """,
        (face_observation_id, variant, safe.tobytes()),
    )
