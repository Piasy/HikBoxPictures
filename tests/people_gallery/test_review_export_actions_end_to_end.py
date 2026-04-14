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


def test_review_and_export_actions_roundtrip(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_export_assets=True)
    try:
        client = TestClient(create_app(workspace=ws.root))

        reviews = client.get("/api/reviews")
        assert reviews.status_code == 200
        review_id = int(reviews.json()[0]["id"])

        resolve_resp = client.post(f"/api/reviews/{review_id}/actions/resolve")
        assert resolve_resp.status_code == 200
        assert resolve_resp.json()["status"] == "resolved"

        run_resp = client.post(f"/api/export/templates/{ws.export_template_id}/actions/run")
        assert run_resp.status_code == 200
        run_id = int(run_resp.json()["run_id"])

        runs_resp = client.get(f"/api/export/templates/{ws.export_template_id}/runs")
        assert runs_resp.status_code == 200
        assert any(int(item["id"]) == run_id for item in runs_resp.json())
    finally:
        ws.close()


def test_web_pages_expose_review_export_action_entries(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_export_assets=True)
    try:
        client = TestClient(create_app(workspace=ws.root))

        reviews_html = client.get("/reviews").text
        assert 'data-action="review-create-person"' in reviews_html
        assert 'data-action="review-assign-person"' in reviews_html
        assert 'data-action="review-resolve"' in reviews_html
        assert 'data-action="review-dismiss"' in reviews_html
        assert 'data-action="review-ignore"' in reviews_html

        exports_html = client.get("/exports").text
        assert 'data-action="export-run"' in exports_html
        assert 'data-action="export-show-runs"' in exports_html
    finally:
        ws.close()
