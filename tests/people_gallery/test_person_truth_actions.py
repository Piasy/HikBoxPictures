from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from hikbox_pictures.ann import AnnIndexStore
from hikbox_pictures.api.app import create_app
from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.deepface_engine import embedding_to_blob
from hikbox_pictures.repositories.person_repo import PersonRepo
from hikbox_pictures.services.person_truth_service import PersonTruthService
from hikbox_pictures.services.prototype_service import PrototypeService
from hikbox_pictures.workspace import load_workspace_paths

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_seed_workspace = _MODULE.build_seed_workspace
build_seed_workspace_with_mock_embeddings = _MODULE.build_seed_workspace_with_mock_embeddings


def test_people_merge_action_marks_source_as_merged(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        client = TestClient(create_app(workspace=ws.root))

        response = client.post("/api/people/2/actions/merge", json={"target_person_id": 1})

        assert response.status_code == 200
        source = ws.get_person_row(2)
        assert source is not None
        assert source["status"] == "merged"
        assert source["merged_into_person_id"] == 1
    finally:
        ws.close()


def test_people_merge_action_returns_422_for_already_merged_source(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        client = TestClient(create_app(workspace=ws.root))

        first = client.post("/api/people/2/actions/merge", json={"target_person_id": 1})
        assert first.status_code == 200

        second = client.post("/api/people/2/actions/merge", json={"target_person_id": 3})
        assert second.status_code == 422
    finally:
        ws.close()


def test_people_lock_assignment_prevents_auto_reassignment(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        assignment_id = ws.create_assignment(person_id=1, locked=False, assignment_source="manual")
        client = TestClient(create_app(workspace=ws.root))

        lock_response = client.post(
            "/api/people/1/actions/lock-assignment",
            json={"assignment_id": assignment_id},
        )
        assert lock_response.status_code == 200

        changed = PersonTruthService(ws.conn).try_auto_reassign(
            assignment_id=assignment_id,
            candidate_person_id=2,
        )
        assert changed is False
        assignment = ws.get_assignment(assignment_id)
        assert assignment is not None
        assert assignment["person_id"] == 1
        assert int(assignment["locked"]) == 1
    finally:
        ws.close()


def test_people_lock_assignment_returns_422_when_assignment_belongs_to_other_person(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        assignment_id = ws.create_assignment(person_id=2, locked=False, assignment_source="manual")
        client = TestClient(create_app(workspace=ws.root))

        response = client.post(
            "/api/people/1/actions/lock-assignment",
            json={"assignment_id": assignment_id},
        )

        assert response.status_code == 422
    finally:
        ws.close()


def test_try_auto_reassign_changed_zero_closes_transaction(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        assignment_id = ws.create_assignment(person_id=1, locked=False, assignment_source="manual")
        ws.conn.execute(
            """
            UPDATE person_face_assignment
            SET active = 0
            WHERE id = ?
            """,
            (assignment_id,),
        )
        ws.conn.commit()
        assert ws.conn.in_transaction is False

        changed = PersonTruthService(ws.conn).try_auto_reassign(
            assignment_id=assignment_id,
            candidate_person_id=2,
        )

        assert changed is False
        assert ws.conn.in_transaction is False
    finally:
        ws.close()


def test_people_split_action_moves_assignment_to_new_person(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        assignment_id = ws.create_assignment(person_id=1, locked=False, assignment_source="manual")
        client = TestClient(create_app(workspace=ws.root))

        response = client.post(
            "/api/people/1/actions/split",
            json={
                "assignment_id": assignment_id,
                "new_person_display_name": "人物A-拆分",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert int(body["person_id"]) == 1
        assert int(body["assignment_id"]) == assignment_id
        new_person_id = int(body["new_person_id"])
        assert new_person_id > 0

        assignment = ws.get_assignment(assignment_id)
        assert assignment is not None
        assert int(assignment["person_id"]) == new_person_id
        assert assignment["assignment_source"] == "split"
    finally:
        ws.close()


def test_people_exclude_assignment_deactivates_history_and_syncs_person_artifacts(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    seeded = build_seed_workspace_with_mock_embeddings(workspace)
    paths = load_workspace_paths(workspace)
    conn = connect_db(paths.db_path)
    try:
        person_id = int(seeded["person_ids_by_name"]["人物甲"])
        assignment_rows = conn.execute(
            """
            SELECT id, face_observation_id
            FROM person_face_assignment
            WHERE person_id = ?
              AND active = 1
            ORDER BY id ASC
            """,
            (person_id,),
        ).fetchall()
        assert len(assignment_rows) == 2
        excluded_assignment_id = int(assignment_rows[0]["id"])
        excluded_observation_id = int(assignment_rows[0]["face_observation_id"])
        remaining_observation_id = int(assignment_rows[1]["face_observation_id"])
        remaining_embedding_blob = conn.execute(
            """
            SELECT vector_blob
            FROM face_embedding
            WHERE face_observation_id = ?
              AND feature_type = 'face'
            """,
            (remaining_observation_id,),
        ).fetchone()["vector_blob"]
        remaining_embedding = np.frombuffer(remaining_embedding_blob, dtype=np.float32).copy()

        ann_path = paths.artifacts_dir / "ann" / "prototype_index.npz"
        prototype_service = PrototypeService(conn, PersonRepo(conn), AnnIndexStore(ann_path))
        prototype_service.rebuild_all_person_prototypes(model_key="pipeline-stub-v1")
        prototype_service.rebuild_ann_index_from_active_prototypes(model_key="pipeline-stub-v1")
        conn.commit()

        client = TestClient(create_app(workspace=workspace))
        response = client.post(
            f"/api/people/{person_id}/actions/exclude-assignment",
            json={"assignment_id": excluded_assignment_id},
        )

        assert response.status_code == 200
        body = response.json()
        assert int(body["assignment_id"]) == excluded_assignment_id
        assert int(body["person_id"]) == person_id
        assert int(body["face_observation_id"]) == excluded_observation_id
        assert int(body["remaining_sample_count"]) == 1

        assignment = conn.execute(
            """
            SELECT active
            FROM person_face_assignment
            WHERE id = ?
            """,
            (excluded_assignment_id,),
        ).fetchone()
        assert assignment is not None
        assert int(assignment["active"]) == 0

        exclusion = conn.execute(
            """
            SELECT active
            FROM person_face_exclusion
            WHERE person_id = ?
              AND face_observation_id = ?
            """,
            (person_id, excluded_observation_id),
        ).fetchone()
        assert exclusion is not None
        assert int(exclusion["active"]) == 1

        review = conn.execute(
            """
            SELECT id, review_type, status
            FROM review_item
            WHERE face_observation_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (excluded_observation_id,),
        ).fetchone()
        assert review is not None
        assert int(review["id"]) == int(body["review_id"])
        assert review["review_type"] == "new_person"
        assert review["status"] == "open"

        prototype_row = conn.execute(
            """
            SELECT vector_blob, quality_score
            FROM person_prototype
            WHERE person_id = ?
              AND prototype_type = 'centroid'
              AND model_key = 'pipeline-stub-v1'
              AND active = 1
            ORDER BY id DESC
            LIMIT 1
            """,
            (person_id,),
        ).fetchone()
        assert prototype_row is not None
        synced_vector = np.frombuffer(prototype_row["vector_blob"], dtype=np.float32).copy()
        expected_vector = remaining_embedding / np.linalg.norm(remaining_embedding)
        assert np.allclose(synced_vector, expected_vector)
        assert float(prototype_row["quality_score"]) == 1.0

        ann_store = AnnIndexStore(ann_path)
        recalled = ann_store.search(expected_vector, 2)
        assert recalled
        assert int(recalled[0][0]) == person_id
    finally:
        conn.close()
