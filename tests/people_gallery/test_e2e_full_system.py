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


def test_full_system_webui_flow_keeps_review_queue_available(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_export_assets=True, seed_media_assets=True)
    try:
        assert ws.media_photo_id is not None
        client = TestClient(create_app(workspace=ws.root))
        baseline_reviews = client.get("/api/reviews")
        assert baseline_reviews.status_code == 200
        baseline_ids = {int(item["id"]) for item in baseline_reviews.json()}
        assert baseline_ids

        rename_resp = client.post("/api/people/1/actions/rename", json={"display_name": "爸爸"})
        assert rename_resp.status_code == 200

        people_html = client.get("/").text
        assert "爸爸" in people_html

        detail_html = client.get("/people/1").text
        reviews_html = client.get("/reviews").text
        exports_html = client.get("/exports").text
        assert 'data-viewer-layer="original"' in detail_html
        assert 'data-action="viewer-next"' in reviews_html
        assert "export-preview-sample" in exports_html

        ws.inject_broken_image_for_photo(int(ws.media_photo_id))
        broken_preview = client.get(f"/api/photos/{ws.media_photo_id}/preview")
        assert broken_preview.status_code == 422

        reviews_after_error = client.get("/reviews")
        assert reviews_after_error.status_code == 200
        html = reviews_after_error.text
        assert "queue-block" in html
        for review_id in sorted(baseline_ids):
            assert f"review #{review_id}" in html
    finally:
        ws.close()
