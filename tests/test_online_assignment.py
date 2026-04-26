from __future__ import annotations

from pathlib import Path
import sqlite3

import numpy as np
import pytest

import hikbox_pictures.product.online_assignment as online_assignment_module
from hikbox_pictures.product.online_assignment import AssignmentFace
from hikbox_pictures.product.online_assignment import AssignmentParams
from hikbox_pictures.product.online_assignment import ExistingAssetFace
from hikbox_pictures.product.online_assignment import OnlineAssignmentEngine
from hikbox_pictures.product.online_assignment import OnlineAssignmentError
from hikbox_pictures.product.online_assignment import RedetectFace
from hikbox_pictures.product.online_assignment import reconcile_asset_redetection
from hikbox_pictures.product.online_assignment import run_online_assignment
from hikbox_pictures.product.sources import add_source
from hikbox_pictures.product.sources import load_workspace_context
from hikbox_pictures.product.workspace_init import initialize_workspace


def _unit_vector(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    vector = rng.normal(size=512).astype(np.float32)
    norm = float(np.linalg.norm(vector))
    if norm > 1e-9:
        vector = vector / norm
    return vector


def _near_vector(base: np.ndarray, noise_seed: int, *, weight: float) -> np.ndarray:
    noise = _unit_vector(noise_seed)
    mixed = ((1.0 - weight) * base) + (weight * noise)
    norm = float(np.linalg.norm(mixed))
    if norm > 1e-9:
        mixed = mixed / norm
    return mixed.astype(np.float32)


def _initialize_assignment_workspace(tmp_path: Path) -> tuple[object, int, int]:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    initialize_workspace(workspace=workspace, external_root=external_root, command_args=["init"])
    add_source(workspace=workspace, source_path=source_dir, label="fixture", command_args=["source", "add"])
    workspace_context = load_workspace_context(workspace)
    connection = sqlite3.connect(workspace_context.library_db_path)
    try:
        with connection:
            source_id = int(connection.execute("SELECT id FROM library_sources ORDER BY id ASC LIMIT 1").fetchone()[0])
            cursor = connection.execute(
                """
                INSERT INTO scan_sessions (
                  plan_fingerprint,
                  batch_size,
                  status,
                  command,
                  total_batches,
                  started_at
                )
                VALUES ('assignment-test-plan', 1, 'running', 'hikbox-pictures scan start --workspace test', 1, '2026-04-25T00:00:00Z')
                """
            )
            scan_session_id = int(cursor.lastrowid)
    finally:
        connection.close()
    return workspace_context, scan_session_id, source_id


def _insert_face_record(
    *,
    workspace_context,
    source_id: int,
    file_name: str,
    face_index: int,
    embedding: np.ndarray | None,
    person_id: str | None = None,
) -> int:
    connection = sqlite3.connect(workspace_context.library_db_path)
    try:
        connection.execute("ATTACH DATABASE ? AS embedding", (str(workspace_context.embedding_db_path),))
        with connection:
            asset_cursor = connection.execute(
                """
                INSERT INTO assets (
                  source_id,
                  absolute_path,
                  file_name,
                  file_extension,
                  capture_month,
                  file_fingerprint,
                  live_photo_mov_path,
                  processing_status,
                  failure_reason,
                  created_at,
                  updated_at
                )
                VALUES (?, ?, ?, 'jpg', '2025-01', ?, NULL, 'succeeded', NULL, '2026-04-25T00:00:00Z', '2026-04-25T00:00:00Z')
                """,
                (
                    source_id,
                    str((workspace_context.workspace_path / file_name).resolve()),
                    file_name,
                    f"fingerprint-{file_name}",
                ),
            )
            asset_id = int(asset_cursor.lastrowid)
            face_cursor = connection.execute(
                """
                INSERT INTO face_observations (
                  asset_id,
                  face_index,
                  bbox_x1,
                  bbox_y1,
                  bbox_x2,
                  bbox_y2,
                  image_width,
                  image_height,
                  score,
                  crop_path,
                  context_path,
                  created_at
                )
                VALUES (?, ?, 10.0, 10.0, 110.0, 150.0, 320, 240, 0.99, ?, ?, '2026-04-25T00:00:00Z')
                """,
                (
                    asset_id,
                    face_index,
                    str((workspace_context.external_root_path / "artifacts" / "crops" / f"{file_name}_{face_index}.jpg").resolve()),
                    str((workspace_context.external_root_path / "artifacts" / "context" / f"{file_name}_{face_index}.jpg").resolve()),
                ),
            )
            face_observation_id = int(face_cursor.lastrowid)
            if embedding is not None:
                connection.execute(
                    """
                    INSERT INTO embedding.face_embeddings (
                      face_observation_id,
                      variant,
                      dimension,
                      l2_norm,
                      vector_blob,
                      created_at
                    )
                    VALUES (?, 'main', 512, 1.0, ?, '2026-04-25T00:00:00Z')
                    """,
                    (face_observation_id, embedding.astype(np.float32).tobytes()),
                )
            if person_id is not None:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO person (id, display_name, is_named, status, created_at, updated_at)
                    VALUES (?, NULL, 0, 'active', '2026-04-25T00:00:00Z', '2026-04-25T00:00:00Z')
                    """,
                    (person_id,),
                )
        return face_observation_id
    finally:
        connection.close()


def _insert_active_assignment(
    *,
    workspace_context,
    scan_session_id: int,
    face_observation_id: int,
    person_id: str,
) -> None:
    connection = sqlite3.connect(workspace_context.library_db_path)
    try:
        with connection:
            cursor = connection.execute(
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
                (scan_session_id,),
            )
            assignment_run_id = int(cursor.lastrowid)
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
                VALUES (?, ?, ?, 'online_v6', 1, '{}', '2026-04-25T00:00:00Z', '2026-04-25T00:00:00Z')
                """,
                (person_id, face_observation_id, assignment_run_id),
            )
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


def _count_rows(db_path: Path, table_name: str) -> int:
    return int(_fetch_one(db_path, f"SELECT COUNT(*) FROM {table_name}")[0])


def test_online_assignment_creates_person_only_when_self_plus_two_neighbors_reach_threshold() -> None:
    base = _unit_vector(100)
    faces = [
        AssignmentFace(
            face_id="face-1",
            sort_key=("source", "a.jpg", 0),
            embedding=_near_vector(base, 101, weight=0.01),
            person_id=None,
            candidate=True,
        ),
        AssignmentFace(
            face_id="face-2",
            sort_key=("source", "b.jpg", 0),
            embedding=_near_vector(base, 102, weight=0.01),
            person_id=None,
            candidate=True,
        ),
        AssignmentFace(
            face_id="face-3",
            sort_key=("source", "c.jpg", 0),
            embedding=_near_vector(base, 103, weight=0.01),
            person_id=None,
            candidate=True,
        ),
    ]

    result = OnlineAssignmentEngine(params=AssignmentParams(max_distance=0.05, min_faces=3)).run(faces)

    assert result.new_person_count == 1
    assert result.assigned_count == 3
    assert result.deferred_count == 0
    assert result.skipped_count == 0
    assert {decision.person_id for decision in result.decisions} == {result.decisions[0].person_id}


def test_online_assignment_second_pass_does_not_create_person_for_self_plus_one_neighbor() -> None:
    base = _unit_vector(200)
    faces = [
        AssignmentFace(
            face_id="face-1",
            sort_key=("source", "a.jpg", 0),
            embedding=_near_vector(base, 201, weight=0.01),
            person_id=None,
            candidate=True,
        ),
        AssignmentFace(
            face_id="face-2",
            sort_key=("source", "b.jpg", 0),
            embedding=_near_vector(base, 202, weight=0.01),
            person_id=None,
            candidate=True,
        ),
    ]

    result = OnlineAssignmentEngine(params=AssignmentParams(max_distance=0.05, min_faces=3)).run(faces)

    assert result.new_person_count == 0
    assert result.assigned_count == 0
    assert result.deferred_count == 2
    assert result.skipped_count == 2
    assert [decision.status for decision in result.decisions] == ["skipped", "skipped"]


def test_online_assignment_second_pass_attaches_deferred_face_to_existing_person() -> None:
    base = _unit_vector(250)
    faces = [
        AssignmentFace(
            face_id="already-assigned",
            sort_key=("source", "a.jpg", 0),
            embedding=_near_vector(base, 251, weight=0.01),
            person_id="person-existing",
            candidate=False,
        ),
        AssignmentFace(
            face_id="candidate",
            sort_key=("source", "b.jpg", 0),
            embedding=_near_vector(base, 252, weight=0.012),
            person_id=None,
            candidate=True,
        ),
    ]

    result = OnlineAssignmentEngine(params=AssignmentParams(max_distance=0.05, min_faces=3)).run(faces)

    assert result.new_person_count == 0
    assert result.assigned_count == 1
    assert result.deferred_count == 1
    assert result.skipped_count == 0
    assert result.decisions[0].status == "assigned"
    assert result.decisions[0].person_id == "person-existing"


def test_online_assignment_prefers_nearest_existing_person_instead_of_voting() -> None:
    base = _unit_vector(300)
    near_person = "person-near"
    far_person = "person-far"
    faces = [
        AssignmentFace(
            face_id="near-assigned",
            sort_key=("source", "near.jpg", 0),
            embedding=_near_vector(base, 301, weight=0.01),
            person_id=near_person,
            candidate=False,
        ),
        AssignmentFace(
            face_id="far-assigned",
            sort_key=("source", "far.jpg", 0),
            embedding=_near_vector(base, 302, weight=0.02),
            person_id=far_person,
            candidate=False,
        ),
        AssignmentFace(
            face_id="candidate",
            sort_key=("source", "candidate.jpg", 0),
            embedding=_near_vector(base, 303, weight=0.011),
            person_id=None,
            candidate=True,
        ),
    ]

    result = OnlineAssignmentEngine(params=AssignmentParams(max_distance=0.05, min_faces=3)).run(faces)

    assert result.new_person_count == 0
    assert result.assigned_count == 1
    assert result.decisions[0].person_id == near_person


def test_reconcile_asset_redetection_reuses_face_by_iou_and_invalidates_unmatched_faces() -> None:
    original_embedding = _unit_vector(400)
    changed_embedding = _unit_vector(401)
    newcomer_embedding = _unit_vector(402)
    existing_faces = [
        ExistingAssetFace(
            face_id="face-a",
            bbox=(20.0, 20.0, 120.0, 180.0),
            image_width=640,
            image_height=480,
            person_id="person-a",
            embedding=original_embedding,
        ),
        ExistingAssetFace(
            face_id="face-b",
            bbox=(220.0, 25.0, 320.0, 185.0),
            image_width=640,
            image_height=480,
            person_id=None,
            embedding=_unit_vector(403),
        ),
    ]
    redetected_faces = [
        RedetectFace(
            bbox=(21.0, 21.0, 121.0, 181.0),
            image_width=640,
            image_height=480,
            embedding=changed_embedding,
        ),
        RedetectFace(
            bbox=(380.0, 30.0, 460.0, 170.0),
            image_width=640,
            image_height=480,
            embedding=newcomer_embedding,
        ),
    ]

    result = reconcile_asset_redetection(existing_faces=existing_faces, redetected_faces=redetected_faces)

    assert result.reused_face_ids == ["face-a"]
    assert result.invalidated_face_ids == ["face-b"]
    assert len(result.pending_faces) == 1
    assert result.pending_faces[0].reused_face_id is None
    assert np.allclose(result.reused_embeddings["face-a"], original_embedding)
    assert not np.allclose(result.reused_embeddings["face-a"], changed_embedding)
    assert np.allclose(result.pending_faces[0].embedding, newcomer_embedding)


def test_reconcile_asset_redetection_reuses_dirty_matched_face_without_reused_embedding() -> None:
    existing_faces = [
        ExistingAssetFace(
            face_id="face-dirty",
            bbox=(20.0, 20.0, 120.0, 180.0),
            image_width=640,
            image_height=480,
            person_id="person-keep",
            embedding=None,
        )
    ]
    redetected_faces = [
        RedetectFace(
            bbox=(21.0, 21.0, 121.0, 181.0),
            image_width=640,
            image_height=480,
            embedding=_unit_vector(450),
        )
    ]

    result = reconcile_asset_redetection(existing_faces=existing_faces, redetected_faces=redetected_faces)

    assert result.reused_face_ids == ["face-dirty"]
    assert result.invalidated_face_ids == []
    assert result.pending_faces == []
    assert result.reused_face_id_by_detection_index == {0: "face-dirty"}
    assert result.reused_embeddings == {}


def test_run_online_assignment_marks_run_failed_when_log_write_fails(tmp_path: Path) -> None:
    workspace_context, scan_session_id, _source_id = _initialize_assignment_workspace(tmp_path)

    def _always_fail_log(_payload: dict[str, object]) -> None:
        raise OSError("测试注入：assignment 日志写失败")

    with pytest.raises(OnlineAssignmentError, match="测试注入：assignment 日志写失败"):
        run_online_assignment(
            workspace_context=workspace_context,
            scan_session_id=scan_session_id,
            append_log=_always_fail_log,
        )

    failed_run = _fetch_one(
        workspace_context.library_db_path,
        """
        SELECT status, failure_reason
        FROM assignment_runs
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    assert failed_run[0] == "failed"
    assert "测试注入：assignment 日志写失败" in str(failed_run[1])
    assert _count_rows(workspace_context.library_db_path, "person_face_assignments") == 0


def test_run_online_assignment_rolls_back_partial_insert_when_active_assignment_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_context, scan_session_id, source_id = _initialize_assignment_workspace(tmp_path)
    base = _unit_vector(500)
    face_one = _insert_face_record(
        workspace_context=workspace_context,
        source_id=source_id,
        file_name="candidate-1.jpg",
        face_index=0,
        embedding=None,
        person_id="person-existing",
    )
    face_two = _insert_face_record(
        workspace_context=workspace_context,
        source_id=source_id,
        file_name="candidate-2.jpg",
        face_index=0,
        embedding=None,
    )
    face_three = _insert_face_record(
        workspace_context=workspace_context,
        source_id=source_id,
        file_name="candidate-3.jpg",
        face_index=0,
        embedding=None,
    )
    _insert_active_assignment(
        workspace_context=workspace_context,
        scan_session_id=scan_session_id,
        face_observation_id=face_one,
        person_id="person-existing",
    )

    faces = [
        AssignmentFace(
            face_id=str(face_one),
            sort_key=("source", "candidate-1.jpg", 0),
            embedding=_near_vector(base, 501, weight=0.01),
            person_id=None,
            candidate=True,
        ),
        AssignmentFace(
            face_id=str(face_two),
            sort_key=("source", "candidate-2.jpg", 0),
            embedding=_near_vector(base, 502, weight=0.01),
            person_id=None,
            candidate=True,
        ),
        AssignmentFace(
            face_id=str(face_three),
            sort_key=("source", "candidate-3.jpg", 0),
            embedding=_near_vector(base, 503, weight=0.01),
            person_id=None,
            candidate=True,
        ),
    ]
    monkeypatch.setattr(
        online_assignment_module,
        "_load_assignment_faces",
        lambda **_kwargs: (faces, []),
    )

    with pytest.raises(OnlineAssignmentError, match="active assignment 写入冲突"):
        run_online_assignment(
            workspace_context=workspace_context,
            scan_session_id=scan_session_id,
            append_log=lambda _payload: None,
        )

    assert _fetch_one(
        workspace_context.library_db_path,
        """
        SELECT status, failure_reason
        FROM assignment_runs
        ORDER BY id DESC
        LIMIT 1
        """,
    )[0] == "failed"
    assert _count_rows(workspace_context.library_db_path, "person") == 1
    assignment_rows = sqlite3.connect(workspace_context.library_db_path).execute(
        """
        SELECT face_observation_id, person_id, active
        FROM person_face_assignments
        ORDER BY id ASC
        """
    ).fetchall()
    assert assignment_rows == [(face_one, "person-existing", 1)]
