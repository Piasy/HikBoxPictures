from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hikbox_pictures.api.app import create_app
from hikbox_pictures.cli import main
from hikbox_pictures.db.connection import connect_db

_DATASET_ROOT = Path("tests/data/e2e-face-input").resolve()


@pytest.mark.real_face_engine
def test_e2e_real_source_pipeline_without_seed_injection(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"

    assert main(["init", "--workspace", str(workspace)]) == 0
    assert (
        main(
            [
                "source",
                "add",
                "--workspace",
                str(workspace),
                "--name",
                "sample-input",
                "--root-path",
                str(_DATASET_ROOT),
            ]
        )
        == 0
    )
    assert main(["scan", "--workspace", str(workspace)]) == 0

    client = TestClient(create_app(workspace=workspace))
    reviews_response = client.get("/api/reviews")
    logs_response = client.get("/api/logs/events", params={"run_kind": "scan", "limit": 200})
    exports_response = client.get("/api/export/templates")
    reviews_page = client.get("/reviews")
    sources_page = client.get("/sources")

    assert reviews_response.status_code == 200
    assert logs_response.status_code == 200
    assert exports_response.status_code == 200
    assert reviews_page.status_code == 200
    assert sources_page.status_code == 200
    assert "queue-new_person" in reviews_page.text
    assert "completed" in sources_page.text
    assert exports_response.json() == []

    conn = connect_db(workspace / ".hikbox" / "library.db")
    try:
        counts = conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM photo_asset) AS photo_count,
              (SELECT COUNT(*) FROM face_observation WHERE active = 1) AS observation_count,
              (SELECT COUNT(*) FROM face_embedding) AS embedding_count,
              (
                SELECT COUNT(*)
                FROM review_item
                WHERE review_type = 'new_person'
                  AND status = 'open'
              ) AS new_person_review_count,
              (
                SELECT COUNT(*)
                FROM face_observation
                WHERE active = 1
                  AND crop_path IS NOT NULL
              ) AS crop_count
            """
        ).fetchone()
        session = conn.execute(
            "SELECT status FROM scan_session ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert counts is not None
        assert session is not None
        assert session["status"] == "completed"
        assert int(counts["photo_count"]) >= 30
        assert int(counts["observation_count"]) >= 30
        assert int(counts["embedding_count"]) >= 30
        assert int(counts["new_person_review_count"]) >= 1
        assert int(counts["crop_count"]) >= 1
    finally:
        conn.close()
