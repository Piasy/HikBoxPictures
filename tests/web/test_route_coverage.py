from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.web.app import ServiceContainer, create_app


def _build_client(tmp_path: Path) -> TestClient:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    app = create_app(ServiceContainer.from_library_db(layout.library_db_path))
    return TestClient(app)


def test_home_page_route(tmp_path: Path) -> None:
    client = _build_client(tmp_path)
    resp = client.get("/")
    assert resp.status_code == 200


def test_people_detail_page_route(tmp_path: Path) -> None:
    client = _build_client(tmp_path)
    resp = client.get("/people/1")
    assert resp.status_code == 200


def test_sources_page_route(tmp_path: Path) -> None:
    client = _build_client(tmp_path)
    resp = client.get("/sources")
    assert resp.status_code == 200


def test_sources_audit_page_route(tmp_path: Path) -> None:
    client = _build_client(tmp_path)
    resp = client.get("/sources/1/audit")
    assert resp.status_code == 200


def test_exports_page_route(tmp_path: Path) -> None:
    client = _build_client(tmp_path)
    resp = client.get("/exports")
    assert resp.status_code == 200


def test_export_detail_page_route(tmp_path: Path) -> None:
    client = _build_client(tmp_path)
    resp = client.get("/exports/1")
    assert resp.status_code == 200


def test_logs_page_route(tmp_path: Path) -> None:
    client = _build_client(tmp_path)
    resp = client.get("/logs")
    assert resp.status_code == 200
