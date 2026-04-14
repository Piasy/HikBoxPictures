from __future__ import annotations

from io import BytesIO
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageOps

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


def _mean_rgb(image: Image.Image) -> tuple[int, int, int]:
    sample = image.resize((1, 1), resample=Image.Resampling.BILINEAR).convert("RGB")
    return tuple(int(channel) for channel in sample.getpixel((0, 0)))


def _create_exif_oriented_observation(ws) -> tuple[int, tuple[int, int], tuple[int, int, int]]:
    source_row = ws.source_repo.list_sources(active=True)[0]
    source_id = int(source_row["id"])
    source_root = ws.root / "oriented-source"
    photo_path = source_root / "exif-rotated-face.jpg"
    photo_path.parent.mkdir(parents=True, exist_ok=True)
    ws.conn.execute(
        "UPDATE library_source SET root_path = ? WHERE id = ?",
        (str(source_root), source_id),
    )

    source = Image.new("RGB", (240, 180), color=(0, 0, 0))
    draw = ImageDraw.Draw(source)
    draw.rectangle((0, 0, 119, 89), fill=(230, 48, 40))
    draw.rectangle((120, 0, 239, 89), fill=(42, 166, 70))
    draw.rectangle((0, 90, 119, 179), fill=(48, 94, 214))
    draw.rectangle((120, 90, 239, 179), fill=(236, 212, 64))
    exif = Image.Exif()
    exif[274] = 6
    source.save(photo_path, format="JPEG", quality=100, exif=exif)

    with Image.open(photo_path) as image:
        normalized = ImageOps.exif_transpose(image).copy()

    left = 12
    top = 12
    right = normalized.width // 2 - 12
    bottom = normalized.height // 2 - 12
    expected_crop = normalized.crop((left, top, right, bottom)).convert("RGB")
    expected_size = expected_crop.size
    expected_rgb = _mean_rgb(expected_crop)

    asset_id = ws.asset_repo.add_photo_asset(source_id, str(photo_path), processing_status="assignment_done")
    observation_id = ws.asset_repo.ensure_face_observation(
        asset_id,
        bbox_top=float(top) / float(normalized.height),
        bbox_right=float(right) / float(normalized.width),
        bbox_bottom=float(bottom) / float(normalized.height),
        bbox_left=float(left) / float(normalized.width),
    )

    with Image.open(photo_path) as raw_image:
        wrong_left = max(0, min(raw_image.width - 1, int(float(left) / float(normalized.width) * raw_image.width)))
        wrong_top = max(0, min(raw_image.height - 1, int(float(top) / float(normalized.height) * raw_image.height)))
        wrong_right = max(
            wrong_left + 1,
            min(raw_image.width, int(float(right) / float(normalized.width) * raw_image.width)),
        )
        wrong_bottom = max(
            wrong_top + 1,
            min(raw_image.height, int(float(bottom) / float(normalized.height) * raw_image.height)),
        )
        wrong_crop = raw_image.crop((wrong_left, wrong_top, wrong_right, wrong_bottom)).convert("RGB")

    crop_path = ws.root / ".hikbox" / "artifacts" / "face-crops" / "stale-rotated.jpg"
    crop_path.parent.mkdir(parents=True, exist_ok=True)
    wrong_crop.save(crop_path, format="JPEG")
    ws.conn.execute(
        "UPDATE face_observation SET crop_path = ? WHERE id = ?",
        (str(crop_path), int(observation_id)),
    )

    context_path = ws.root / ".hikbox" / "artifacts" / "context" / f"obs-{int(observation_id)}.jpg"
    context_path.parent.mkdir(parents=True, exist_ok=True)
    wrong_context = Image.new("RGB", (260, 220), color=(24, 26, 32))
    wrong_context_draw = ImageDraw.Draw(wrong_context)
    wrong_context_draw.rectangle((70, 56, 182, 150), fill=(64, 76, 96))
    wrong_context_draw.rectangle((70, 56, 182, 150), outline=(255, 64, 64), width=4)
    wrong_context.save(context_path, format="JPEG")
    ws.conn.commit()

    return int(observation_id), expected_size, expected_rgb


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


def test_missing_crop_is_rebuilt_under_external_root(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    ws = build_seed_workspace(workspace, seed_media_assets=True, external_root=external_root)
    try:
        assert ws.media_observation_id is not None
        ws.break_crop_for_observation(int(ws.media_observation_id))
        client = TestClient(create_app(workspace=ws.root))

        response = client.get(f"/api/observations/{ws.media_observation_id}/crop")

        assert response.status_code == 200
        row = ws.conn.execute(
            "SELECT crop_path FROM face_observation WHERE id = ?",
            (int(ws.media_observation_id),),
        ).fetchone()
        assert row is not None
        rebuilt_path = Path(str(row["crop_path"]))
        assert rebuilt_path.is_relative_to(external_root.resolve())
        assert not rebuilt_path.is_relative_to((workspace / ".hikbox").resolve())
    finally:
        ws.close()


def test_stale_exif_oriented_crop_and_context_are_rebuilt_on_access(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        observation_id, expected_crop_size, expected_rgb = _create_exif_oriented_observation(ws)
        client = TestClient(create_app(workspace=ws.root))

        crop_response = client.get(f"/api/observations/{observation_id}/crop")
        context_response = client.get(f"/api/observations/{observation_id}/context")

        assert crop_response.status_code == 200
        crop_image = Image.open(BytesIO(crop_response.content)).convert("RGB")
        assert crop_image.size == expected_crop_size
        rebuilt_rgb = _mean_rgb(crop_image)
        assert all(abs(actual - expected) <= 30 for actual, expected in zip(rebuilt_rgb, expected_rgb))

        assert context_response.status_code == 200
        rebuilt_context = Image.open(BytesIO(context_response.content)).convert("RGB")
        assert rebuilt_context.size != (260, 220)
        assert max(rebuilt_context.size) <= 320
        assert _bbox_highlight_bounds(rebuilt_context) is not None
        assert ws.count_ops_event("preview.context.rebuild_requested") >= 2
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
