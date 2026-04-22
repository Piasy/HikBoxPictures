import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import json
import sqlite3
from pathlib import Path

import numpy as np
from PIL import Image

import hikbox_pictures.product.engine.frozen_v5 as frozen_v5_engine
from hikbox_pictures.product.scan.assignment_stage import AssignmentStageService
from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.scan.execution_service import DetectStageRunResult, ScanExecutionService
from hikbox_pictures.product.scan.session_service import ScanSessionRepository
from hikbox_pictures.product.source.repository import SourceRepository
from hikbox_pictures.product.source.service import SourceService


def test_scan_session_runs_frozen_v5_end_to_end(tmp_path: Path) -> None:
    layout, session_id = _seed_workspace_for_scan(tmp_path)
    runtime_root = tmp_path / "runtime"
    service = ScanExecutionService(db_path=layout.library_db, output_root=runtime_root)

    run_result = service.run_session(
        scan_session_id=session_id,
        detector=_detector_with_multi_faces,
        embedding_calculator=_test_embedding_calculator,
    )

    assert run_result.assignment_run_id > 0

    conn = sqlite3.connect(layout.library_db)
    try:
        stage_rows = conn.execute(
            "SELECT stage_status_json FROM scan_session_source WHERE scan_session_id=? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
        assignment_sources = conn.execute(
            "SELECT DISTINCT assignment_source FROM person_face_assignment ORDER BY assignment_source ASC"
        ).fetchall()
    finally:
        conn.close()

    assert stage_rows
    for row in stage_rows:
        status = json.loads(str(row[0]))
        assert status["discover"] == "done"
        assert status["metadata"] == "done"
        assert status["detect"] == "done"
        assert status["embed"] == "done"
        assert status["cluster"] == "done"
        assert status["assignment"] == "done"

    assert {str(row[0]) for row in assignment_sources}.issubset({"hdbscan", "person_consensus", "merge", "undo"})
    context_files = sorted((runtime_root / "artifacts" / "context").glob("*.jpg"))
    assert context_files
    with Image.open(context_files[0]) as context_img:
        assert max(context_img.size) <= 480


def test_scan_main_chain_calls_run_frozen_v5_assignment(tmp_path: Path, monkeypatch) -> None:
    layout, session_id = _seed_workspace_for_scan(tmp_path)
    service = ScanExecutionService(db_path=layout.library_db, output_root=tmp_path / "runtime")

    called = {"count": 0}
    original = AssignmentStageService.run_frozen_v5_assignment

    def wrapped(self, *args, **kwargs):
        called["count"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(AssignmentStageService, "run_frozen_v5_assignment", wrapped)

    service.run_session(
        scan_session_id=session_id,
        detector=_detector_with_multi_faces,
        embedding_calculator=_test_embedding_calculator,
    )
    assert called["count"] == 1


def test_scan_runtime_rejects_synthetic_observation_embedding_assignment(tmp_path: Path, monkeypatch) -> None:
    layout, session_id = _seed_workspace_for_scan(tmp_path)
    service = ScanExecutionService(db_path=layout.library_db, output_root=tmp_path / "runtime")

    def fake_hdbscan(vectors, min_cluster_size: int, min_samples: int):
        total = int(vectors.shape[0])
        if total <= 0:
            return [], []
        return [0 for _ in range(total)], [0.94 for _ in range(total)]

    monkeypatch.setattr(frozen_v5_engine, "_cluster_with_hdbscan", fake_hdbscan)

    service.run_session(
        scan_session_id=session_id,
        detector=_detector_with_multi_faces,
        embedding_calculator=_test_embedding_calculator,
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        obs_count = int(conn.execute("SELECT COUNT(*) FROM face_observation WHERE active=1").fetchone()[0])
        assignment_count = int(conn.execute("SELECT COUNT(*) FROM person_face_assignment WHERE active=1").fetchone()[0])
        invalid_source_count = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM person_face_assignment
                WHERE assignment_source NOT IN ('hdbscan', 'person_consensus', 'merge', 'undo')
                """
            ).fetchone()[0]
        )
    finally:
        conn.close()

    emb_conn = sqlite3.connect(layout.embedding_db)
    try:
        embedding_count = int(emb_conn.execute("SELECT COUNT(*) FROM face_embedding").fetchone()[0])
    finally:
        emb_conn.close()

    assert obs_count > 0
    assert embedding_count >= obs_count * 2
    assert assignment_count > 0
    assert invalid_source_count == 0


def test_abort_between_detect_and_assignment_interrupts_without_assignment(tmp_path: Path, monkeypatch) -> None:
    layout, session_id = _seed_workspace_for_scan(tmp_path)
    service = ScanExecutionService(db_path=layout.library_db, output_root=tmp_path / "runtime")

    def fake_detect(self, *, scan_session_id: int, runtime_defaults=None, detector=None):
        ScanSessionRepository(layout.library_db).update_status(scan_session_id, status="aborting")
        return DetectStageRunResult(claimed_batches=1, acked_batches=1, interrupted=False)

    def should_not_run_assignment(self, *, scan_session_id: int, run_kind: str, embedding_calculator=None):
        raise AssertionError("detect 后 abort 时不应执行 assignment")

    monkeypatch.setattr(ScanExecutionService, "run_detect_stage", fake_detect)
    monkeypatch.setattr(AssignmentStageService, "run_frozen_v5_assignment", should_not_run_assignment)

    result = service.run_session(
        scan_session_id=session_id,
        detector=_detector_with_multi_faces,
        embedding_calculator=_test_embedding_calculator,
    )
    assert result.assignment_run_id == 0

    conn = sqlite3.connect(layout.library_db)
    try:
        session_status = str(conn.execute("SELECT status FROM scan_session WHERE id=?", (session_id,)).fetchone()[0])
        assignment_run_count = int(conn.execute("SELECT COUNT(*) FROM assignment_run WHERE scan_session_id=?", (session_id,)).fetchone()[0])
    finally:
        conn.close()

    assert session_status == "interrupted"
    assert assignment_run_count == 0


def test_abort_during_assignment_marks_assignment_run_failed_and_session_interrupted(tmp_path: Path, monkeypatch) -> None:
    layout, session_id = _seed_workspace_for_scan(tmp_path)
    service = ScanExecutionService(db_path=layout.library_db, output_root=tmp_path / "runtime")

    original_persist_embeddings = AssignmentStageService._persist_embeddings

    def aborting_persist_embeddings(self, *, scan_session_id: int, faces):
        original_persist_embeddings(self, scan_session_id=scan_session_id, faces=faces)
        ScanSessionRepository(layout.library_db).update_status(scan_session_id, status="aborting")

    monkeypatch.setattr(AssignmentStageService, "_persist_embeddings", aborting_persist_embeddings)
    result = service.run_session(
        scan_session_id=session_id,
        detector=_detector_with_multi_faces,
        embedding_calculator=_test_embedding_calculator,
    )
    assert result.assignment_run_id == 0

    conn = sqlite3.connect(layout.library_db)
    try:
        session_row = conn.execute("SELECT status FROM scan_session WHERE id=?", (session_id,)).fetchone()
        run_row = conn.execute(
            "SELECT status FROM assignment_run WHERE scan_session_id=? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    assert session_row is not None and str(session_row[0]) == "interrupted"
    assert run_row is not None and str(run_row[0]) == "failed"


def _seed_workspace_for_scan(tmp_path: Path) -> tuple[object, int]:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True, exist_ok=True)

    Image.new("RGB", (240, 180), color=(205, 205, 205)).save(source_root / "img_a.jpg")
    Image.new("RGB", (320, 220), color=(206, 206, 206)).save(source_root / "img_b.jpg")
    Image.new("RGB", (280, 210), color=(204, 204, 204)).save(source_root / "img_c.jpg")

    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    SourceService(SourceRepository(layout.library_db)).add_source(str(source_root), label="src")
    session = ScanSessionRepository(layout.library_db).create_session(
        run_kind="scan_full",
        status="running",
        triggered_by="manual_cli",
    )
    return layout, session.id


def _detector_with_multi_faces(image: np.ndarray) -> list[dict[str, object]]:
    h, w = image.shape[:2]
    rows: list[dict[str, object]] = [
        {
            "bbox": np.array([w * 0.12, h * 0.16, w * 0.50, h * 0.78], dtype=np.float32),
            "kps": np.array(
                [[w * 0.21, h * 0.30], [w * 0.36, h * 0.30], [w * 0.29, h * 0.45], [w * 0.24, h * 0.58], [w * 0.35, h * 0.58]],
                dtype=np.float32,
            ),
            "det_score": 0.90,
        }
    ]
    if w >= 300:
        rows.append(
            {
                "bbox": np.array([w * 0.56, h * 0.18, w * 0.90, h * 0.72], dtype=np.float32),
                "kps": np.array(
                    [[w * 0.64, h * 0.30], [w * 0.82, h * 0.30], [w * 0.73, h * 0.43], [w * 0.68, h * 0.56], [w * 0.79, h * 0.56]],
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
