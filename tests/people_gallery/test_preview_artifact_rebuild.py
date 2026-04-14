from __future__ import annotations

from io import BytesIO
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from hikbox_pictures.api.app import create_app

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_seed_workspace = _MODULE.build_seed_workspace


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


def test_missing_crop_is_rebuilt_on_demand(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_media_assets=True)
    try:
        assert ws.media_observation_id is not None
        ws.break_crop_for_observation(int(ws.media_observation_id))
        assert ws.crop_exists(int(ws.media_observation_id)) is False

        client = TestClient(create_app(workspace=ws.root))
        response = client.get(f"/api/observations/{ws.media_observation_id}/crop")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/")
        assert ws.crop_exists(int(ws.media_observation_id)) is True
        assert ws.count_ops_event("preview.context.rebuild_requested") >= 1
    finally:
        ws.close()


def test_legacy_tight_context_artifact_is_rebuilt_for_webui(tmp_path) -> None:
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
        source = Image.new("RGB", (400, 500), color=(232, 238, 244))
        draw = ImageDraw.Draw(source)
        draw.rectangle((40, 60, 170, 240), fill=(255, 214, 221))
        draw.rectangle((250, 300, 390, 470), fill=(213, 236, 215))
        draw.rectangle((170, 160, 230, 340), fill=(188, 150, 132))
        source.save(original_path, format="JPEG")
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

        context_path = ws.root / ".hikbox" / "artifacts" / "context" / f"obs-{int(ws.media_observation_id)}.jpg"
        context_path.parent.mkdir(parents=True, exist_ok=True)

        legacy = Image.new("RGB", (220, 260), color=(220, 225, 230))
        draw = ImageDraw.Draw(legacy)
        draw.rectangle((28, 30, 190, 228), fill=(188, 150, 132))
        draw.rectangle((28, 30, 190, 228), outline=(255, 64, 64), width=4)
        legacy.save(context_path, format="JPEG")

        client = TestClient(create_app(workspace=ws.root))
        response = client.get(f"/api/observations/{ws.media_observation_id}/context")

        assert response.status_code == 200
        rebuilt = Image.open(BytesIO(response.content)).convert("RGB")
        bounds = _bbox_highlight_bounds(rebuilt)
        assert bounds is not None
        min_x, min_y, max_x, max_y = bounds
        bbox_width = max_x - min_x + 1
        bbox_height = max_y - min_y + 1
        assert bbox_width > 0
        assert bbox_height > 0
        assert (rebuilt.width * rebuilt.height) / (bbox_width * bbox_height) >= 2.5
        assert ws.count_ops_event("preview.context.rebuild_requested") >= 1
    finally:
        ws.close()


def test_legacy_tiny_context_artifact_is_rebuilt_for_webui(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_media_assets=True)
    try:
        assert ws.media_observation_id is not None
        context_path = ws.root / ".hikbox" / "artifacts" / "context" / f"obs-{int(ws.media_observation_id)}.jpg"
        context_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (32, 24), color=(180, 140, 110)).save(context_path, format="JPEG")

        client = TestClient(create_app(workspace=ws.root))
        response = client.get(f"/api/observations/{ws.media_observation_id}/context")

        assert response.status_code == 200
        rebuilt = Image.open(BytesIO(response.content))
        assert max(rebuilt.size) > 48
        assert ws.count_ops_event("preview.context.rebuild_requested") >= 1
    finally:
        ws.close()
