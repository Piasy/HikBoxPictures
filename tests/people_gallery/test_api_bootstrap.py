from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from hikbox_pictures.api.app import create_app

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace_api_bootstrap", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_seed_workspace = _MODULE.build_seed_workspace


def test_create_app_binds_workspace_and_health_route(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    build_seed_workspace(workspace, external_root=external_root).close()
    app = create_app(workspace=workspace)
    client = TestClient(app)

    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["workspace"] == str(workspace.resolve())
    assert body["db_path"].endswith(".hikbox/library.db")


def test_create_app_requires_initialized_workspace(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="config.json"):
        create_app(workspace=tmp_path)


def test_create_app_exposes_seed_workspace_core_routes(tmp_path: Path) -> None:
    ws = build_seed_workspace(tmp_path, seed_export_assets=True)
    try:
        client = TestClient(create_app(workspace=ws.root))

        scan_status = client.get("/api/scan/status")
        reviews = client.get("/api/reviews")
        preview = client.get(f"/api/export/templates/{ws.export_template_id}/preview")
        logs = client.get("/api/logs/events", params={"limit": 20})

        assert scan_status.status_code == 200
        assert reviews.status_code == 200
        assert preview.status_code == 200
        assert logs.status_code == 200
        scan_status_data = scan_status.json()
        assert scan_status_data["status"] == "paused"
        assert len(scan_status_data["sources"]) >= 1
        source = scan_status_data["sources"][0]
        assert "progress" in source
        assert "discovered_count" in source
        assert "metadata_done_count" in source
        assert "faces_done_count" in source
        assert "embeddings_done_count" in source
        assert "assignment_done_count" in source
        assert len(reviews.json()) >= 1
        preview_data = preview.json()
        assert preview_data["matched_only_count"] == 2
        assert preview_data["matched_group_count"] == 1
        assert isinstance(logs.json(), list)
    finally:
        ws.close()
