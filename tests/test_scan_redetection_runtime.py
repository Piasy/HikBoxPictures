from __future__ import annotations

from pathlib import Path
import sqlite3

import numpy as np
from PIL import Image
import pytest

from hikbox_pictures.product import scan as scan_module
from hikbox_pictures.product.sources import load_workspace_context
from hikbox_pictures.product.sources import add_source
from hikbox_pictures.product.workspace_init import initialize_workspace


def _unit_vector(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    vector = rng.normal(size=512).astype(np.float32)
    norm = float(np.linalg.norm(vector))
    if norm > 1e-9:
        vector = vector / norm
    return vector


def _write_detection_artifacts(root: Path, prefix: str, count: int) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    for index in range(count):
        crop_path = root / f"{prefix}_crop_{index}.jpg"
        context_path = root / f"{prefix}_context_{index}.jpg"
        Image.new("RGB", (64, 64), color=(120, 100, 80)).save(crop_path)
        Image.new("RGB", (128, 96), color=(80, 100, 120)).save(context_path)
        artifacts.append(
            {
                "crop_path": str(crop_path),
                "context_path": str(context_path),
            }
        )
    return artifacts


def _insert_session_batch_item(
    *,
    workspace_context,
    absolute_path: Path,
    batch_index: int,
) -> tuple[int, list[dict[str, object]]]:
    connection = sqlite3.connect(workspace_context.library_db_path)
    try:
        with connection:
            session_cursor = connection.execute(
                """
                INSERT INTO scan_sessions (
                  plan_fingerprint,
                  batch_size,
                  status,
                  command,
                  total_batches,
                  started_at
                )
                VALUES (?, 1, 'running', 'hikbox-pictures scan start --workspace test', 1, '2026-04-25T00:00:00Z')
                """,
                (f"plan-{batch_index}",),
            )
            session_id = int(session_cursor.lastrowid)
            batch_cursor = connection.execute(
                """
                INSERT INTO scan_batches (session_id, batch_index, status, item_count)
                VALUES (?, ?, 'running', 1)
                """,
                (session_id, batch_index),
            )
            batch_id = int(batch_cursor.lastrowid)
            source_id = int(
                connection.execute("SELECT id FROM library_sources ORDER BY id ASC LIMIT 1").fetchone()[0]
            )
            item_cursor = connection.execute(
                """
                INSERT INTO scan_batch_items (
                  batch_id,
                  item_index,
                  source_id,
                  absolute_path,
                  status
                )
                VALUES (?, 1, ?, ?, 'pending')
                """,
                (batch_id, source_id, str(absolute_path.resolve())),
            )
            item_id = int(item_cursor.lastrowid)
            return session_id, [
                {
                    "scan_batch_item_id": item_id,
                    "item_index": 1,
                    "source_id": source_id,
                    "absolute_path": str(absolute_path.resolve()),
                    "file_name": absolute_path.name,
                    "file_extension": absolute_path.suffix.lower().lstrip("."),
                    "capture_month": "2025-01",
                    "file_fingerprint": f"fingerprint-{batch_index}",
                    "live_photo_mov_path": None,
                }
            ]
    finally:
        connection.close()


def _fetch_assignment_rows(db_path: Path) -> list[tuple[int, str, int]]:
    connection = sqlite3.connect(db_path)
    try:
        return [
            (int(row[0]), str(row[1]), int(row[2]))
            for row in connection.execute(
                """
                SELECT face_observation_id, person_id, active
                FROM person_face_assignments
                ORDER BY face_observation_id ASC
                """
            ).fetchall()
        ]
    finally:
        connection.close()


def _fetch_face_rows(db_path: Path) -> list[tuple[int, int]]:
    connection = sqlite3.connect(db_path)
    try:
        return [
            (int(row[0]), int(row[1]))
            for row in connection.execute(
                """
                SELECT id, face_index
                FROM face_observations
                ORDER BY id ASC
                """
            ).fetchall()
        ]
    finally:
        connection.close()


def _fetch_embedding_blob(db_path: Path, *, face_observation_id: int) -> bytes:
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            """
            SELECT vector_blob
            FROM face_embeddings
            WHERE face_observation_id = ? AND variant = 'main'
            """,
            (face_observation_id,),
        ).fetchone()
        assert row is not None
        return bytes(row[0])
    finally:
        connection.close()


def _fetch_embedding_row(db_path: Path, *, face_observation_id: int) -> tuple[int, bytes] | None:
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            """
            SELECT dimension, vector_blob
            FROM face_embeddings
            WHERE face_observation_id = ? AND variant = 'main'
            """,
            (face_observation_id,),
        ).fetchone()
        if row is None:
            return None
        return int(row[0]), bytes(row[1])
    finally:
        connection.close()


def _fetch_face_artifact_paths(db_path: Path, *, face_observation_id: int) -> tuple[Path, Path]:
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            """
            SELECT crop_path, context_path
            FROM face_observations
            WHERE id = ?
            """,
            (face_observation_id,),
        ).fetchone()
        assert row is not None
        return Path(str(row[0])), Path(str(row[1]))
    finally:
        connection.close()


def _count_rows_matching(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> int:
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(sql, params).fetchone()
        assert row is not None
        return int(row[0])
    finally:
        connection.close()


def _fetch_one(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> tuple[object, ...]:
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(sql, params).fetchone()
        assert row is not None
        return tuple(row)
    finally:
        connection.close()


def test_commit_batch_results_reuses_existing_face_assignment_on_redetect(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    image_path = source_dir / "asset.jpg"
    Image.new("RGB", (640, 480), color=(160, 140, 120)).save(image_path)

    initialize_workspace(workspace=workspace, external_root=external_root, command_args=["init"])
    add_source(workspace=workspace, source_path=source_dir, command_args=["source", "add"])
    workspace_context = load_workspace_context(workspace)

    session_id, candidates = _insert_session_batch_item(
        workspace_context=workspace_context,
        absolute_path=image_path,
        batch_index=1,
    )
    first_artifacts = _write_detection_artifacts(tmp_path, "first", 2)
    first_worker_result = {
        "items": [
            {
                "absolute_path": str(image_path.resolve()),
                "status": "succeeded",
                "image_width": 640,
                "image_height": 480,
                "detections": [
                    {
                        "bbox": [20.0, 20.0, 120.0, 180.0],
                        "score": 0.99,
                        "embedding": _unit_vector(1).tolist(),
                    },
                    {
                        "bbox": [220.0, 25.0, 320.0, 185.0],
                        "score": 0.98,
                        "embedding": _unit_vector(2).tolist(),
                    },
                ],
                "artifacts": first_artifacts,
            }
        ]
    }

    scan_module._commit_batch_results(
        workspace_context=workspace_context,
        batch_id=1,
        batch_index=1,
        session_id=session_id,
        candidates=candidates,
        worker_result=first_worker_result,
    )

    first_face_rows = _fetch_face_rows(workspace_context.library_db_path)
    assert len(first_face_rows) == 2
    reused_face_id = first_face_rows[0][0]
    removed_face_id = first_face_rows[1][0]
    reused_embedding_before = _fetch_embedding_blob(
        workspace_context.embedding_db_path,
        face_observation_id=reused_face_id,
    )
    connection = sqlite3.connect(workspace_context.library_db_path)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO person (id, display_name, is_named, status, created_at, updated_at)
                VALUES ('person-a', NULL, 0, 'active', '2026-04-25T00:00:00Z', '2026-04-25T00:00:00Z')
                """
            )
            connection.execute(
                """
                INSERT INTO assignment_runs (
                  scan_session_id,
                  algorithm_version,
                  status,
                  param_snapshot_json,
                  started_at,
                  completed_at,
                  updated_at
                )
                VALUES (?, 'immich_v6_online_v1', 'completed', '{}', '2026-04-25T00:00:00Z', '2026-04-25T00:00:00Z', '2026-04-25T00:00:00Z')
                """,
                (session_id,),
            )
            assignment_run_id = int(connection.execute("SELECT id FROM assignment_runs ORDER BY id DESC LIMIT 1").fetchone()[0])
            connection.execute(
                """
                INSERT INTO person_face_assignments (
                  person_id,
                  face_observation_id,
                  assignment_run_id,
                  assignment_source,
                  active,
                  evidence_json,
                  created_at,
                  updated_at
                )
                VALUES ('person-a', ?, ?, 'online_v6', 1, '{}', '2026-04-25T00:00:00Z', '2026-04-25T00:00:00Z')
                """,
                (reused_face_id, assignment_run_id),
            )
    finally:
        connection.close()

    second_session_id, second_candidates = _insert_session_batch_item(
        workspace_context=workspace_context,
        absolute_path=image_path,
        batch_index=2,
    )
    second_artifacts = _write_detection_artifacts(tmp_path, "second", 2)
    second_worker_result = {
        "items": [
            {
                "absolute_path": str(image_path.resolve()),
                "status": "succeeded",
                "image_width": 640,
                "image_height": 480,
                "detections": [
                    {
                        "bbox": [21.0, 21.0, 121.0, 181.0],
                        "score": 0.97,
                        "embedding": _unit_vector(101).tolist(),
                    },
                    {
                        "bbox": [380.0, 30.0, 460.0, 170.0],
                        "score": 0.96,
                        "embedding": _unit_vector(102).tolist(),
                    },
                ],
                "artifacts": second_artifacts,
            }
        ]
    }

    scan_module._commit_batch_results(
        workspace_context=workspace_context,
        batch_id=2,
        batch_index=2,
        session_id=second_session_id,
        candidates=second_candidates,
        worker_result=second_worker_result,
    )

    final_face_rows = _fetch_face_rows(workspace_context.library_db_path)
    final_face_ids = {row[0] for row in final_face_rows}
    assert reused_face_id in final_face_ids
    assert removed_face_id not in final_face_ids
    assert len(final_face_rows) == 2
    assert _fetch_embedding_blob(
        workspace_context.embedding_db_path,
        face_observation_id=reused_face_id,
    ) == reused_embedding_before
    assignment_rows = _fetch_assignment_rows(workspace_context.library_db_path)
    assert assignment_rows == [(reused_face_id, "person-a", 1)]
    new_face_id = next(face_id for face_id in final_face_ids if face_id != reused_face_id)
    connection = sqlite3.connect(workspace_context.library_db_path)
    try:
        active_assignment = connection.execute(
            """
            SELECT COUNT(*)
            FROM person_face_assignments
            WHERE face_observation_id = ? AND active = 1
            """,
            (new_face_id,),
        ).fetchone()
        assert active_assignment is not None
        assert int(active_assignment[0]) == 0
    finally:
        connection.close()


def test_commit_batch_results_failed_redetect_cleans_existing_faces_assignments_and_artifacts(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-failed-redetect"
    external_root = tmp_path / "external-root-failed-redetect"
    source_dir = tmp_path / "source-failed-redetect"
    source_dir.mkdir()
    image_path = source_dir / "asset.jpg"
    Image.new("RGB", (640, 480), color=(150, 120, 90)).save(image_path)

    initialize_workspace(workspace=workspace, external_root=external_root, command_args=["init"])
    add_source(workspace=workspace, source_path=source_dir, command_args=["source", "add"])
    workspace_context = load_workspace_context(workspace)

    session_id, candidates = _insert_session_batch_item(
        workspace_context=workspace_context,
        absolute_path=image_path,
        batch_index=1,
    )
    first_artifacts = _write_detection_artifacts(tmp_path, "failed-first", 1)
    first_worker_result = {
        "items": [
            {
                "absolute_path": str(image_path.resolve()),
                "status": "succeeded",
                "image_width": 640,
                "image_height": 480,
                "detections": [
                    {
                        "bbox": [24.0, 24.0, 124.0, 184.0],
                        "score": 0.99,
                        "embedding": _unit_vector(10).tolist(),
                    }
                ],
                "artifacts": first_artifacts,
            }
        ]
    }

    scan_module._commit_batch_results(
        workspace_context=workspace_context,
        batch_id=1,
        batch_index=1,
        session_id=session_id,
        candidates=candidates,
        worker_result=first_worker_result,
    )

    existing_face_id = _fetch_face_rows(workspace_context.library_db_path)[0][0]
    old_crop_path, old_context_path = _fetch_face_artifact_paths(
        workspace_context.library_db_path,
        face_observation_id=existing_face_id,
    )
    connection = sqlite3.connect(workspace_context.library_db_path)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO person (id, display_name, is_named, status, created_at, updated_at)
                VALUES ('person-stale', NULL, 0, 'active', '2026-04-25T00:00:00Z', '2026-04-25T00:00:00Z')
                """
            )
            connection.execute(
                """
                INSERT INTO assignment_runs (
                  scan_session_id,
                  algorithm_version,
                  status,
                  param_snapshot_json,
                  started_at,
                  completed_at,
                  updated_at
                )
                VALUES (?, 'immich_v6_online_v1', 'completed', '{}', '2026-04-25T00:00:00Z', '2026-04-25T00:00:00Z', '2026-04-25T00:00:00Z')
                """,
                (session_id,),
            )
            assignment_run_id = int(connection.execute("SELECT id FROM assignment_runs ORDER BY id DESC LIMIT 1").fetchone()[0])
            connection.execute(
                """
                INSERT INTO person_face_assignments (
                  person_id,
                  face_observation_id,
                  assignment_run_id,
                  assignment_source,
                  active,
                  evidence_json,
                  created_at,
                  updated_at
                )
                VALUES ('person-stale', ?, ?, 'online_v6', 1, '{}', '2026-04-25T00:00:00Z', '2026-04-25T00:00:00Z')
                """,
                (existing_face_id, assignment_run_id),
            )
    finally:
        connection.close()

    second_session_id, second_candidates = _insert_session_batch_item(
        workspace_context=workspace_context,
        absolute_path=image_path,
        batch_index=2,
    )
    failed_worker_result = {
        "items": [
            {
                "absolute_path": str(image_path.resolve()),
                "status": "failed",
                "failure_reason": "测试注入：重检失败",
                "image_width": 640,
                "image_height": 480,
                "face_count": 0,
                "detections": [],
                "artifacts": [],
            }
        ]
    }

    scan_module._commit_batch_results(
        workspace_context=workspace_context,
        batch_id=2,
        batch_index=2,
        session_id=second_session_id,
        candidates=second_candidates,
        worker_result=failed_worker_result,
    )

    assert _count_rows_matching(
        workspace_context.library_db_path,
        "SELECT COUNT(*) FROM face_observations WHERE asset_id = 1",
    ) == 0
    assert _count_rows_matching(
        workspace_context.embedding_db_path,
        "SELECT COUNT(*) FROM face_embeddings",
    ) == 0
    assert _count_rows_matching(
        workspace_context.library_db_path,
        "SELECT COUNT(*) FROM person_face_assignments",
    ) == 0
    assert _count_rows_matching(
        workspace_context.library_db_path,
        "SELECT COUNT(*) FROM person WHERE status = 'active'",
    ) == 0
    asset_status = _fetch_one(
        workspace_context.library_db_path,
        "SELECT processing_status, failure_reason FROM assets WHERE id = 1",
    )
    assert asset_status == ("failed", "测试注入：重检失败")
    batch_item_status = _fetch_one(
        workspace_context.library_db_path,
        "SELECT status, failure_reason FROM scan_batch_items WHERE batch_id = 2",
    )
    assert batch_item_status == ("failed", "测试注入：重检失败")
    assert not old_crop_path.exists()
    assert not old_context_path.exists()


def test_commit_batch_results_redetect_invalidates_old_face_even_when_old_embedding_is_dirty(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-dirty-old-embedding"
    external_root = tmp_path / "external-root-dirty-old-embedding"
    source_dir = tmp_path / "source-dirty-old-embedding"
    source_dir.mkdir()
    image_path = source_dir / "asset.jpg"
    Image.new("RGB", (640, 480), color=(120, 130, 140)).save(image_path)

    initialize_workspace(workspace=workspace, external_root=external_root, command_args=["init"])
    add_source(workspace=workspace, source_path=source_dir, command_args=["source", "add"])
    workspace_context = load_workspace_context(workspace)

    session_id, candidates = _insert_session_batch_item(
        workspace_context=workspace_context,
        absolute_path=image_path,
        batch_index=1,
    )
    first_artifacts = _write_detection_artifacts(tmp_path, "dirty-first", 2)
    first_worker_result = {
        "items": [
            {
                "absolute_path": str(image_path.resolve()),
                "status": "succeeded",
                "image_width": 640,
                "image_height": 480,
                "detections": [
                    {
                        "bbox": [20.0, 20.0, 120.0, 180.0],
                        "score": 0.99,
                        "embedding": _unit_vector(20).tolist(),
                    },
                    {
                        "bbox": [220.0, 30.0, 320.0, 180.0],
                        "score": 0.98,
                        "embedding": _unit_vector(21).tolist(),
                    },
                ],
                "artifacts": first_artifacts,
            }
        ]
    }

    scan_module._commit_batch_results(
        workspace_context=workspace_context,
        batch_id=1,
        batch_index=1,
        session_id=session_id,
        candidates=candidates,
        worker_result=first_worker_result,
    )

    first_face_rows = _fetch_face_rows(workspace_context.library_db_path)
    reusable_face_id = first_face_rows[0][0]
    dirty_face_id = first_face_rows[1][0]
    dirty_crop_path, dirty_context_path = _fetch_face_artifact_paths(
        workspace_context.library_db_path,
        face_observation_id=dirty_face_id,
    )
    connection = sqlite3.connect(workspace_context.library_db_path)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO person (id, display_name, is_named, status, created_at, updated_at)
                VALUES ('person-dirty', NULL, 0, 'active', '2026-04-25T00:00:00Z', '2026-04-25T00:00:00Z')
                """
            )
            connection.execute(
                """
                INSERT INTO assignment_runs (
                  scan_session_id,
                  algorithm_version,
                  status,
                  param_snapshot_json,
                  started_at,
                  completed_at,
                  updated_at
                )
                VALUES (?, 'immich_v6_online_v1', 'completed', '{}', '2026-04-25T00:00:00Z', '2026-04-25T00:00:00Z', '2026-04-25T00:00:00Z')
                """,
                (session_id,),
            )
            assignment_run_id = int(connection.execute("SELECT id FROM assignment_runs ORDER BY id DESC LIMIT 1").fetchone()[0])
            connection.execute(
                """
                INSERT INTO person_face_assignments (
                  person_id,
                  face_observation_id,
                  assignment_run_id,
                  assignment_source,
                  active,
                  evidence_json,
                  created_at,
                  updated_at
                )
                VALUES ('person-dirty', ?, ?, 'online_v6', 1, '{}', '2026-04-25T00:00:00Z', '2026-04-25T00:00:00Z')
                """,
                (dirty_face_id, assignment_run_id),
            )
    finally:
        connection.close()
    embedding_connection = sqlite3.connect(workspace_context.embedding_db_path)
    try:
        with embedding_connection:
            embedding_connection.execute(
                """
                UPDATE face_embeddings
                SET dimension = 128,
                    vector_blob = ?
                WHERE face_observation_id = ? AND variant = 'main'
                """,
                (_unit_vector(22)[:128].astype(np.float32).tobytes(), dirty_face_id),
            )
    finally:
        embedding_connection.close()

    second_session_id, second_candidates = _insert_session_batch_item(
        workspace_context=workspace_context,
        absolute_path=image_path,
        batch_index=2,
    )
    second_worker_result = {
        "items": [
            {
                "absolute_path": str(image_path.resolve()),
                "status": "succeeded",
                "image_width": 640,
                "image_height": 480,
                "detections": [
                    {
                        "bbox": [21.0, 21.0, 121.0, 181.0],
                        "score": 0.97,
                        "embedding": _unit_vector(120).tolist(),
                    }
                ],
                "artifacts": _write_detection_artifacts(tmp_path, "dirty-second", 1),
            }
        ]
    }

    scan_module._commit_batch_results(
        workspace_context=workspace_context,
        batch_id=2,
        batch_index=2,
        session_id=second_session_id,
        candidates=second_candidates,
        worker_result=second_worker_result,
    )

    final_face_ids = {row[0] for row in _fetch_face_rows(workspace_context.library_db_path)}
    assert reusable_face_id in final_face_ids
    assert dirty_face_id not in final_face_ids
    assert _count_rows_matching(
        workspace_context.embedding_db_path,
        "SELECT COUNT(*) FROM face_embeddings WHERE face_observation_id = ?",
        (dirty_face_id,),
    ) == 0
    assert _count_rows_matching(
        workspace_context.library_db_path,
        "SELECT COUNT(*) FROM person_face_assignments WHERE face_observation_id = ?",
        (dirty_face_id,),
    ) == 0
    assert _count_rows_matching(
        workspace_context.library_db_path,
        "SELECT COUNT(*) FROM person WHERE id = 'person-dirty'",
    ) == 0
    assert not dirty_crop_path.exists()
    assert not dirty_context_path.exists()


@pytest.mark.parametrize(
    ("dirty_dimension", "dirty_blob"),
    [
        (128, _unit_vector(31)[:128].astype(np.float32).tobytes()),
        (512, b"\x00"),
    ],
    ids=["wrong-dimension", "undecodable-blob"],
)
def test_commit_batch_results_reuses_dirty_iou_matched_face_and_refreshes_artifacts_without_refreshing_embedding(
    tmp_path: Path,
    dirty_dimension: int,
    dirty_blob: bytes,
) -> None:
    workspace = tmp_path / "workspace-dirty-matched"
    external_root = tmp_path / "external-root-dirty-matched"
    source_dir = tmp_path / "source-dirty-matched"
    source_dir.mkdir()
    image_path = source_dir / "asset.jpg"
    Image.new("RGB", (640, 480), color=(140, 125, 110)).save(image_path)

    initialize_workspace(workspace=workspace, external_root=external_root, command_args=["init"])
    add_source(workspace=workspace, source_path=source_dir, command_args=["source", "add"])
    workspace_context = load_workspace_context(workspace)

    session_id, candidates = _insert_session_batch_item(
        workspace_context=workspace_context,
        absolute_path=image_path,
        batch_index=1,
    )
    first_worker_result = {
        "items": [
            {
                "absolute_path": str(image_path.resolve()),
                "status": "succeeded",
                "image_width": 640,
                "image_height": 480,
                "detections": [
                    {
                        "bbox": [40.0, 40.0, 140.0, 200.0],
                        "score": 0.99,
                        "embedding": _unit_vector(30).tolist(),
                    }
                ],
                "artifacts": _write_detection_artifacts(tmp_path, "dirty-match-first", 1),
            }
        ]
    }

    scan_module._commit_batch_results(
        workspace_context=workspace_context,
        batch_id=1,
        batch_index=1,
        session_id=session_id,
        candidates=candidates,
        worker_result=first_worker_result,
    )

    original_face_id = _fetch_face_rows(workspace_context.library_db_path)[0][0]
    original_crop_path, original_context_path = _fetch_face_artifact_paths(
        workspace_context.library_db_path,
        face_observation_id=original_face_id,
    )
    connection = sqlite3.connect(workspace_context.library_db_path)
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO person (id, display_name, is_named, status, created_at, updated_at)
                VALUES ('person-keep', NULL, 0, 'active', '2026-04-25T00:00:00Z', '2026-04-25T00:00:00Z')
                """
            )
            connection.execute(
                """
                INSERT INTO assignment_runs (
                  scan_session_id,
                  algorithm_version,
                  status,
                  param_snapshot_json,
                  started_at,
                  completed_at,
                  updated_at
                )
                VALUES (?, 'immich_v6_online_v1', 'completed', '{}', '2026-04-25T00:00:00Z', '2026-04-25T00:00:00Z', '2026-04-25T00:00:00Z')
                """,
                (session_id,),
            )
            assignment_run_id = int(connection.execute("SELECT id FROM assignment_runs ORDER BY id DESC LIMIT 1").fetchone()[0])
            connection.execute(
                """
                INSERT INTO person_face_assignments (
                  person_id,
                  face_observation_id,
                  assignment_run_id,
                  assignment_source,
                  active,
                  evidence_json,
                  created_at,
                  updated_at
                )
                VALUES ('person-keep', ?, ?, 'online_v6', 1, '{}', '2026-04-25T00:00:00Z', '2026-04-25T00:00:00Z')
                """,
                (original_face_id, assignment_run_id),
            )
    finally:
        connection.close()

    embedding_connection = sqlite3.connect(workspace_context.embedding_db_path)
    try:
        with embedding_connection:
            embedding_connection.execute(
                """
                UPDATE face_embeddings
                SET dimension = ?,
                    vector_blob = ?
                WHERE face_observation_id = ? AND variant = 'main'
                """,
                (dirty_dimension, dirty_blob, original_face_id),
            )
    finally:
        embedding_connection.close()

    second_session_id, second_candidates = _insert_session_batch_item(
        workspace_context=workspace_context,
        absolute_path=image_path,
        batch_index=2,
    )
    second_worker_result = {
        "items": [
            {
                "absolute_path": str(image_path.resolve()),
                "status": "succeeded",
                "image_width": 640,
                "image_height": 480,
                "detections": [
                    {
                        "bbox": [41.0, 41.0, 141.0, 201.0],
                        "score": 0.96,
                        "embedding": _unit_vector(130).tolist(),
                    }
                ],
                "artifacts": _write_detection_artifacts(tmp_path, "dirty-match-second", 1),
            }
        ]
    }

    scan_module._commit_batch_results(
        workspace_context=workspace_context,
        batch_id=2,
        batch_index=2,
        session_id=second_session_id,
        candidates=second_candidates,
        worker_result=second_worker_result,
    )

    final_face_rows = _fetch_face_rows(workspace_context.library_db_path)
    assert final_face_rows == [(original_face_id, 0)]
    assert _fetch_assignment_rows(workspace_context.library_db_path) == [(original_face_id, "person-keep", 1)]
    assert _count_rows_matching(
        workspace_context.library_db_path,
        "SELECT COUNT(*) FROM person WHERE id = 'person-keep'",
    ) == 1
    assert _fetch_embedding_row(
        workspace_context.embedding_db_path,
        face_observation_id=original_face_id,
    ) == (dirty_dimension, dirty_blob)
    final_crop_path, final_context_path = _fetch_face_artifact_paths(
        workspace_context.library_db_path,
        face_observation_id=original_face_id,
    )
    assert final_crop_path != original_crop_path
    assert final_context_path != original_context_path
    assert final_crop_path.is_file()
    assert final_context_path.is_file()
    assert not original_crop_path.exists()
    assert not original_context_path.exists()
    assert _fetch_one(
        workspace_context.library_db_path,
        """
        SELECT bbox_x1, bbox_y1, bbox_x2, bbox_y2, image_width, image_height, score
        FROM face_observations
        WHERE id = ?
        """,
        (original_face_id,),
    ) == (41.0, 41.0, 141.0, 201.0, 640, 480, 0.96)
