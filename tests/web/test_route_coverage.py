from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hikbox_pictures.product.config import initialize_workspace


@pytest.fixture()
def route_client(tmp_path: Path) -> TestClient:
    from hikbox_pictures.product.service_registry import build_service_container
    from hikbox_pictures.web.app import create_app

    layout = initialize_workspace(
        workspace_root=tmp_path / "workspace",
        external_root=tmp_path / "external",
    )
    app = create_app(build_service_container(layout))
    return TestClient(app)


@pytest.mark.parametrize(
    ("path", "expected_status"),
    [
        ("/", 200),
        ("/people/1", 200),
        ("/sources", 200),
        ("/sources/1/audit", 200),
        ("/exports", 200),
        ("/exports/1", 200),
        ("/logs", 200),
    ],
)
def test_required_page_routes_exist(route_client: TestClient, path: str, expected_status: int) -> None:
    response = route_client.get(path)

    assert response.status_code == expected_status

