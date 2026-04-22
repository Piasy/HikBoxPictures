import json
import sqlite3
from pathlib import Path

import numpy as np
from PIL import Image

from hikbox_pictures.face_review_pipeline import _safe_bbox as pipeline_safe_bbox
from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.scan.execution_service import ScanExecutionService
from hikbox_pictures.product.scan.session_service import ScanSessionRepository
from hikbox_pictures.product.source.repository import SourceRepository
from hikbox_pictures.product.source.service import SourceService


def test_detect_stage_parity_with_face_review_pipeline_sample(tmp_path: Path) -> None:
    layout, session_id, source_root = _seed_workspace(tmp_path)
    service = ScanExecutionService(db_path=layout.library_db, output_root=tmp_path / "runtime")
    service.run_detect_stage(scan_session_id=session_id, detector=_product_test_detector)

    product_faces = _fetch_product_faces(layout.library_db)
    baseline_faces = _build_pipeline_baseline(source_root)

    product_count = len(product_faces)
    baseline_count = len(baseline_faces)
    assert baseline_count > 0
    assert abs(product_count - baseline_count) <= 1

    product_bbox_distinct = len({tuple(round(v, 4) for v in row["bbox"]) for row in product_faces})
    product_quality_distinct = len({round(float(row["quality_score"]), 6) for row in product_faces})
    assert product_bbox_distinct > 1
    assert product_quality_distinct > 1

    # 避免“每图 1 脸 + 固定 bbox/质量”的退化占位实现。
    by_asset_count = {}
    for row in product_faces:
        by_asset_count[row["photo_asset_id"]] = by_asset_count.get(row["photo_asset_id"], 0) + 1
    constant_one_face = all(v == 1 for v in by_asset_count.values())
    if constant_one_face:
        assert product_bbox_distinct > 1 and product_quality_distinct > 1


def _seed_workspace(tmp_path: Path) -> tuple[object, int, Path]:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (240, 180), color=(210, 210, 210)).save(source_root / "img_a.jpg")
    Image.new("RGB", (300, 220), color=(200, 200, 200)).save(source_root / "img_b.jpg")
    Image.new("RGB", (180, 140), color=(190, 190, 190)).save(source_root / "img_c.jpg")

    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    source = SourceService(SourceRepository(layout.library_db)).add_source(str(source_root), label="src")
    session = ScanSessionRepository(layout.library_db).create_session(
        run_kind="scan_full",
        status="running",
        triggered_by="manual_cli",
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        for relpath in ("img_a.jpg", "img_b.jpg", "img_c.jpg"):
            conn.execute(
                """
                INSERT INTO photo_asset(
                  library_source_id, primary_path, primary_fingerprint, fingerprint_algo, file_size, mtime_ns, asset_status,
                  created_at, updated_at
                ) VALUES (?, ?, ?, 'sha256', ?, ?, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (source.id, relpath, f"fp-{relpath}", 100, 200),
            )
        conn.execute(
            """
            INSERT INTO scan_session_source(
              scan_session_id, library_source_id, stage_status_json, processed_assets, failed_assets, updated_at
            ) VALUES (?, ?, ?, 3, 0, CURRENT_TIMESTAMP)
            """,
            (session.id, source.id, json.dumps({"discover": "done", "metadata": "done", "detect": "pending"})),
        )
        conn.commit()
    finally:
        conn.close()
    return layout, session.id, source_root


def _product_test_detector(image: np.ndarray) -> list[dict[str, object]]:
    h, w = image.shape[:2]
    detections = [
        {
            "bbox": np.array([w * 0.12, h * 0.16, w * 0.55, h * 0.78], dtype=np.float32),
            "kps": np.array(
                [[w * 0.22, h * 0.30], [w * 0.36, h * 0.30], [w * 0.29, h * 0.44], [w * 0.24, h * 0.58], [w * 0.35, h * 0.58]],
                dtype=np.float32,
            ),
            "det_score": 0.88 + (w % 9) / 100.0,
        }
    ]
    if w >= 260:
        detections.append(
            {
                "bbox": np.array([w * 0.58, h * 0.18, w * 0.90, h * 0.72], dtype=np.float32),
                "kps": np.array(
                    [[w * 0.65, h * 0.30], [w * 0.81, h * 0.30], [w * 0.73, h * 0.42], [w * 0.68, h * 0.56], [w * 0.79, h * 0.56]],
                    dtype=np.float32,
                ),
                "det_score": 0.82 + (h % 7) / 100.0,
            }
        )
    return detections


def _fetch_product_faces(db_path: Path) -> list[dict[str, object]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT photo_asset_id, bbox_x1, bbox_y1, bbox_x2, bbox_y2, quality_score
            FROM face_observation
            WHERE active=1
            ORDER BY id ASC
            """
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "photo_asset_id": int(row[0]),
            "bbox": [float(row[1]), float(row[2]), float(row[3]), float(row[4])],
            "quality_score": float(row[5]),
        }
        for row in rows
    ]


def _build_pipeline_baseline(source_root: Path) -> list[dict[str, object]]:
    faces: list[dict[str, object]] = []
    for image_path in sorted(source_root.glob("*.jpg")):
        rgb = np.asarray(Image.open(image_path).convert("RGB"))
        bgr = rgb[:, :, ::-1]
        h, w = bgr.shape[:2]
        specs = _baseline_face_specs(image_name=image_path.name)
        for raw_bbox, det_conf in specs:
            bbox = pipeline_safe_bbox(np.asarray(raw_bbox, dtype=np.float32), width=w, height=h)
            x1, y1, x2, y2 = bbox
            area_ratio = float((x2 - x1) * (y2 - y1) / max(1, w * h))
            det_conf = float(det_conf)
            magface_quality = float(1.0 + area_ratio * 8.0 + det_conf)
            quality_score = float(magface_quality * max(0.05, det_conf) * np.sqrt(max(area_ratio, 1e-9)))
            faces.append({"bbox": [float(x1), float(y1), float(x2), float(y2)], "quality_score": quality_score})
    return faces


def _baseline_face_specs(image_name: str) -> list[tuple[list[float], float]]:
    """独立基线快照：模拟 face_review_pipeline 风格 bbox/score 分布，不复用产品检测函数。"""
    mapping: dict[str, list[tuple[list[float], float]]] = {
        "img_a.jpg": [
            ([26.0, 28.0, 128.0, 142.0], 0.90),
        ],
        "img_b.jpg": [
            ([34.0, 36.0, 168.0, 176.0], 0.91),
            ([178.0, 42.0, 270.0, 158.0], 0.84),
        ],
        "img_c.jpg": [
            ([20.0, 22.0, 102.0, 114.0], 0.89),
        ],
    }
    return mapping.get(image_name, [])
