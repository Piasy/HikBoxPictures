from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hikbox_pictures.api.app import create_app
from hikbox_pictures.services.path_guard import ensure_safe_asset_path

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_seed_workspace = _MODULE.build_seed_workspace


def test_ensure_safe_asset_path_blocks_traversal() -> None:
    with pytest.raises(PermissionError):
        ensure_safe_asset_path(candidate="/etc/passwd", allowed_roots=["/tmp/workspace/sample"])


def test_ensure_safe_asset_path_accepts_allowed_root(tmp_path) -> None:
    root = tmp_path / "allowed"
    root.mkdir(parents=True, exist_ok=True)
    target = root / "a" / "b.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"ok")

    resolved = ensure_safe_asset_path(candidate=str(target), allowed_roots=[str(root)])
    assert resolved == target.resolve()


def test_media_endpoint_rejects_photo_outside_allowed_roots(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_media_assets=True)
    try:
        assert ws.media_photo_id is not None
        ws.conn.execute(
            "UPDATE photo_asset SET primary_path = ? WHERE id = ?",
            ("/etc/passwd", int(ws.media_photo_id)),
        )
        ws.conn.commit()

        client = TestClient(create_app(workspace=ws.root))
        response = client.get(f"/api/photos/{ws.media_photo_id}/original")
        assert response.status_code == 403
    finally:
        ws.close()


def test_media_context_rejects_observation_outside_allowed_roots(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_media_assets=True)
    try:
        assert ws.media_photo_id is not None
        assert ws.media_observation_id is not None
        ws.conn.execute(
            "UPDATE photo_asset SET primary_path = ? WHERE id = ?",
            ("/etc/passwd", int(ws.media_photo_id)),
        )
        ws.conn.commit()

        client = TestClient(create_app(workspace=ws.root))
        response = client.get(f"/api/observations/{ws.media_observation_id}/context")
        assert response.status_code == 403
    finally:
        ws.close()
