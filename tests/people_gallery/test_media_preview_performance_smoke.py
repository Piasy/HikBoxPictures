from __future__ import annotations

import math
import sys
import time
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


def test_preview_endpoint_latency_smoke_under_600ms(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_media_assets=True)
    try:
        assert ws.media_photo_id is not None
        client = TestClient(create_app(workspace=ws.root))

        warmup = client.get(f"/api/photos/{ws.media_photo_id}/preview")
        assert warmup.status_code == 200

        durations_ms: list[float] = []
        for _ in range(5):
            start = time.perf_counter()
            response = client.get(f"/api/photos/{ws.media_photo_id}/preview")
            elapsed_ms = (time.perf_counter() - start) * 1000
            assert response.status_code == 200
            durations_ms.append(elapsed_ms)

        # 烟测允许一次偶发抖动，使用 80 分位门槛降低环境噪声导致的误报。
        rank = max(math.ceil(len(durations_ms) * 0.8) - 1, 0)
        p80_ms = sorted(durations_ms)[rank]
        assert p80_ms <= 600.0
    finally:
        ws.close()
