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


def test_web_people_page_reflects_api_rename_action(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        client = TestClient(create_app(workspace=ws.root))

        rename_resp = client.post("/api/people/1/actions/rename", json={"display_name": "爸爸"})
        assert rename_resp.status_code == 200

        html = client.get("/").text
        assert "person-card" in html
        assert "爸爸" in html
    finally:
        ws.close()


def test_web_viewer_actions_visible_in_people_reviews_exports(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_export_assets=True)
    try:
        client = TestClient(create_app(workspace=ws.root))
        person_html = client.get("/people/1").text
        review_html = client.get("/reviews").text
        export_html = client.get("/exports").text

        for html in (person_html, review_html, export_html):
            assert 'data-action="viewer-prev"' in html
            assert 'data-action="viewer-next"' in html
            assert 'data-action="viewer-toggle-bbox"' not in html

        assert 'data-action="person-exclude-assignment"' in person_html
        assert 'data-action="person-exclude-selected-assignments"' in person_html
        assert 'data-action="person-exclude-assignment"' not in review_html
        assert 'data-action="person-exclude-selected-assignments"' not in review_html
        assert 'data-action="person-exclude-assignment"' not in export_html
        assert 'data-action="person-exclude-selected-assignments"' not in export_html
    finally:
        ws.close()


def test_web_pages_append_version_to_static_assets(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_export_assets=True)
    try:
        client = TestClient(create_app(workspace=ws.root))
        html = client.get("/people/1").text

        assert '/static/style.css?v=' in html
        assert '/static/app.js?v=' in html
    finally:
        ws.close()
