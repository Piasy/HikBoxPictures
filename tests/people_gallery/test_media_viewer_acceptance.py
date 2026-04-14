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


def test_viewer_semantics_cover_person_reviews_exports_pages(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_export_assets=True)
    try:
        client = TestClient(create_app(workspace=ws.root))

        person_html = client.get("/people/1").text
        review_html = client.get("/reviews").text
        export_html = client.get("/exports").text

        for html in (person_html, review_html, export_html):
            assert 'data-viewer-layer="crop"' in html
            assert 'data-viewer-layer="context"' in html
            assert 'data-viewer-layer="original"' in html
            assert 'data-action="viewer-prev"' in html
            assert 'data-action="viewer-next"' in html
            assert 'data-action="viewer-toggle-bbox"' not in html

        assert "export-preview-tile" in export_html
    finally:
        ws.close()


def test_single_preview_failure_does_not_block_review_queue(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_media_assets=True)
    try:
        assert ws.media_photo_id is not None

        client = TestClient(create_app(workspace=ws.root))
        baseline_reviews = client.get("/api/reviews")
        assert baseline_reviews.status_code == 200
        baseline_ids = {int(item["id"]) for item in baseline_reviews.json()}
        assert baseline_ids

        ws.inject_broken_image_for_photo(int(ws.media_photo_id))

        preview = client.get(f"/api/photos/{ws.media_photo_id}/preview")
        assert preview.status_code == 422
        payload = preview.json()
        assert payload["error_code"] == "preview.asset.decode_failed"

        reviews_api = client.get("/api/reviews")
        assert reviews_api.status_code == 200
        after_ids = {int(item["id"]) for item in reviews_api.json()}
        assert after_ids == baseline_ids

        reviews_page = client.get("/reviews")
        assert reviews_page.status_code == 200
        html = reviews_page.text
        assert "待审核" in html
        assert "queue-block" in html
        for review_id in sorted(baseline_ids):
            assert f"review #{review_id}" in html
    finally:
        ws.close()
