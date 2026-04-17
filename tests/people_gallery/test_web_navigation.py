from __future__ import annotations

import importlib.resources as resources

from fastapi.testclient import TestClient

from hikbox_pictures.api.app import create_app
from tests.people_gallery.fixtures_workspace import build_seed_workspace


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

        identity_tuning = client.get("/identity-tuning")
        assert identity_tuning.status_code == 409
        assert "完整性错误" in identity_tuning.text

        css = client.get("/static/style.css")
        js = client.get("/static/app.js")
        assert css.status_code == 200
        assert js.status_code == 200
        assert ".person-card-cover" in css.text
        assert ".person-empty-state" in css.text
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
    assert pkg_root.joinpath("web/templates/identity_tuning.html").is_file()
    assert pkg_root.joinpath("web/static/style.css").is_file()
    assert pkg_root.joinpath("web/static/app.js").is_file()
