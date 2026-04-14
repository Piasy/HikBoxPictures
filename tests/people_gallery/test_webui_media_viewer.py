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


def test_person_detail_contains_media_viewer_layers_and_actions(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_export_assets=True)
    try:
        client = TestClient(create_app(workspace=ws.root))
        html = client.get("/people/1").text

        assert "person-detail-page" in html
        assert "person-detail-viewer-shell" in html
        assert 'data-viewer-layer="crop"' in html
        assert 'data-viewer-layer="context"' in html
        assert 'data-viewer-layer="original"' in html

        assert 'data-action="viewer-prev"' in html
        assert 'data-action="viewer-next"' in html
        assert 'data-action="viewer-toggle-bbox"' not in html
    finally:
        ws.close()
