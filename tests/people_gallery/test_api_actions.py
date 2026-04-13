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
