from __future__ import annotations

import importlib.resources as resources
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


def test_web_navigation_routes_and_static_assets(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        client = TestClient(create_app(workspace=ws.root))

        route_checks = {
            "/": "人物库",
            "/people/1": "人物详情",
            "/reviews": "待审核",
            "/sources": "源目录与扫描",
            "/exports": "导出模板",
            "/logs": "运行日志",
        }
        for route, expected_text in route_checks.items():
            response = client.get(route)
            assert response.status_code == 200
            assert expected_text in response.text

        css = client.get("/static/style.css")
        js = client.get("/static/app.js")
        assert css.status_code == 200
        assert js.status_code == 200
        assert ".people-gallery-viewer .viewer-layers" in css.text
        assert 'img[data-viewer-layer="context"]' in css.text
    finally:
        ws.close()


def test_person_detail_returns_404_when_person_missing(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        client = TestClient(create_app(workspace=ws.root))
        response = client.get("/people/999999")
        assert response.status_code == 404
    finally:
        ws.close()


def test_web_resources_paths_available_in_package() -> None:
    pkg_root = resources.files("hikbox_pictures")
    assert pkg_root.joinpath("web/templates/base.html").is_file()
    assert pkg_root.joinpath("web/templates/people.html").is_file()
    assert pkg_root.joinpath("web/static/style.css").is_file()
    assert pkg_root.joinpath("web/static/app.js").is_file()
