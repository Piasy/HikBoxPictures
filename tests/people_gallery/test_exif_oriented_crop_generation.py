from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from hikbox_pictures.services.asset_stage_runner import AssetStageRunner

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace_exif_crop", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_seed_workspace = _MODULE.build_seed_workspace


def _mean_rgb(image: Image.Image) -> tuple[int, int, int]:
    sample = image.resize((1, 1), resample=Image.Resampling.BILINEAR).convert("RGB")
    return tuple(int(channel) for channel in sample.getpixel((0, 0)))


def test_scan_crop_generation_uses_exif_oriented_coordinates(tmp_path: Path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        source_id = int(ws.source_repo.list_sources(active=True)[0]["id"])
        photo_path = tmp_path / "scan-oriented.jpg"

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

        asset_id = ws.asset_repo.add_photo_asset(source_id, str(photo_path), processing_status="faces_done")
        observation_id = ws.asset_repo.ensure_face_observation(
            asset_id,
            bbox_top=float(top) / float(normalized.height),
            bbox_right=float(right) / float(normalized.width),
            bbox_bottom=float(bottom) / float(normalized.height),
            bbox_left=float(left) / float(normalized.width),
        )
        ws.conn.commit()

        crop_path = AssetStageRunner(ws.conn)._ensure_face_crop(int(observation_id))

        with Image.open(crop_path) as crop_image:
            rebuilt = crop_image.convert("RGB")
            assert rebuilt.size == expected_crop.size
            rebuilt_rgb = _mean_rgb(rebuilt)
            expected_rgb = _mean_rgb(expected_crop)
            assert all(abs(actual - expected) <= 30 for actual, expected in zip(rebuilt_rgb, expected_rgb))
    finally:
        ws.close()


def test_scan_crop_generation_writes_under_external_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    ws = build_seed_workspace(workspace, external_root=external_root)
    try:
        source_id = int(ws.source_repo.list_sources(active=True)[0]["id"])
        photo_path = tmp_path / "scan-external-root.jpg"

        Image.new("RGB", (120, 120), color=(220, 180, 160)).save(photo_path, format="JPEG")
        asset_id = ws.asset_repo.add_photo_asset(source_id, str(photo_path), processing_status="faces_done")
        observation_id = ws.asset_repo.ensure_face_observation(
            asset_id,
            bbox_top=0.1,
            bbox_right=0.8,
            bbox_bottom=0.9,
            bbox_left=0.2,
        )
        ws.conn.commit()

        crop_path = Path(AssetStageRunner(ws.conn)._ensure_face_crop(int(observation_id)))

        assert crop_path.is_relative_to(external_root.resolve())
        assert not crop_path.is_relative_to((workspace / ".hikbox").resolve())
    finally:
        ws.close()
