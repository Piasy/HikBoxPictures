from __future__ import annotations

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
