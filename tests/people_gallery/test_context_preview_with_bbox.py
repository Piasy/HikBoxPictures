from __future__ import annotations

import io
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from hikbox_pictures.api.app import create_app

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_seed_workspace = _MODULE.build_seed_workspace


def _has_bbox_highlight(image: Image.Image) -> bool:
    rgb = image.convert("RGB")
    highlighted = 0
    pixels = rgb.load()
    for y in range(rgb.height):
        for x in range(rgb.width):
            r, g, b = pixels[x, y]
            if r >= 180 and g <= 130 and b <= 130:
                highlighted += 1
                if highlighted >= 12:
                    return True
    return False


def test_context_endpoint_returns_bbox_highlighted_region(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_media_assets=True)
    try:
        assert ws.media_observation_id is not None
        assert ws.media_photo_id is not None
        client = TestClient(create_app(workspace=ws.root))

        context_resp = client.get(f"/api/observations/{ws.media_observation_id}/context")
        original_resp = client.get(f"/api/photos/{ws.media_photo_id}/original")

        assert context_resp.status_code == 200
        assert original_resp.status_code == 200
        assert context_resp.headers["content-type"].startswith("image/")
        assert len(context_resp.content) < len(original_resp.content)

        context_img = Image.open(io.BytesIO(context_resp.content)).convert("RGB")
        original_img = Image.open(io.BytesIO(original_resp.content)).convert("RGB")
        assert context_img.width < original_img.width
        assert context_img.height < original_img.height
        assert _has_bbox_highlight(context_img) is True
    finally:
        ws.close()


def test_context_artifact_is_written_under_workspace(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_media_assets=True)
    try:
        assert ws.media_observation_id is not None
        client = TestClient(create_app(workspace=ws.root))

        response = client.get(f"/api/observations/{ws.media_observation_id}/context")
        assert response.status_code == 200

        artifact_path = ws.root / ".hikbox" / "artifacts" / "context" / f"obs-{int(ws.media_observation_id)}.jpg"
        assert artifact_path.exists() is True
        assert artifact_path.is_file() is True
    finally:
        ws.close()
