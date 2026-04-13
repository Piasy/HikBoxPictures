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


def test_media_endpoints_return_images_from_workspace_data(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_media_assets=True)
    try:
        client = TestClient(create_app(workspace=ws.root))
        assert ws.media_photo_id is not None
        assert ws.media_observation_id is not None

        original = client.get(f"/api/photos/{ws.media_photo_id}/original")
        preview = client.get(f"/api/photos/{ws.media_photo_id}/preview")
        crop = client.get(f"/api/observations/{ws.media_observation_id}/crop")
        context = client.get(f"/api/observations/{ws.media_observation_id}/context")

        assert original.status_code == 200
        assert preview.status_code == 200
        assert crop.status_code == 200
        assert context.status_code == 200

        assert original.headers["content-type"].startswith("image/")
        assert preview.headers["content-type"].startswith("image/")
        assert crop.headers["content-type"].startswith("image/")
        assert context.headers["content-type"].startswith("image/")

        assert len(original.content) > 0
        assert len(crop.content) > 0
    finally:
        ws.close()


def test_media_endpoint_returns_404_when_photo_missing(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_media_assets=True)
    try:
        client = TestClient(create_app(workspace=ws.root))
        response = client.get("/api/photos/999999/original")
        assert response.status_code == 404
    finally:
        ws.close()


def test_media_crop_auto_rebuilds_when_crop_missing(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_media_assets=True)
    try:
        assert ws.media_observation_id is not None
        ws.conn.execute(
            "UPDATE face_observation SET crop_path = NULL WHERE id = ?",
            (int(ws.media_observation_id),),
        )
        ws.conn.commit()
        client = TestClient(create_app(workspace=ws.root))
        response = client.get(f"/api/observations/{ws.media_observation_id}/crop")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/")
    finally:
        ws.close()
