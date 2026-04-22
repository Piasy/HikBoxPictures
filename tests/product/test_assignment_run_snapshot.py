import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import json
import sqlite3
from pathlib import Path

from PIL import Image

from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.scan.assignment_stage import AssignmentStageService
from hikbox_pictures.product.scan.session_service import ScanSessionRepository
from hikbox_pictures.product.source.repository import SourceRepository
from hikbox_pictures.product.source.service import SourceService


def test_param_snapshot_full_frozen_params(tmp_path: Path) -> None:
    layout, session_id, runtime_root = _seed_runtime_workspace(tmp_path)

    service = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    )
    started = service.start_assignment_run(scan_session_id=session_id, run_kind="scan_full")
    snapshot = started.param_snapshot

    assert snapshot["det_size"] == 640
    assert snapshot["preview_max_side"] == 480
    assert snapshot["min_cluster_size"] == 2
    assert snapshot["min_samples"] == 1
    assert snapshot["person_merge_threshold"] == 0.26
    assert snapshot["person_linkage"] == "single"
    assert snapshot["person_rep_top_k"] == 3
    assert snapshot["person_knn_k"] == 8
    assert snapshot["person_enable_same_photo_cannot_link"] is False
    assert snapshot["embedding_enable_flip"] is True
    assert snapshot["person_consensus_distance_threshold"] == 0.24
    assert snapshot["person_consensus_margin_threshold"] == 0.04
    assert snapshot["person_consensus_rep_top_k"] == 3
    assert snapshot["face_min_quality_for_assignment"] == 0.25
    assert snapshot["low_quality_micro_cluster_max_size"] == 3
    assert snapshot["low_quality_micro_cluster_top2_weight"] == 0.5
    assert snapshot["low_quality_micro_cluster_min_quality_evidence"] == 0.72
    assert snapshot["person_cluster_recall_distance_threshold"] == 0.32
    assert snapshot["person_cluster_recall_margin_threshold"] == 0.04
    assert snapshot["person_cluster_recall_top_n"] == 5
    assert snapshot["person_cluster_recall_min_votes"] == 3
    assert snapshot["person_cluster_recall_source_max_cluster_size"] == 20
    assert snapshot["person_cluster_recall_source_max_person_faces"] == 8
    assert snapshot["person_cluster_recall_target_min_person_faces"] == 40
    assert snapshot["person_cluster_recall_max_rounds"] == 2
    assert "embedding_flip_weight" not in snapshot


def test_noise_and_low_quality_ignored_not_persisted_as_assignment(tmp_path: Path, monkeypatch) -> None:
    layout, session_id, runtime_root = _seed_runtime_workspace(tmp_path)
    _seed_face_observations(layout.library_db, runtime_root)

    service = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    )

    def fake_run(*, faces, params):
        assert len(faces) == 3
        return {
            "faces": [
                {
                    "face_observation_id": faces[0]["face_observation_id"],
                    "person_temp_key": "p0",
                    "assignment_source": "hdbscan",
                    "probability": 0.91,
                },
                {
                    "face_observation_id": faces[1]["face_observation_id"],
                    "person_temp_key": None,
                    "assignment_source": "noise",
                    "probability": 0.0,
                },
                {
                    "face_observation_id": faces[2]["face_observation_id"],
                    "person_temp_key": None,
                    "assignment_source": "low_quality_ignored",
                    "probability": 0.0,
                },
            ],
            "persons": [{"person_temp_key": "p0", "face_observation_ids": [faces[0]["face_observation_id"]]}],
            "stats": {"person_count": 1, "assignment_count": 1},
        }

    monkeypatch.setattr("hikbox_pictures.product.scan.assignment_stage.run_frozen_v5_assignment", fake_run)

    run_result = service.run_frozen_v5_assignment(scan_session_id=session_id, run_kind="scan_full")
    assert run_result.assignment_run_id > 0

    conn = sqlite3.connect(layout.library_db)
    try:
        rows = conn.execute(
            "SELECT assignment_source FROM person_face_assignment WHERE assignment_run_id=? ORDER BY id ASC",
            (run_result.assignment_run_id,),
        ).fetchall()
    finally:
        conn.close()

    sources = [str(row[0]) for row in rows]
    assert sources == ["hdbscan"]


def _seed_runtime_workspace(tmp_path: Path) -> tuple[object, int, Path]:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (240, 180), color=(200, 210, 220)).save(source_root / "a.jpg")

    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    source = SourceService(SourceRepository(layout.library_db)).add_source(str(source_root), label="src")
    session = ScanSessionRepository(layout.library_db).create_session(
        run_kind="scan_full",
        status="running",
        triggered_by="manual_cli",
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        conn.execute(
            """
            INSERT INTO photo_asset(
              library_source_id, primary_path, primary_fingerprint, fingerprint_algo, file_size, mtime_ns, asset_status,
              created_at, updated_at
            ) VALUES (?, ?, ?, 'sha256', ?, ?, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (source.id, "a.jpg", "fp-a", 100, 200),
        )
        conn.execute(
            """
            INSERT INTO scan_session_source(
              scan_session_id, library_source_id, stage_status_json, processed_assets, failed_assets, updated_at
            ) VALUES (?, ?, ?, 1, 0, CURRENT_TIMESTAMP)
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


def _seed_face_observations(library_db: Path, runtime_root: Path) -> None:
    aligned_dir = runtime_root / "artifacts" / "aligned"
    aligned_dir.mkdir(parents=True, exist_ok=True)
    relpaths = []
    for idx, color in enumerate(((220, 180, 160), (210, 170, 150), (200, 160, 140)), start=1):
        relpath = f"artifacts/aligned/f{idx}.png"
        Image.new("RGB", (112, 112), color=color).save(runtime_root / relpath)
        relpaths.append(relpath)

    conn = sqlite3.connect(library_db)
    try:
        asset_id = int(conn.execute("SELECT id FROM photo_asset ORDER BY id LIMIT 1").fetchone()[0])
        for idx, relpath in enumerate(relpaths):
            conn.execute(
                """
                INSERT INTO face_observation(
                  photo_asset_id, face_index, crop_relpath, aligned_relpath, context_relpath,
                  bbox_x1, bbox_y1, bbox_x2, bbox_y2,
                  detector_confidence, face_area_ratio, magface_quality, quality_score,
                  active, inactive_reason, pending_reassign, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    asset_id,
                    idx,
                    f"artifacts/crops/f{idx}.jpg",
                    relpath,
                    f"artifacts/context/f{idx}.jpg",
                    10.0,
                    10.0,
                    80.0,
                    80.0,
                    0.9,
                    0.2 + idx * 0.01,
                    1.2 + idx * 0.01,
                    0.4 + idx * 0.05,
                ),
            )
        conn.commit()
    finally:
        conn.close()
