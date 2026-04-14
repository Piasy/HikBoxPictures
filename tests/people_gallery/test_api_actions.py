from __future__ import annotations

import json
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from fastapi.testclient import TestClient

from hikbox_pictures.api.app import create_app

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_seed_workspace = _MODULE.build_seed_workspace


def _append_new_person_review(ws, *, file_name: str) -> tuple[int, int]:
    source_id = int(ws.source_repo.list_sources(active=True)[0]["id"])
    asset_id = ws.asset_repo.add_photo_asset(
        source_id,
        str((ws.root / file_name).resolve()),
        processing_status="assignment_done",
    )
    observation_id = ws.asset_repo.ensure_face_observation(asset_id)
    review_id = ws.review_repo.create_review_item(
        "new_person",
        payload_json=json.dumps(
            {
                "face_observation_id": int(observation_id),
                "candidates": [],
                "model_key": "test-model",
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        priority=15,
        face_observation_id=int(observation_id),
    )
    ws.conn.commit()
    return int(review_id), int(observation_id)


def test_people_rename_action_persists_to_db(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        client = TestClient(create_app(workspace=ws.root))

        response = client.post("/api/people/1/actions/rename", json={"display_name": "  爸爸  "})

        assert response.status_code == 200
        assert ws.person_display_name(1) == "爸爸"

        people = client.get("/api/people").json()
        person = next((row for row in people if row["id"] == 1), None)
        assert person is not None
        assert person["display_name"] == "爸爸"
    finally:
        ws.close()


def test_people_rename_action_returns_404_when_missing_person(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        client = TestClient(create_app(workspace=ws.root))

        response = client.post("/api/people/999/actions/rename", json={"display_name": "爸爸"})

        assert response.status_code == 404
    finally:
        ws.close()


def test_people_rename_action_rejects_blank_display_name(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        client = TestClient(create_app(workspace=ws.root))

        response = client.post("/api/people/1/actions/rename", json={"display_name": "   "})

        assert response.status_code == 422
    finally:
        ws.close()


def test_review_resolve_and_ignore_actions_update_review_status(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        client = TestClient(create_app(workspace=ws.root))

        resolve_resp = client.post("/api/reviews/1/actions/resolve")
        assert resolve_resp.status_code == 200
        assert resolve_resp.json()["status"] == "resolved"

        ignore_resp = client.post("/api/reviews/2/actions/ignore")
        assert ignore_resp.status_code == 200
        assert ignore_resp.json()["status"] == "dismissed"

        review1 = ws.get_review_item(1)
        review2 = ws.get_review_item(2)
        assert review1 is not None and review1["status"] == "resolved"
        assert review2 is not None and review2["status"] == "dismissed"
    finally:
        ws.close()


def test_new_person_create_person_action_creates_confirmed_person_and_batch_assignments(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        review_a, observation_a = _append_new_person_review(ws, file_name="review-create-a.jpg")
        review_b, observation_b = _append_new_person_review(ws, file_name="review-create-b.jpg")
        client = TestClient(create_app(workspace=ws.root))

        response = client.post(
            f"/api/reviews/{review_a}/actions/create-person",
            json={
                "review_ids": [review_a, review_b],
                "display_name": "新人物甲",
            },
        )

        assert response.status_code == 200
        body = response.json()
        new_person_id = int(body["person_id"])
        assert body["status"] == "resolved"
        assert body["updated_count"] == 2
        assert body["assigned_observation_count"] == 2
        assert body["display_name"] == "新人物甲"

        person = ws.person_repo.get_person(new_person_id)
        assert person is not None
        assert person["display_name"] == "新人物甲"
        assert bool(person["confirmed"]) is True

        assignment_a = ws.asset_repo.get_active_assignment_for_observation(observation_a)
        assignment_b = ws.asset_repo.get_active_assignment_for_observation(observation_b)
        assert assignment_a is not None
        assert assignment_b is not None
        assert int(assignment_a["person_id"]) == new_person_id
        assert int(assignment_b["person_id"]) == new_person_id
        assert assignment_a["assignment_source"] == "manual"
        assert assignment_b["assignment_source"] == "manual"
        assert int(assignment_a["locked"]) == 1
        assert int(assignment_b["locked"]) == 1

        review_row_a = ws.get_review_item(review_a)
        review_row_b = ws.get_review_item(review_b)
        assert review_row_a is not None and review_row_a["status"] == "resolved"
        assert review_row_b is not None and review_row_b["status"] == "resolved"
    finally:
        ws.close()


def test_new_person_assign_person_action_assigns_existing_person_and_resolves_review(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        review_id, observation_id = _append_new_person_review(ws, file_name="review-assign-existing.jpg")
        client = TestClient(create_app(workspace=ws.root))

        response = client.post(
            f"/api/reviews/{review_id}/actions/assign-person",
            json={"person_id": 1},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "resolved"
        assert int(body["person_id"]) == 1
        assert body["display_name"] == "人物A"
        assert body["assigned_observation_count"] == 1

        assignment = ws.asset_repo.get_active_assignment_for_observation(observation_id)
        assert assignment is not None
        assert int(assignment["person_id"]) == 1
        assert assignment["assignment_source"] == "manual"
        assert int(assignment["locked"]) == 1

        review_row = ws.get_review_item(review_id)
        assert review_row is not None
        assert review_row["status"] == "resolved"
    finally:
        ws.close()


def test_export_run_and_runs_api_roundtrip(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_export_assets=True)
    try:
        client = TestClient(create_app(workspace=ws.root))

        run_resp = client.post(f"/api/export/templates/{ws.export_template_id}/actions/run")
        assert run_resp.status_code == 200
        run_payload = run_resp.json()
        assert int(run_payload["template_id"]) == int(ws.export_template_id)
        run_id = int(run_payload["run_id"])

        list_resp = client.get(f"/api/export/templates/{ws.export_template_id}/runs")
        assert list_resp.status_code == 200
        runs = list_resp.json()
        assert any(int(row["id"]) == run_id for row in runs)
    finally:
        ws.close()


def test_export_template_create_update_delete_api_roundtrip(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        client = TestClient(create_app(workspace=ws.root))

        create_resp = client.post(
            "/api/export/templates",
            json={
                "name": "双人精选",
                "output_root": str(ws.paths.exports_dir / "duo"),
                "person_ids": [1, 2],
                "include_group": False,
                "export_live_mov": False,
                "start_datetime": "2026-04-01T00:00:00+08:00",
                "end_datetime": "2026-04-30T23:59:59+08:00",
                "enabled": True,
            },
        )

        assert create_resp.status_code == 200
        created = create_resp.json()
        template_id = int(created["id"])
        assert created["name"] == "双人精选"
        assert created["person_ids"] == [1, 2]
        assert created["include_group"] is False

        created_row = ws.export_repo.get_template(template_id)
        assert created_row is not None
        assert str(created_row["output_root"]) == str(ws.paths.exports_dir / "duo")
        assert ws.export_repo.list_template_person_ids(template_id) == [1, 2]

        update_resp = client.put(
            f"/api/export/templates/{template_id}",
            json={
                "name": "人物B 单人",
                "output_root": str(ws.paths.exports_dir / "solo-b"),
                "person_ids": [2],
                "include_group": True,
                "export_live_mov": True,
                "start_datetime": "",
                "end_datetime": "",
                "enabled": False,
            },
        )

        assert update_resp.status_code == 200
        updated = update_resp.json()
        assert updated["name"] == "人物B 单人"
        assert updated["person_ids"] == [2]
        assert updated["enabled"] is False

        updated_row = ws.export_repo.get_template(template_id)
        assert updated_row is not None
        assert str(updated_row["output_root"]) == str(ws.paths.exports_dir / "solo-b")
        assert int(updated_row["export_live_mov"]) == 1
        assert int(updated_row["enabled"]) == 0
        assert ws.export_repo.list_template_person_ids(template_id) == [2]

        delete_resp = client.delete(f"/api/export/templates/{template_id}")
        assert delete_resp.status_code == 200
        assert delete_resp.json()["status"] == "deleted"
        assert ws.export_repo.get_template(template_id) is None
    finally:
        ws.close()
