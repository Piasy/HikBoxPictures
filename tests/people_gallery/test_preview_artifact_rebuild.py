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
