from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from fastapi.testclient import TestClient

from hikbox_pictures.api.app import create_app
from hikbox_pictures.services.person_truth_service import PersonTruthService

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_seed_workspace = _MODULE.build_seed_workspace


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
