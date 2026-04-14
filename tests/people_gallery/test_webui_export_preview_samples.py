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

        assert "export-preview-grid" in html
        assert "export-preview-tile" in html
        assert "家庭模板" in html
        assert "IMG_ONLY_1.jpg" not in html
        assert "<code>/api/photos/1/original</code>" not in html
        assert "/api/photos/1/preview" in html
    finally:
        ws.close()


def test_export_preview_samples_ignore_unrelated_assets(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_export_assets=True)
    try:
        sources = ws.source_repo.list_sources(active=True)
        assert sources
        source_id = int(sources[0]["id"])
        asset_ids = ws.seed_source_assets(
            source_id,
            ["/tmp/unrelated-export-preview.jpg"],
        )
        ws.asset_repo.ensure_face_observation(asset_ids[0])
        ws.conn.commit()

        client = TestClient(create_app(workspace=ws.root))
        html = client.get("/exports").text

        assert "export-preview-tile" in html
        assert f"/api/photos/{asset_ids[0]}/original" not in html
        assert f"/api/photos/{asset_ids[0]}/preview" not in html
    finally:
        ws.close()


def test_export_preview_page_hides_fake_samples_without_templates(tmp_path) -> None:
    client = TestClient(create_app(workspace=tmp_path))
    html = client.get("/exports").text

    assert "当前还没有导出模板" in html
    assert "export-preview-tile" not in html
    assert "export-photo-" not in html
