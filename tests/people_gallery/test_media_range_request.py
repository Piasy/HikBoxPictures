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


def test_original_endpoint_supports_range_requests(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_media_assets=True)
    try:
        client = TestClient(create_app(workspace=ws.root))
        assert ws.media_photo_id is not None

        response = client.get(
            f"/api/photos/{ws.media_photo_id}/original",
            headers={"Range": "bytes=0-15"},
        )

        assert response.status_code == 206
        assert response.headers.get("accept-ranges") == "bytes"
        assert response.headers.get("content-range", "").startswith("bytes 0-15/")
        assert len(response.content) == 16
    finally:
        ws.close()


def test_original_endpoint_invalid_range_returns_416_with_content_range(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_media_assets=True)
    try:
        client = TestClient(create_app(workspace=ws.root))
        assert ws.media_photo_id is not None

        response = client.get(
            f"/api/photos/{ws.media_photo_id}/original",
            headers={"Range": "bytes=999999-1000000"},
        )

        assert response.status_code == 416
        assert response.headers.get("content-range", "").startswith("bytes */")
        assert response.json()["detail"] == "无效的 Range 请求"
    finally:
        ws.close()
