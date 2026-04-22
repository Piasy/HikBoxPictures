import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import sqlite3
from pathlib import Path

import numpy as np
from PIL import Image

import hikbox_pictures.face_review_pipeline as face_review_pipeline
import hikbox_pictures.product.engine.frozen_v5 as frozen_v5_engine
from hikbox_pictures.face_review_pipeline import (
    attach_micro_clusters_to_existing_persons,
    attach_noise_faces_to_person_consensus,
    demote_low_quality_micro_clusters,
    exclude_low_quality_faces_from_assignment,
    group_faces_by_cluster,
    merge_clusters_to_persons,
)
from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.scan.execution_service import ScanExecutionService
from hikbox_pictures.product.scan.session_service import ScanSessionRepository
from hikbox_pictures.product.source.repository import SourceRepository
from hikbox_pictures.product.source.service import SourceService


def test_scan_behavior_parity_with_face_review_pipeline_sample(tmp_path: Path, monkeypatch) -> None:
    def fake_hdbscan(vectors, min_cluster_size: int, min_samples: int):
        total = int(vectors.shape[0])
        if total <= 0:
            return [], []
        labels = []
        probs = []
        for idx in range(total):
            if idx == 2:
                labels.append(-1)
                probs.append(0.0)
            else:
                labels.append(0 if idx < max(2, total // 2) else 1)
                probs.append(0.93)
        return labels, probs

    monkeypatch.setattr(frozen_v5_engine, "_cluster_with_hdbscan", fake_hdbscan)
    monkeypatch.setattr(face_review_pipeline, "_cluster_with_hdbscan", fake_hdbscan)

    layout, session_id = _seed_workspace_for_parity(tmp_path)
    service = ScanExecutionService(db_path=layout.library_db, output_root=tmp_path / "runtime")

    service.run_session(
        scan_session_id=session_id,
        detector=_parity_detector,
        embedding_calculator=_test_embedding_calculator,
    )

    product_person_count, product_assignment_count, source_dist = _load_product_stats(layout.library_db)
    baseline_person_count, baseline_assignment_count = _build_pipeline_baseline(layout.library_db, layout.embedding_db)

    assert abs(product_person_count - baseline_person_count) <= 1
    assert abs(product_assignment_count - baseline_assignment_count) <= 2
    assert "hdbscan" in source_dist
    assert "person_consensus" in source_dist or product_assignment_count == 0


def test_scan_behavior_parity_with_face_review_pipeline_sample_without_hdbscan_stub(tmp_path: Path) -> None:
    layout, session_id = _seed_workspace_for_parity(tmp_path)
    service = ScanExecutionService(db_path=layout.library_db, output_root=tmp_path / "runtime")
    service.run_session(
        scan_session_id=session_id,
        detector=_parity_detector,
        embedding_calculator=_test_embedding_calculator,
    )

    product_person_count, product_assignment_count, source_dist = _load_product_stats(layout.library_db)
    baseline_person_count, baseline_assignment_count = _build_pipeline_baseline(layout.library_db, layout.embedding_db)

    assert abs(product_person_count - baseline_person_count) <= 2
    assert abs(product_assignment_count - baseline_assignment_count) <= 3
    assert source_dist.issubset({"hdbscan", "person_consensus", "merge", "undo"})


def _build_pipeline_baseline(library_db: Path, embedding_db: Path) -> tuple[int, int]:
    lib_conn = sqlite3.connect(library_db)
    emb_conn = sqlite3.connect(embedding_db)
    try:
        rows = lib_conn.execute(
            """
            SELECT f.id, f.photo_asset_id, f.quality_score
            FROM face_observation AS f
            WHERE f.active=1
            ORDER BY f.id ASC
            """
        ).fetchall()
        faces = []
        for obs_id, asset_id, quality_score in rows:
            emb_rows = emb_conn.execute(
                "SELECT variant, vector_blob FROM face_embedding WHERE face_observation_id=?",
                (int(obs_id),),
            ).fetchall()
            embedding_main = None
            embedding_flip = None
            for variant, blob in emb_rows:
                vec = np.frombuffer(blob, dtype=np.float32)
                if str(variant) == "main":
                    embedding_main = vec.tolist()
                elif str(variant) == "flip":
                    embedding_flip = vec.tolist()
            if embedding_main is None:
                continue
            faces.append(
                {
                    "face_id": str(obs_id),
                    "photo_relpath": f"asset-{asset_id}",
                    "quality_score": float(quality_score),
                    "embedding": embedding_main,
                    "embedding_flip": embedding_flip,
                }
            )
    finally:
        lib_conn.close()
        emb_conn.close()

    vectors = np.asarray([row["embedding"] for row in faces], dtype=np.float32)
    if vectors.size == 0:
        return 0, 0
    labels, probs = face_review_pipeline._cluster_with_hdbscan(vectors, min_cluster_size=2, min_samples=1)
    labels, probs, excluded_flags, _ = exclude_low_quality_faces_from_assignment(
        faces=[{"quality_score": row["quality_score"]} for row in faces],
        labels=labels,
        probabilities=probs,
        min_quality_score=0.25,
    )
    for row, excluded in zip(faces, excluded_flags, strict=True):
        row["quality_gate_excluded"] = bool(excluded)
    labels, probs, _, _ = demote_low_quality_micro_clusters(
        faces=[{"quality_score": row["quality_score"]} for row in faces],
        labels=labels,
        probabilities=probs,
        max_cluster_size=3,
        top2_weight=0.5,
        min_quality_evidence=0.72,
    )

    preliminary = []
    for row, label, prob in zip(faces, labels, probs, strict=True):
        payload = dict(row)
        payload["cluster_label"] = int(label)
        payload["cluster_probability"] = None if prob is None else float(prob)
        preliminary.append(payload)

    preliminary_clusters = group_faces_by_cluster(preliminary, labels=[int(v) for v in labels])
    preliminary_persons = merge_clusters_to_persons(
        clusters=preliminary_clusters,
        distance_threshold=0.26,
        rep_top_k=3,
        knn_k=8,
        linkage="single",
        enable_same_photo_cannot_link=False,
    )
    labels, probs, _ = attach_noise_faces_to_person_consensus(
        faces=preliminary,
        labels=labels,
        probabilities=probs,
        persons=preliminary_persons,
        rep_top_k=3,
        distance_threshold=0.24,
        margin_threshold=0.04,
    )

    grouped_clusters = group_faces_by_cluster(preliminary, labels=[int(v) for v in labels])
    persons = merge_clusters_to_persons(
        clusters=grouped_clusters,
        distance_threshold=0.26,
        rep_top_k=3,
        knn_k=8,
        linkage="single",
        enable_same_photo_cannot_link=False,
    )
    persons, _, _ = attach_micro_clusters_to_existing_persons(
        persons=persons,
        source_max_cluster_size=20,
        source_max_person_face_count=8,
        target_min_person_face_count=40,
        knn_top_n=5,
        min_votes=3,
        distance_threshold=0.32,
        margin_threshold=0.04,
        max_rounds=2,
    )

    assigned = sum(1 for label in labels if int(label) != -1)
    return len(persons), int(assigned)


def _load_product_stats(library_db: Path) -> tuple[int, int, set[str]]:
    conn = sqlite3.connect(library_db)
    try:
        person_count = int(conn.execute("SELECT COUNT(*) FROM person WHERE status='active'").fetchone()[0])
        assignment_count = int(conn.execute("SELECT COUNT(*) FROM person_face_assignment WHERE active=1").fetchone()[0])
        source_dist = {
            str(row[0])
            for row in conn.execute("SELECT DISTINCT assignment_source FROM person_face_assignment WHERE active=1").fetchall()
        }
    finally:
        conn.close()
    return person_count, assignment_count, source_dist


def _seed_workspace_for_parity(tmp_path: Path) -> tuple[object, int]:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True, exist_ok=True)

    Image.new("RGB", (240, 180), color=(200, 200, 200)).save(source_root / "img_a.jpg")
    Image.new("RGB", (320, 220), color=(180, 210, 200)).save(source_root / "img_b.jpg")
    Image.new("RGB", (260, 190), color=(220, 180, 200)).save(source_root / "img_c.jpg")

    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    SourceService(SourceRepository(layout.library_db)).add_source(str(source_root), label="src")
    session = ScanSessionRepository(layout.library_db).create_session(
        run_kind="scan_full",
        status="running",
        triggered_by="manual_cli",
    )
    return layout, session.id


def _parity_detector(image: np.ndarray) -> list[dict[str, object]]:
    h, w = image.shape[:2]
    rows = [
        {
            "bbox": np.array([w * 0.10, h * 0.15, w * 0.48, h * 0.78], dtype=np.float32),
            "kps": np.array(
                [[w * 0.20, h * 0.30], [w * 0.34, h * 0.30], [w * 0.27, h * 0.43], [w * 0.22, h * 0.56], [w * 0.33, h * 0.56]],
                dtype=np.float32,
            ),
            "det_score": 0.89,
        }
    ]
    if w >= 300:
        rows.append(
            {
                "bbox": np.array([w * 0.57, h * 0.20, w * 0.90, h * 0.72], dtype=np.float32),
                "kps": np.array(
                    [[w * 0.64, h * 0.30], [w * 0.82, h * 0.30], [w * 0.73, h * 0.42], [w * 0.68, h * 0.56], [w * 0.79, h * 0.56]],
                    dtype=np.float32,
                ),
                "det_score": 0.84,
            }
        )
    return rows


def _test_embedding_calculator(aligned_path: Path) -> tuple[list[float], list[float], float]:
    image = Image.open(aligned_path).convert("L")
    try:
        base = np.asarray(image.resize((32, 16), Image.Resampling.BILINEAR), dtype=np.float32).reshape(-1)
        flip = np.asarray(
            image.transpose(Image.Transpose.FLIP_LEFT_RIGHT).resize((32, 16), Image.Resampling.BILINEAR),
            dtype=np.float32,
        ).reshape(-1)
    finally:
        image.close()

    base = base[:512] if base.shape[0] >= 512 else np.pad(base, (0, 512 - base.shape[0]), mode="constant")
    flip = flip[:512] if flip.shape[0] >= 512 else np.pad(flip, (0, 512 - flip.shape[0]), mode="constant")
    base_norm = float(np.linalg.norm(base))
    flip_norm = float(np.linalg.norm(flip))
    base = base if base_norm <= 1e-9 else (base / base_norm)
    flip = flip if flip_norm <= 1e-9 else (flip / flip_norm)
    return base.astype(float).tolist(), flip.astype(float).tolist(), max(base_norm, 1e-6)
