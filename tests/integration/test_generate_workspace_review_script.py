import json
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

from PIL import Image

from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.scan.session_service import ScanSessionRepository
from hikbox_pictures.product.source.repository import SourceRepository
from hikbox_pictures.product.source.service import SourceService


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "generate_workspace_review.py"


def test_generate_workspace_review_script_exports_active_workspace_review(tmp_path: Path) -> None:
    workspace_root, external_root = _seed_workspace_with_active_assignment(tmp_path)
    output_dir = tmp_path / "review"

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--workspace",
            str(workspace_root),
            "--output-dir",
            str(output_dir),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout

    review_html = (output_dir / "review.html").read_text(encoding="utf-8")
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    meta = json.loads((output_dir / "review_payload_meta.json").read_text(encoding="utf-8"))
    person_pages = json.loads((output_dir / "review_person_pages.json").read_text(encoding="utf-8"))

    artifacts_link = output_dir / "artifacts"
    assert artifacts_link.is_symlink()
    assert artifacts_link.resolve() == (external_root / "artifacts").resolve()

    assert meta["assignment_run_id"] == 1
    assert meta["scan_session_id"] == 1
    assert meta["run_kind"] == "scan_full"
    assert meta["image_count"] == 1
    assert meta["face_count"] == 2
    assert meta["cluster_count"] == 1
    assert meta["noise_count"] == 1
    assert meta["person_count"] == 1

    assert manifest["meta"]["model"] == "workspace/frozen_v5"
    assert manifest["meta"]["clusterer"] == "workspace face_cluster"
    assert manifest["meta"]["person_clusterer"] == "frozen_v5"
    assert manifest["meta"]["person_linkage"] == "single"
    assert manifest["meta"]["person_enable_same_photo_cannot_link"] is False
    assert manifest["meta"]["source"].endswith("sources=src")

    assert len(manifest["persons"]) == 1
    assert manifest["persons"][0]["person_key"] == "person_1"
    assert manifest["persons"][0]["person_face_count"] == 1
    assert manifest["persons"][0]["person_cluster_count"] == 1

    clusters_by_key = {cluster["cluster_key"]: cluster for cluster in manifest["clusters"]}
    assert set(clusters_by_key) == {"cluster_1", "noise"}
    assert clusters_by_key["cluster_1"]["member_count"] == 1
    assert clusters_by_key["noise"]["member_count"] == 1

    assert "person_1" in review_html
    assert "cluster_1" in review_html
    assert "noise" in review_html
    assert "artifacts/crops/a1_000.jpg" in review_html
    assert "artifacts/context/a1_001.jpg" in review_html

    assert person_pages["count"] == 0


def test_generate_workspace_review_script_ignores_replaced_cluster_memberships(tmp_path: Path) -> None:
    workspace_root, _ = _seed_workspace_with_active_assignment(tmp_path)
    output_dir = tmp_path / "review"

    conn = sqlite3.connect(workspace_root / ".hikbox" / "library.db")
    try:
        conn.execute(
            """
            INSERT INTO assignment_run(
              scan_session_id, algorithm_version, param_snapshot_json, run_kind,
              started_at, finished_at, status, updated_at
            ) VALUES (1, 'frozen_v5', ?, 'scan_full', ?, ?, 'completed', CURRENT_TIMESTAMP)
            """,
            (
                json.dumps(
                    {
                        "person_linkage": "single",
                        "person_enable_same_photo_cannot_link": False,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "2026-04-23T15:01:00",
                "2026-04-23T15:02:00",
            ),
        )
        conn.execute(
            """
            UPDATE face_cluster
            SET status='replaced',
                updated_assignment_run_id=2,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=1
            """
        )
        conn.execute(
            """
            INSERT INTO face_cluster(
              cluster_uuid, person_id, status, rebuild_scope,
              created_assignment_run_id, updated_assignment_run_id, created_at, updated_at
            ) VALUES (?, 1, 'active', 'full', 2, 2, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (str(uuid.uuid4()),),
        )
        conn.execute(
            """
            INSERT INTO face_cluster_member(face_cluster_id, face_observation_id, assignment_run_id, created_at)
            VALUES (2, 1, 2, CURRENT_TIMESTAMP)
            """
        )
        conn.execute(
            """
            INSERT INTO face_cluster_rep_face(
              face_cluster_id, face_observation_id, rep_rank, assignment_run_id, created_at
            ) VALUES (2, 1, 1, 2, CURRENT_TIMESTAMP)
            """
        )
        conn.execute(
            """
            UPDATE person_face_assignment
            SET assignment_run_id=2,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=1
            """
        )
        conn.commit()
    finally:
        conn.close()

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--workspace",
            str(workspace_root),
            "--output-dir",
            str(output_dir),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout

    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["meta"]["assignment_run_id"] == 2
    assert manifest["persons"][0]["clusters"][0]["cluster_key"] == "cluster_2"
    assert all(cluster["cluster_key"] != "cluster_missing_person_1" for cluster in manifest["persons"][0]["clusters"])


def _seed_workspace_with_active_assignment(tmp_path: Path) -> tuple[Path, Path]:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (320, 240), color=(180, 200, 220)).save(source_root / "a.jpg")

    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    source = SourceService(SourceRepository(layout.library_db)).add_source(str(source_root), label="src")
    session = ScanSessionRepository(layout.library_db).create_session(
        run_kind="scan_full",
        status="completed",
        triggered_by="manual_cli",
        finished_at="2026-04-23T15:00:00",
    )

    _write_image(external_root / "artifacts" / "crops" / "a1_000.jpg", color=(210, 160, 150))
    _write_image(external_root / "artifacts" / "crops" / "a1_001.jpg", color=(180, 140, 130))
    _write_image(external_root / "artifacts" / "context" / "a1_000.jpg", color=(130, 160, 190))
    _write_image(external_root / "artifacts" / "context" / "a1_001.jpg", color=(100, 130, 170))
    _write_image(external_root / "artifacts" / "aligned" / "a1_000.png", color=(200, 180, 160))
    _write_image(external_root / "artifacts" / "aligned" / "a1_001.png", color=(170, 150, 130))

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
        asset_id = int(conn.execute("SELECT id FROM photo_asset ORDER BY id ASC LIMIT 1").fetchone()[0])
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
                        "embed": "done",
                        "cluster": "done",
                        "assignment": "done",
                    },
                    ensure_ascii=False,
                ),
            ),
        )
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
                0,
                "artifacts/crops/a1_000.jpg",
                "artifacts/aligned/a1_000.png",
                "artifacts/context/a1_000.jpg",
                10.0,
                10.0,
                80.0,
                80.0,
                0.95,
                0.22,
                18.2,
                1.10,
            ),
        )
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
                1,
                "artifacts/crops/a1_001.jpg",
                "artifacts/aligned/a1_001.png",
                "artifacts/context/a1_001.jpg",
                30.0,
                20.0,
                100.0,
                100.0,
                0.91,
                0.18,
                16.5,
                0.88,
            ),
        )
        conn.execute(
            """
            INSERT INTO person(
              person_uuid, display_name, is_named, status, merged_into_person_id, created_at, updated_at
            ) VALUES (?, NULL, 0, 'active', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (str(uuid.uuid4()),),
        )
        conn.execute(
            """
            INSERT INTO assignment_run(
              scan_session_id, algorithm_version, param_snapshot_json, run_kind,
              started_at, finished_at, status, updated_at
            ) VALUES (?, 'frozen_v5', ?, 'scan_full', ?, ?, 'completed', CURRENT_TIMESTAMP)
            """,
            (
                session.id,
                json.dumps(
                    {
                        "person_linkage": "single",
                        "person_enable_same_photo_cannot_link": False,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "2026-04-23T14:59:00",
                "2026-04-23T15:00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO face_cluster(
              cluster_uuid, person_id, status, rebuild_scope,
              created_assignment_run_id, updated_assignment_run_id, created_at, updated_at
            ) VALUES (?, 1, 'active', 'full', 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (str(uuid.uuid4()),),
        )
        conn.execute(
            """
            INSERT INTO face_cluster_member(face_cluster_id, face_observation_id, assignment_run_id, created_at)
            VALUES (1, 1, 1, CURRENT_TIMESTAMP)
            """
        )
        conn.execute(
            """
            INSERT INTO face_cluster_rep_face(
              face_cluster_id, face_observation_id, rep_rank, assignment_run_id, created_at
            ) VALUES (1, 1, 1, 1, CURRENT_TIMESTAMP)
            """
        )
        conn.execute(
            """
            INSERT INTO person_face_assignment(
              person_id, face_observation_id, assignment_run_id, assignment_source,
              active, confidence, margin, created_at, updated_at
            ) VALUES (1, 1, 1, 'hdbscan', 1, 0.97, 0.05, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        )
        conn.commit()
    finally:
        conn.close()

    return workspace_root, external_root


def _write_image(path: Path, *, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), color=color).save(path)
