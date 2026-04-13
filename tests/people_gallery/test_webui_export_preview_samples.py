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


def test_export_preview_has_sample_cards(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_export_assets=True)
    try:
        client = TestClient(create_app(workspace=ws.root))
        html = client.get("/exports").text

        assert "export-preview-sample" in html
    finally:
        ws.close()


def test_export_preview_samples_skip_photos_without_observation_before_limit(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        sources = ws.source_repo.list_sources(active=True)
        assert sources
        source_id = int(sources[0]["id"])
        asset_ids = ws.seed_source_assets(
            source_id,
            [f"/tmp/no-observation-{index}.jpg" for index in range(1, 8)],
        )
        ws.asset_repo.ensure_face_observation(asset_ids[6])
        ws.conn.commit()

        client = TestClient(create_app(workspace=ws.root))
        html = client.get("/exports").text

        assert "export-preview-sample" in html
        assert f"/api/photos/{asset_ids[6]}/original" in html
    finally:
        ws.close()
