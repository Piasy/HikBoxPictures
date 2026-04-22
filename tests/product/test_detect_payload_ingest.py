import sqlite3
from pathlib import Path

from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.scan.detect_stage import DetectStageRepository
from hikbox_pictures.product.scan.session_service import ScanSessionRepository
from hikbox_pictures.product.source.repository import SourceRepository
from hikbox_pictures.product.source.service import SourceService


def _seed_one_asset_workspace(tmp_path: Path) -> tuple[Path, int, int]:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "img.jpg").write_bytes(b"fake-jpg")

    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    source = SourceService(SourceRepository(layout.library_db)).add_source(str(source_root), label="src")
    session = ScanSessionRepository(layout.library_db).create_session(
        run_kind="scan_full",
        status="running",
        triggered_by="manual_cli",
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        cursor = conn.execute(
            """
            INSERT INTO photo_asset(
              library_source_id, primary_path, primary_fingerprint, fingerprint_algo, file_size, mtime_ns, asset_status,
              created_at, updated_at
            ) VALUES (?, ?, ?, 'sha256', ?, ?, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (source.id, "img.jpg", "f1", 123, 456),
        )
        asset_id = int(cursor.lastrowid)
        conn.commit()
    finally:
        conn.close()

    return layout.library_db, session.id, asset_id


def _seed_two_asset_workspace(tmp_path: Path) -> tuple[Path, int, tuple[int, int]]:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "img1.jpg").write_bytes(b"fake-jpg-1")
    (source_root / "img2.jpg").write_bytes(b"fake-jpg-2")

    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    source = SourceService(SourceRepository(layout.library_db)).add_source(str(source_root), label="src")
    session = ScanSessionRepository(layout.library_db).create_session(
        run_kind="scan_full",
        status="running",
        triggered_by="manual_cli",
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        cursor1 = conn.execute(
            """
            INSERT INTO photo_asset(
              library_source_id, primary_path, primary_fingerprint, fingerprint_algo, file_size, mtime_ns, asset_status,
              created_at, updated_at
            ) VALUES (?, ?, ?, 'sha256', ?, ?, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (source.id, "img1.jpg", "f1", 123, 456),
        )
        cursor2 = conn.execute(
            """
            INSERT INTO photo_asset(
              library_source_id, primary_path, primary_fingerprint, fingerprint_algo, file_size, mtime_ns, asset_status,
              created_at, updated_at
            ) VALUES (?, ?, ?, 'sha256', ?, ?, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (source.id, "img2.jpg", "f2", 124, 457),
        )
        conn.commit()
    finally:
        conn.close()

    return layout.library_db, session.id, (int(cursor1.lastrowid), int(cursor2.lastrowid))


def test_ack_without_payload_is_rejected(tmp_path: Path) -> None:
    db_path, session_id, _ = _seed_one_asset_workspace(tmp_path)
    repo = DetectStageRepository(db_path)
    repo.prepare_detect_batches(scan_session_id=session_id, batch_size=10, workers=1)
    claimed = repo.claim_detect_batch(scan_session_id=session_id, worker_slot=0)
    assert claimed is not None

    try:
        repo.ack_detect_batch(
            batch_id=claimed.batch_id,
            claim_token=claimed.claim_token,
            worker_payload={"results": []},
        )
    except ValueError:
        return
    raise AssertionError("ack_detect_batch 必须拒绝空 payload")


def test_ack_ingests_worker_faces_into_face_observation(tmp_path: Path) -> None:
    db_path, session_id, asset_id = _seed_one_asset_workspace(tmp_path)
    repo = DetectStageRepository(db_path)
    repo.prepare_detect_batches(scan_session_id=session_id, batch_size=10, workers=1)
    claimed = repo.claim_detect_batch(scan_session_id=session_id, worker_slot=0)
    assert claimed is not None

    worker_payload = {
        "results": [
            {
                "photo_asset_id": asset_id,
                "status": "done",
                "faces": [
                    {
                        "bbox": [10.0, 20.0, 80.0, 100.0],
                        "detector_confidence": 0.96,
                        "face_area_ratio": 0.28,
                        "crop_relpath": "artifacts/crops/a.jpg",
                        "aligned_relpath": "artifacts/aligned/a.png",
                        "context_relpath": "artifacts/context/a.jpg",
                        "magface_quality": 0.8,
                        "quality_score": 0.6,
                    }
                ],
            }
        ]
    }
    repo.ack_detect_batch(
        batch_id=claimed.batch_id,
        claim_token=claimed.claim_token,
        worker_payload=worker_payload,
    )

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT photo_asset_id, bbox_x1, bbox_y1, bbox_x2, bbox_y2, detector_confidence, face_area_ratio,
                   crop_relpath, aligned_relpath, context_relpath
            FROM face_observation
            ORDER BY id ASC
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert int(row[0]) == asset_id
    assert tuple(float(v) for v in row[1:7]) == (10.0, 20.0, 80.0, 100.0, 0.96, 0.28)
    assert tuple(str(v) for v in row[7:10]) == (
        "artifacts/crops/a.jpg",
        "artifacts/aligned/a.png",
        "artifacts/context/a.jpg",
    )


def test_ack_rejects_payload_with_out_of_batch_asset_id_and_no_pollution(tmp_path: Path) -> None:
    db_path, session_id, (asset_a, asset_b) = _seed_two_asset_workspace(tmp_path)
    repo = DetectStageRepository(db_path)
    repo.prepare_detect_batches(scan_session_id=session_id, batch_size=10, workers=1)
    claimed = repo.claim_detect_batch(scan_session_id=session_id, worker_slot=0)
    assert claimed is not None

    try:
        repo.ack_detect_batch(
            batch_id=claimed.batch_id,
            claim_token=claimed.claim_token,
            worker_payload={
                "results": [
                    {"photo_asset_id": asset_a, "status": "done", "faces": []},
                    {"photo_asset_id": asset_b + 999, "status": "done", "faces": []},
                ]
            },
        )
    except ValueError:
        pass
    else:
        raise AssertionError("越界 asset_id 必须被拒绝")

    conn = sqlite3.connect(db_path)
    try:
        obs_count = conn.execute("SELECT COUNT(*) FROM face_observation").fetchone()
        batch_row = conn.execute("SELECT status FROM scan_batch WHERE id=?", (claimed.batch_id,)).fetchone()
    finally:
        conn.close()
    assert obs_count is not None and int(obs_count[0]) == 0
    assert batch_row is not None and str(batch_row[0]) == "running"


def test_ack_rejects_payload_with_missing_asset_id_and_no_pollution(tmp_path: Path) -> None:
    db_path, session_id, (asset_a, _asset_b) = _seed_two_asset_workspace(tmp_path)
    repo = DetectStageRepository(db_path)
    repo.prepare_detect_batches(scan_session_id=session_id, batch_size=10, workers=1)
    claimed = repo.claim_detect_batch(scan_session_id=session_id, worker_slot=0)
    assert claimed is not None

    try:
        repo.ack_detect_batch(
            batch_id=claimed.batch_id,
            claim_token=claimed.claim_token,
            worker_payload={"results": [{"photo_asset_id": asset_a, "status": "done", "faces": []}]},
        )
    except ValueError:
        pass
    else:
        raise AssertionError("缺失 asset_id 必须被拒绝")

    conn = sqlite3.connect(db_path)
    try:
        obs_count = conn.execute("SELECT COUNT(*) FROM face_observation").fetchone()
    finally:
        conn.close()
    assert obs_count is not None and int(obs_count[0]) == 0


def test_ack_rejects_payload_with_duplicate_asset_id_and_no_pollution(tmp_path: Path) -> None:
    db_path, session_id, (asset_a, _asset_b) = _seed_two_asset_workspace(tmp_path)
    repo = DetectStageRepository(db_path)
    repo.prepare_detect_batches(scan_session_id=session_id, batch_size=10, workers=1)
    claimed = repo.claim_detect_batch(scan_session_id=session_id, worker_slot=0)
    assert claimed is not None

    try:
        repo.ack_detect_batch(
            batch_id=claimed.batch_id,
            claim_token=claimed.claim_token,
            worker_payload={
                "results": [
                    {"photo_asset_id": asset_a, "status": "done", "faces": []},
                    {"photo_asset_id": asset_a, "status": "done", "faces": []},
                ]
            },
        )
    except ValueError:
        pass
    else:
        raise AssertionError("重复 asset_id 必须被拒绝")

    conn = sqlite3.connect(db_path)
    try:
        obs_count = conn.execute("SELECT COUNT(*) FROM face_observation").fetchone()
    finally:
        conn.close()
    assert obs_count is not None and int(obs_count[0]) == 0
