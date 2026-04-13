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


def test_missing_original_returns_structured_error(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_media_assets=True)
    try:
        assert ws.media_photo_id is not None
        ws.break_original_for_photo(int(ws.media_photo_id))

        client = TestClient(create_app(workspace=ws.root))
        response = client.get(f"/api/photos/{ws.media_photo_id}/original")

        assert response.status_code == 404
        payload = response.json()
        assert payload["error_code"] == "preview.asset.missing"
        assert "message" in payload
    finally:
        ws.close()


def test_decode_failed_emits_ops_event(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_media_assets=True)
    try:
        assert ws.media_photo_id is not None
        ws.inject_broken_image_for_photo(int(ws.media_photo_id))

        client = TestClient(create_app(workspace=ws.root))
        response = client.get(f"/api/photos/{ws.media_photo_id}/preview")

        assert response.status_code == 422
        payload = response.json()
        assert payload["error_code"] == "preview.asset.decode_failed"
        assert ws.count_ops_event("preview.asset.decode_failed") >= 1
    finally:
        ws.close()


def test_rebuild_failed_returns_structured_error_and_event(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_media_assets=True)
    try:
        assert ws.media_observation_id is not None
        assert ws.media_photo_id is not None
        ws.break_crop_for_observation(int(ws.media_observation_id))
        ws.inject_broken_image_for_photo(int(ws.media_photo_id))

        client = TestClient(create_app(workspace=ws.root))
        response = client.get(f"/api/observations/{ws.media_observation_id}/crop")

        assert response.status_code == 422
        payload = response.json()
        assert payload["error_code"] == "preview.context.rebuild_failed"
        assert ws.count_ops_event("preview.context.rebuild_failed") >= 1
    finally:
        ws.close()
