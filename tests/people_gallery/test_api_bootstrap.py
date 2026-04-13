from __future__ import annotations

from fastapi.testclient import TestClient

from hikbox_pictures.api.app import create_app


def test_create_app_binds_workspace_and_health_route(tmp_path) -> None:
    app = create_app(workspace=tmp_path)
    client = TestClient(app)

    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["workspace"] == str(tmp_path.resolve())
    assert body["db_path"].endswith(".hikbox/library.db")
