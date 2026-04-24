import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import sqlite3
from pathlib import Path

import numpy as np
from PIL import Image

import hikbox_pictures.product.engine.frozen_v5 as frozen_v5_engine
from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.engine.frozen_v5 import late_fusion_similarity
from hikbox_pictures.product.scan.assignment_stage import AssignmentStageService
from hikbox_pictures.product.scan.session_service import ScanSessionRepository


def test_late_fusion_uses_max_main_flip() -> None:
    assert late_fusion_similarity(sim_main=0.33, sim_flip=0.48) == 0.48
    assert late_fusion_similarity(sim_main=0.66, sim_flip=0.21) == 0.66


def test_main_and_flip_embeddings_persisted_in_embedding_db(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    runtime_root = tmp_path / "runtime"
    layout = initialize_workspace(workspace_root=workspace, external_root=external)

    session = ScanSessionRepository(layout.library_db).create_session(
        run_kind="scan_full",
        status="running",
        triggered_by="manual_cli",
    )

    obs_id = _seed_minimal_observation(layout.library_db, runtime_root, scan_session_id=session.id)

    service = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    )

    def fake_cluster(*, faces, params):
        assert len(faces) == 1
        return {
            "faces": [
                {
                    "face_observation_id": faces[0]["face_observation_id"],
                    "person_temp_key": "p0",
                    "assignment_source": "hdbscan",
                    "probability": 0.88,
                }
            ],
            "persons": [{"person_temp_key": "p0", "face_observation_ids": [faces[0]["face_observation_id"]]}],
            "stats": {"person_count": 1, "assignment_count": 1},
        }

    monkeypatch.setattr("hikbox_pictures.product.scan.assignment_stage.run_frozen_v5_assignment", fake_cluster)

    service.run_frozen_v5_assignment(
        scan_session_id=session.id,
        run_kind="scan_full",
        embedding_calculator=_fake_embedding_calculator,
    )

    conn = sqlite3.connect(layout.embedding_db)
    try:
        rows = conn.execute(
            "SELECT variant, dim, dtype FROM face_embedding WHERE face_observation_id=? ORDER BY variant ASC",
            (obs_id,),
        ).fetchall()
    finally:
        conn.close()

    assert [str(row[0]) for row in rows] == ["flip", "main"]
    assert all(int(row[1]) == 512 for row in rows)
    assert all(str(row[2]) == "float32" for row in rows)


def test_runtime_consensus_uses_late_fusion_max_main_flip(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    runtime_root = tmp_path / "runtime"
    layout = initialize_workspace(workspace_root=workspace, external_root=external)
    session = ScanSessionRepository(layout.library_db).create_session(
        run_kind="scan_full",
        status="running",
        triggered_by="manual_cli",
    )
    _seed_minimal_observation(layout.library_db, runtime_root, scan_session_id=session.id)

    service = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    )

    vec_a_main = np.zeros(512, dtype=np.float32)
    vec_a_main[0] = 1.0
    vec_a_flip = np.zeros(512, dtype=np.float32)
    vec_a_flip[1] = 1.0
    vec_b_main = np.zeros(512, dtype=np.float32)
    vec_b_main[2] = 1.0
    vec_b_flip = np.zeros(512, dtype=np.float32)
    vec_b_flip[1] = 1.0

    def fake_inputs(
        self,
        *,
        scan_session_id: int,
        param_snapshot,
        embedding_calculator=None,
        include_all_active_sources: bool = False,
    ):
        assert include_all_active_sources is False
        return [
            {
                "face_observation_id": 101,
                "photo_asset_id": 1,
                "photo_relpath": "asset-1",
                "quality_score": 0.92,
                "embedding_main": vec_a_main.astype(float).tolist(),
                "embedding_flip": vec_a_flip.astype(float).tolist(),
            },
            {
                "face_observation_id": 202,
                "photo_asset_id": 2,
                "photo_relpath": "asset-2",
                "quality_score": 0.91,
                "embedding_main": vec_b_main.astype(float).tolist(),
                "embedding_flip": vec_b_flip.astype(float).tolist(),
            },
        ]

    def fake_hdbscan(vectors, min_cluster_size: int, min_samples: int):
        return [0, -1], [0.95, 0.0]

    captured = {"rows": []}

    def fake_persist_assignments(self, *, scan_session_id: int, assignment_rows, assignment_run_id: int, person_map, conn):
        captured["rows"] = assignment_rows
        return sum(
            1
            for row in assignment_rows
            if row.get("person_temp_key") is not None and str(row.get("assignment_source")) in {"hdbscan", "person_consensus"}
        )

    monkeypatch.setattr(AssignmentStageService, "_build_face_inputs", fake_inputs)
    monkeypatch.setattr(AssignmentStageService, "_persist_embeddings", lambda self, scan_session_id, faces: None)
    monkeypatch.setattr(AssignmentStageService, "_persist_assignments", fake_persist_assignments)
    monkeypatch.setattr(frozen_v5_engine, "_cluster_with_hdbscan", fake_hdbscan)

    result = service.run_frozen_v5_assignment(scan_session_id=session.id, run_kind="scan_full")
    assert result.assignment_count == 2
    by_obs = {int(row["face_observation_id"]): str(row["assignment_source"]) for row in captured["rows"]}
    assert by_obs[101] == "hdbscan"
    assert by_obs[202] == "person_consensus"


def _seed_minimal_observation(library_db: Path, runtime_root: Path, scan_session_id: int) -> int:
    aligned_path = runtime_root / "artifacts" / "aligned" / "f0.png"
    aligned_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (112, 112), color=(90, 120, 180)).save(aligned_path)

    conn = sqlite3.connect(library_db)
    try:
        conn.execute("INSERT INTO library_source(root_path, label, enabled) VALUES (?, ?, 1)", (str(runtime_root), "src"))
        source_id = int(conn.execute("SELECT id FROM library_source ORDER BY id DESC LIMIT 1").fetchone()[0])
        conn.execute(
            """
            INSERT INTO photo_asset(
              library_source_id, primary_path, primary_fingerprint, fingerprint_algo, file_size, mtime_ns, asset_status,
              created_at, updated_at
            ) VALUES (?, 'a.jpg', 'fp-a', 'sha256', 100, 200, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (source_id,),
        )
        asset_id = int(conn.execute("SELECT id FROM photo_asset ORDER BY id DESC LIMIT 1").fetchone()[0])
        conn.execute(
            """
            INSERT INTO scan_session_source(
              scan_session_id, library_source_id, stage_status_json, processed_assets, failed_assets, updated_at
            ) VALUES (?, ?, ?, 1, 0, CURRENT_TIMESTAMP)
            """,
            (
                int(scan_session_id),
                source_id,
                "{\"discover\":\"done\",\"metadata\":\"done\",\"detect\":\"done\"}",
            ),
        )
        conn.execute(
            """
            INSERT INTO face_observation(
              photo_asset_id, face_index, crop_relpath, aligned_relpath, context_relpath,
              bbox_x1, bbox_y1, bbox_x2, bbox_y2,
              detector_confidence, face_area_ratio, magface_quality, quality_score,
              active, inactive_reason, pending_reassign, created_at, updated_at
            ) VALUES (?, 0, 'artifacts/crops/f0.jpg', ?, 'artifacts/context/f0.jpg',
                      10, 10, 80, 80, 0.9, 0.2, 1.3, 0.6, 1, NULL, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (asset_id, str(aligned_path.relative_to(runtime_root).as_posix())),
        )
        obs_id = int(conn.execute("SELECT id FROM face_observation ORDER BY id DESC LIMIT 1").fetchone()[0])
        conn.commit()
    finally:
        conn.close()
    return obs_id


def _fake_embedding_calculator(aligned_path: Path) -> tuple[list[float], list[float], float]:
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
