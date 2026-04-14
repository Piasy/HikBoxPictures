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


def _bbox_highlight_bounds(image: Image.Image) -> tuple[int, int, int, int] | None:
    rgb = image.convert("RGB")
    pixels = rgb.load()
    min_x = rgb.width
    min_y = rgb.height
    max_x = -1
    max_y = -1
    for y in range(rgb.height):
        for x in range(rgb.width):
            r, g, b = pixels[x, y]
            if r >= 180 and g <= 130 and b <= 130:
                min_x = min(min_x, x)
                min_y = min(min_y, y)
                max_x = max(max_x, x)
                max_y = max(max_y, y)
    if max_x < 0 or max_y < 0:
        return None
    return (min_x, min_y, max_x, max_y)


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

        context_img = Image.open(io.BytesIO(context_resp.content)).convert("RGB")
        original_img = Image.open(io.BytesIO(original_resp.content)).convert("RGB")
        assert context_img.width > 0
        assert context_img.height > 0
        assert max(context_img.size) <= 320
        assert original_img.width > 0
        assert original_img.height > 0
        assert _has_bbox_highlight(context_img) is True
    finally:
        ws.close()


def test_context_endpoint_keeps_meaningful_scene_around_face(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_media_assets=True)
    try:
        assert ws.media_photo_id is not None
        assert ws.media_observation_id is not None

        photo_row = ws.conn.execute(
            "SELECT primary_path FROM photo_asset WHERE id = ?",
            (int(ws.media_photo_id),),
        ).fetchone()
        assert photo_row is not None
        original_path = Path(str(photo_row["primary_path"]))
        image = Image.new("RGB", (400, 500), color=(232, 238, 244))
        for y in range(60, 240):
            for x in range(40, 170):
                image.putpixel((x, y), (255, 214, 221))
        for y in range(300, 470):
            for x in range(250, 390):
                image.putpixel((x, y), (213, 236, 215))
        for y in range(160, 340):
            for x in range(170, 230):
                image.putpixel((x, y), (188, 150, 132))
        image.save(original_path, format="JPEG")

        ws.conn.execute(
            """
            UPDATE face_observation
            SET bbox_top = 0.32,
                bbox_right = 0.575,
                bbox_bottom = 0.68,
                bbox_left = 0.425
            WHERE id = ?
            """,
            (int(ws.media_observation_id),),
        )
        ws.conn.commit()

        artifact_path = ws.root / ".hikbox" / "artifacts" / "context" / f"obs-{int(ws.media_observation_id)}.jpg"
        if artifact_path.exists():
            artifact_path.unlink()

        client = TestClient(create_app(workspace=ws.root))
        response = client.get(f"/api/observations/{ws.media_observation_id}/context")

        assert response.status_code == 200
        context_img = Image.open(io.BytesIO(response.content)).convert("RGB")
        bounds = _bbox_highlight_bounds(context_img)
        assert bounds is not None

        min_x, min_y, max_x, max_y = bounds
        bbox_width = max_x - min_x + 1
        bbox_height = max_y - min_y + 1
        assert bbox_width > 0
        assert bbox_height > 0

        # context 应该明显大于红框本身，而不是退化成“带框 crop”。
        assert context_img.width / bbox_width >= 2.5
        assert context_img.height / bbox_height >= 2.5
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
