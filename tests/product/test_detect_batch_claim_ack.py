import json
import sqlite3
from pathlib import Path

import numpy as np
from PIL import Image

from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.scan import execution_service
from hikbox_pictures.product.scan.detect_stage import DetectStageRepository
from hikbox_pictures.product.scan.execution_service import (
    ScanExecutionService,
    build_scan_runtime_defaults,
    split_batch,
)
from hikbox_pictures.product.scan.session_service import ScanSessionRepository
from hikbox_pictures.product.source.repository import SourceRepository
from hikbox_pictures.product.source.service import SourceService


def test_build_scan_runtime_defaults_and_split_batch_rules() -> None:
    defaults = build_scan_runtime_defaults(cpu_count=8)
    assert defaults.det_size == 640
    assert defaults.batch_size == 300
    assert defaults.workers == 4
    assert build_scan_runtime_defaults(cpu_count=1).workers == 1
    assert build_scan_runtime_defaults(cpu_count=2).workers == 2
    assert build_scan_runtime_defaults(cpu_count=3).workers == 3
    assert build_scan_runtime_defaults(cpu_count=4).workers == 4

    assert split_batch(total=300, workers=3) == [100, 100, 100]
    assert split_batch(total=302, workers=3) == [101, 101, 100]


def test_detect_claim_dispatch_ack_updates_batch_and_items(tmp_path: Path) -> None:
    layout, session_id = _seed_detect_workspace(tmp_path)
    service = ScanExecutionService(db_path=layout.library_db, output_root=tmp_path / "runtime")

    result = service.run_detect_stage(
        scan_session_id=session_id,
        detector=_fake_detector,
    )
    assert result.claimed_batches >= 1
    assert result.acked_batches == result.claimed_batches
    assert result.interrupted is False

    conn = sqlite3.connect(layout.library_db)
    try:
        batch_status = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN status='acked' THEN 1 ELSE 0 END) FROM scan_batch"
        ).fetchone()
        item_status = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) FROM scan_batch_item"
        ).fetchone()
        obs_count = conn.execute("SELECT COUNT(*) FROM face_observation WHERE active=1").fetchone()
        stage_status_row = conn.execute(
            "SELECT stage_status_json FROM scan_session_source WHERE scan_session_id=? LIMIT 1",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    assert batch_status is not None
    assert int(batch_status[0]) >= 1
    assert int(batch_status[0]) == int(batch_status[1] or 0)
    assert item_status is not None
    assert int(item_status[0]) >= 2
    assert int(item_status[0]) == int(item_status[1] or 0)
    assert obs_count is not None and int(obs_count[0]) >= 2
    assert stage_status_row is not None
    stage_status = json.loads(str(stage_status_row[0]))
    assert stage_status["detect"] == "done"


def test_abort_rolls_back_unacked_batches(tmp_path: Path) -> None:
    layout, session_id = _seed_detect_workspace(tmp_path)
    repo = DetectStageRepository(layout.library_db)
    repo.prepare_detect_batches(scan_session_id=session_id, batch_size=10, workers=1)
    claimed = repo.claim_detect_batch(scan_session_id=session_id, worker_slot=0)
    assert claimed is not None

    session_repo = ScanSessionRepository(layout.library_db)
    session_repo.update_status(session_id, status="aborting")
    service = ScanExecutionService(db_path=layout.library_db, output_root=tmp_path / "runtime")
    result = service.run_detect_stage(scan_session_id=session_id, detector=_fake_detector)
    assert result.interrupted is True

    conn = sqlite3.connect(layout.library_db)
    try:
        batch_row = conn.execute(
            "SELECT status, error_message FROM scan_batch WHERE id=?",
            (claimed.batch_id,),
        ).fetchone()
        pending_count_row = conn.execute(
            "SELECT COUNT(*) FROM scan_batch_item WHERE scan_batch_id=? AND status='pending'",
            (claimed.batch_id,),
        ).fetchone()
        session_row = conn.execute("SELECT status FROM scan_session WHERE id=?", (session_id,)).fetchone()
    finally:
        conn.close()

    assert batch_row is not None
    assert str(batch_row[0]) == "failed"
    assert "aborting" in str(batch_row[1])
    assert pending_count_row is not None and int(pending_count_row[0]) >= 1
    assert session_row is not None and str(session_row[0]) == "interrupted"


def test_detect_processes_more_than_batch_size_and_zero_face_assets_without_requeue(
    tmp_path: Path,
    monkeypatch,
) -> None:
    layout, session_id = _seed_detect_workspace_bulk(tmp_path, asset_count=302)
    service = ScanExecutionService(db_path=layout.library_db, output_root=tmp_path / "runtime")

    def fake_worker(_request: dict[str, object], *, detector=None) -> dict[str, object]:
        items = _request["items"]
        return {
            "results": [
                {
                    "photo_asset_id": int(item["photo_asset_id"]),
                    "status": "done",
                    "faces": [],
                }
                for item in items
            ]
        }

    monkeypatch.setattr("hikbox_pictures.product.scan.execution_service.run_detect_worker", fake_worker)
    result = service.run_detect_stage(scan_session_id=session_id, detector=lambda _img: [])
    assert result.interrupted is False
    assert result.acked_batches >= 2

    conn = sqlite3.connect(layout.library_db)
    try:
        item_stats = conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done_count,
              SUM(CASE WHEN status IN ('pending', 'running') THEN 1 ELSE 0 END) AS non_terminal_count
            FROM scan_batch_item
            """
        ).fetchone()
        obs_count = conn.execute("SELECT COUNT(*) FROM face_observation WHERE active=1").fetchone()
        stage_status_row = conn.execute(
            "SELECT stage_status_json FROM scan_session_source WHERE scan_session_id=? LIMIT 1",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    assert item_stats is not None
    assert int(item_stats[0]) == 302
    assert int(item_stats[1] or 0) == 302
    assert int(item_stats[2] or 0) == 0
    assert obs_count is not None and int(obs_count[0]) == 0
    assert stage_status_row is not None
    assert json.loads(str(stage_status_row[0]))["detect"] == "done"


def test_detect_default_path_uses_subprocess_worker(tmp_path: Path, monkeypatch) -> None:
    layout, session_id = _seed_detect_workspace(tmp_path)
    service = ScanExecutionService(db_path=layout.library_db, output_root=tmp_path / "runtime")
    called = {"count": 0}

    def fake_subprocess_worker(request: dict[str, object], *, workdir: Path) -> dict[str, object]:
        called["count"] += 1
        assert request["items"]
        assert str(workdir).endswith("runtime")
        return {
            "results": [
                {
                    "photo_asset_id": int(item["photo_asset_id"]),
                    "status": "done",
                    "faces": [],
                }
                for item in request["items"]
            ]
        }

    monkeypatch.setattr(
        "hikbox_pictures.product.scan.execution_service._run_detect_worker_subprocess",
        fake_subprocess_worker,
    )

    result = service.run_detect_stage(scan_session_id=session_id)
    assert result.interrupted is False
    assert called["count"] >= 1


def test_run_detect_worker_subprocess_writes_resolved_insightface_root(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    response_payload = {"results": []}
    captured_request: dict[str, object] = {}

    def fake_subprocess_run(cmd: list[str], *, check: bool) -> None:
        assert check is True
        request_path = Path(cmd[cmd.index("--request-json") + 1])
        response_path = Path(cmd[cmd.index("--response-json") + 1])
        captured_request.update(json.loads(request_path.read_text(encoding="utf-8")))
        response_path.write_text(json.dumps(response_payload, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(
        execution_service,
        "_resolve_insightface_root",
        lambda: Path("/shared-cache/.insightface"),
        raising=False,
    )
    monkeypatch.setattr(execution_service.subprocess, "run", fake_subprocess_run)

    payload = execution_service._run_detect_worker_subprocess(
        {"items": [{"photo_asset_id": 1, "image_path": "demo.jpg", "photo_key": "a1"}]},
        workdir=runtime_root,
    )

    assert payload == response_payload
    assert captured_request["insightface_root"] == "/shared-cache/.insightface"


def test_resolve_insightface_root_prefers_ready_ancestor_cache_over_partial_local_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo_root = tmp_path / "repo"
    task_root = repo_root / ".worktrees" / "task-real-data-e2e"
    module_path = task_root / "hikbox_pictures" / "product" / "scan" / "execution_service.py"
    ready_cache = repo_root / ".insightface" / "models" / "buffalo_l"
    partial_cache = task_root / ".insightface" / "models"

    ready_cache.mkdir(parents=True, exist_ok=True)
    partial_cache.mkdir(parents=True, exist_ok=True)
    (ready_cache / "det_10g.onnx").write_bytes(b"ready")
    (partial_cache / "buffalo_l.zip").write_bytes(b"partial")
    module_path.parent.mkdir(parents=True, exist_ok=True)
    module_path.write_text("# fake module path\n", encoding="utf-8")

    monkeypatch.chdir(task_root)
    monkeypatch.setattr(execution_service, "__file__", str(module_path))

    assert execution_service._resolve_insightface_root() == repo_root / ".insightface"


def test_replace_face_observation_keeps_existing_pending_reassign_on_same_face_index(tmp_path: Path) -> None:
    layout, _ = _seed_detect_workspace(tmp_path)
    repo = DetectStageRepository(layout.library_db)
    photo_asset_id = 1
    baseline_face = {
        "bbox": [10.0, 12.0, 130.0, 150.0],
        "detector_confidence": 0.91,
        "face_area_ratio": 0.22,
        "magface_quality": 1.13,
        "quality_score": 0.99,
        "crop_relpath": "crops/a.png",
        "aligned_relpath": "aligned/a.png",
        "context_relpath": "context/a.png",
    }
    second_face = {
        **baseline_face,
        "bbox": [20.0, 22.0, 150.0, 170.0],
        "crop_relpath": "crops/b.png",
        "aligned_relpath": "aligned/b.png",
        "context_relpath": "context/b.png",
    }

    conn = sqlite3.connect(layout.library_db)
    try:
        repo._replace_face_observations(conn, photo_asset_id=photo_asset_id, faces=[baseline_face, second_face])
        first_rows = conn.execute(
            """
            SELECT id, face_index, pending_reassign
            FROM face_observation
            WHERE photo_asset_id=?
            ORDER BY face_index ASC
            """,
            (photo_asset_id,),
        ).fetchall()
        assert len(first_rows) == 2
        first_face_row_id = int(first_rows[0][0])
        conn.execute(
            "UPDATE face_observation SET pending_reassign=1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (first_face_row_id,),
        )

        updated_face = {
            **baseline_face,
            "detector_confidence": 0.97,
            "quality_score": 1.07,
            "crop_relpath": "crops/a2.png",
            "aligned_relpath": "aligned/a2.png",
            "context_relpath": "context/a2.png",
        }
        updated_second_face = {
            **second_face,
            "detector_confidence": 0.95,
            "quality_score": 1.01,
            "crop_relpath": "crops/b2.png",
            "aligned_relpath": "aligned/b2.png",
            "context_relpath": "context/b2.png",
        }
        repo._replace_face_observations(conn, photo_asset_id=photo_asset_id, faces=[updated_face, updated_second_face])
        conn.commit()

        second_rows = conn.execute(
            """
            SELECT id, face_index, pending_reassign, detector_confidence, crop_relpath, active
            FROM face_observation
            WHERE photo_asset_id=?
            ORDER BY face_index ASC
            """,
            (photo_asset_id,),
        ).fetchall()
    finally:
        conn.close()

    assert len(second_rows) == 2
    first_face, second_face_row = second_rows
    assert int(first_face[0]) == first_face_row_id
    assert int(first_face[2]) == 1
    assert float(first_face[3]) == 0.97
    assert str(first_face[4]) == "crops/a2.png"
    assert int(first_face[5]) == 1
    assert int(second_face_row[2]) == 0
    assert float(second_face_row[3]) == 0.95
    assert str(second_face_row[4]) == "crops/b2.png"
    assert int(second_face_row[5]) == 1


def test_replace_face_observation_faces_reordered时_pending_reassign仍跟随同一张脸(tmp_path: Path) -> None:
    layout, _ = _seed_detect_workspace(tmp_path)
    repo = DetectStageRepository(layout.library_db)
    photo_asset_id = 1
    face_a = {
        "bbox": [10.0, 12.0, 130.0, 150.0],
        "detector_confidence": 0.91,
        "face_area_ratio": 0.22,
        "magface_quality": 1.13,
        "quality_score": 0.99,
        "crop_relpath": "crops/a.png",
        "aligned_relpath": "aligned/a.png",
        "context_relpath": "context/a.png",
    }
    face_b = {
        "bbox": [160.0, 22.0, 260.0, 170.0],
        "detector_confidence": 0.89,
        "face_area_ratio": 0.19,
        "magface_quality": 1.08,
        "quality_score": 0.93,
        "crop_relpath": "crops/b.png",
        "aligned_relpath": "aligned/b.png",
        "context_relpath": "context/b.png",
    }

    conn = sqlite3.connect(layout.library_db)
    try:
        repo._replace_face_observations(conn, photo_asset_id=photo_asset_id, faces=[face_a, face_b])
        baseline_rows = conn.execute(
            """
            SELECT id, face_index, bbox_x1, bbox_y1, bbox_x2, bbox_y2
            FROM face_observation
            WHERE photo_asset_id=?
            ORDER BY face_index ASC
            """,
            (photo_asset_id,),
        ).fetchall()
        pending_face_row_id = int(baseline_rows[0][0])
        conn.execute(
            "UPDATE face_observation SET pending_reassign=1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (pending_face_row_id,),
        )
        reordered_face_b = {
            **face_b,
            "crop_relpath": "crops/b2.png",
            "aligned_relpath": "aligned/b2.png",
            "context_relpath": "context/b2.png",
        }
        reordered_face_a = {
            **face_a,
            "crop_relpath": "crops/a2.png",
            "aligned_relpath": "aligned/a2.png",
            "context_relpath": "context/a2.png",
        }
        repo._replace_face_observations(conn, photo_asset_id=photo_asset_id, faces=[reordered_face_b, reordered_face_a])
        conn.commit()

        rows = conn.execute(
            """
            SELECT id, face_index, pending_reassign, crop_relpath, bbox_x1, bbox_y1, bbox_x2, bbox_y2
            FROM face_observation
            WHERE photo_asset_id=?
            ORDER BY id ASC
            """,
            (photo_asset_id,),
        ).fetchall()
    finally:
        conn.close()

    row_by_id = {int(row[0]): row for row in rows}
    pending_row = row_by_id[pending_face_row_id]
    other_rows = [row for row in rows if int(row[0]) != pending_face_row_id]

    assert int(pending_row[2]) == 1
    assert str(pending_row[3]) == "crops/a2.png"
    assert (float(pending_row[4]), float(pending_row[5]), float(pending_row[6]), float(pending_row[7])) == (
        10.0,
        12.0,
        130.0,
        150.0,
    )
    assert len(other_rows) == 1
    assert int(other_rows[0][2]) == 0
    assert str(other_rows[0][3]) == "crops/b2.png"


def test_worker_exception_converges_to_failed_without_running_hang(tmp_path: Path, monkeypatch) -> None:
    layout, session_id = _seed_detect_workspace(tmp_path)
    service = ScanExecutionService(db_path=layout.library_db, output_root=tmp_path / "runtime")

    def boom_worker(_request: dict[str, object], *, workdir: Path) -> dict[str, object]:
        raise RuntimeError("worker boom")

    monkeypatch.setattr(
        "hikbox_pictures.product.scan.execution_service._run_detect_worker_subprocess",
        boom_worker,
    )

    try:
        service.run_detect_stage(scan_session_id=session_id)
    except RuntimeError as exc:
        assert "worker boom" in str(exc)
    else:
        raise AssertionError("worker 异常应继续抛出，供上层感知失败")

    conn = sqlite3.connect(layout.library_db)
    try:
        session_row = conn.execute(
            "SELECT status, last_error FROM scan_session WHERE id=?",
            (session_id,),
        ).fetchone()
        batch_running = conn.execute(
            "SELECT COUNT(*) FROM scan_batch WHERE scan_session_id=? AND status='running'",
            (session_id,),
        ).fetchone()
        item_running = conn.execute(
            """
            SELECT COUNT(*)
            FROM scan_batch_item AS i
            JOIN scan_batch AS b ON b.id=i.scan_batch_id
            WHERE b.scan_session_id=? AND i.status='running'
            """,
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    assert session_row is not None
    assert str(session_row[0]) == "failed"
    assert "worker boom" in str(session_row[1])
    assert batch_running is not None and int(batch_running[0]) == 0
    assert item_running is not None and int(item_running[0]) == 0


def test_invalid_worker_payload_converges_to_failed_without_running_hang(tmp_path: Path, monkeypatch) -> None:
    layout, session_id = _seed_detect_workspace(tmp_path)
    service = ScanExecutionService(db_path=layout.library_db, output_root=tmp_path / "runtime")

    def bad_payload_worker(request: dict[str, object], *, workdir: Path) -> dict[str, object]:
        # 故意返回 batch 外 photo_asset_id，触发 ack 完整性校验失败。
        return {
            "results": [
                {
                    "photo_asset_id": int(request["items"][0]["photo_asset_id"]) + 99999,
                    "status": "done",
                    "faces": [],
                }
            ]
        }

    monkeypatch.setattr(
        "hikbox_pictures.product.scan.execution_service._run_detect_worker_subprocess",
        bad_payload_worker,
    )

    try:
        service.run_detect_stage(scan_session_id=session_id)
    except ValueError as exc:
        assert "不一致" in str(exc) or "missing" in str(exc)
    else:
        raise AssertionError("非法 payload 应触发失败并抛异常")

    conn = sqlite3.connect(layout.library_db)
    try:
        session_row = conn.execute(
            "SELECT status, last_error FROM scan_session WHERE id=?",
            (session_id,),
        ).fetchone()
        batch_running = conn.execute(
            "SELECT COUNT(*) FROM scan_batch WHERE scan_session_id=? AND status='running'",
            (session_id,),
        ).fetchone()
        item_running = conn.execute(
            """
            SELECT COUNT(*)
            FROM scan_batch_item AS i
            JOIN scan_batch AS b ON b.id=i.scan_batch_id
            WHERE b.scan_session_id=? AND i.status='running'
            """,
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    assert session_row is not None
    assert str(session_row[0]) == "failed"
    assert str(session_row[1]).strip() != ""
    assert batch_running is not None and int(batch_running[0]) == 0
    assert item_running is not None and int(item_running[0]) == 0


def _seed_detect_workspace(tmp_path: Path) -> tuple[object, int]:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (240, 180), color=(210, 210, 210)).save(source_root / "img1.jpg")
    Image.new("RGB", (220, 160), color=(200, 200, 200)).save(source_root / "img2.jpg")

    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    source = SourceService(SourceRepository(layout.library_db)).add_source(str(source_root), label="src")
    session = ScanSessionRepository(layout.library_db).create_session(
        run_kind="scan_full",
        status="running",
        triggered_by="manual_cli",
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        for relpath in ("img1.jpg", "img2.jpg"):
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
            ) VALUES (?, ?, ?, 2, 0, CURRENT_TIMESTAMP)
            """,
            (session.id, source.id, json.dumps({"discover": "done", "metadata": "done", "detect": "pending"})),
        )
        conn.commit()
    finally:
        conn.close()
    return layout, session.id


def _seed_detect_workspace_bulk(tmp_path: Path, *, asset_count: int) -> tuple[object, int]:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True, exist_ok=True)

    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    source = SourceService(SourceRepository(layout.library_db)).add_source(str(source_root), label="src")
    session = ScanSessionRepository(layout.library_db).create_session(
        run_kind="scan_full",
        status="running",
        triggered_by="manual_cli",
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        for idx in range(asset_count):
            relpath = f"img_{idx:04d}.jpg"
            conn.execute(
                """
                INSERT INTO photo_asset(
                  library_source_id, primary_path, primary_fingerprint, fingerprint_algo, file_size, mtime_ns, asset_status,
                  created_at, updated_at
                ) VALUES (?, ?, ?, 'sha256', ?, ?, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (source.id, relpath, f"fp-{idx}", 100, 200 + idx),
            )
        conn.execute(
            """
            INSERT INTO scan_session_source(
              scan_session_id, library_source_id, stage_status_json, processed_assets, failed_assets, updated_at
            ) VALUES (?, ?, ?, ?, 0, CURRENT_TIMESTAMP)
            """,
            (
                session.id,
                source.id,
                json.dumps({"discover": "done", "metadata": "done", "detect": "pending"}),
                asset_count,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return layout, session.id


def _fake_detector(image: np.ndarray) -> list[dict[str, object]]:
    h, w = image.shape[:2]
    bbox = np.array([max(1, w * 0.15), max(1, h * 0.2), max(2, w * 0.58), max(2, h * 0.82)], dtype=np.float32)
    kps = np.array(
        [
            [bbox[0] + 10, bbox[1] + 12],
            [bbox[0] + 32, bbox[1] + 13],
            [bbox[0] + 22, bbox[1] + 25],
            [bbox[0] + 14, bbox[1] + 38],
            [bbox[0] + 30, bbox[1] + 39],
        ],
        dtype=np.float32,
    )
    return [{"bbox": bbox, "kps": kps, "det_score": 0.87 + (w % 17) / 100.0}]
