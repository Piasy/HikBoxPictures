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


def test_scan_status_reads_real_session(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        client = TestClient(create_app(workspace=ws.root))
        expected = ws.latest_resumable_session()
        assert expected is not None
        expected_sources = ws.scan_repo.list_session_sources(int(expected["id"]))

        response = client.get("/api/scan/status")

        assert response.status_code == 200
        body = response.json()
        assert body["session_id"] == expected["id"]
        assert body["status"] == expected["status"]
        assert body["mode"] == expected["mode"]
        assert len(body["sources"]) == len(expected_sources)
        assert body["sources"][0]["status"] == expected_sources[0]["status"]
    finally:
        ws.close()


def test_scan_status_returns_idle_when_no_resumable_session(tmp_path) -> None:
    client = TestClient(create_app(workspace=tmp_path))
    response = client.get("/api/scan/status")
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] is None
    assert body["status"] == "idle"
    assert body["sources"] == []


def test_scan_start_or_resume_prefers_latest_resumable_session(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        client = TestClient(create_app(workspace=ws.root))
        expected = ws.latest_resumable_session()
        assert expected is not None

        response = client.post("/api/scan/start_or_resume")

        assert response.status_code == 200
        body = response.json()
        assert body["session_id"] == expected["id"]
        assert body["status"] == "running"
    finally:
        ws.close()


def test_scan_start_or_resume_creates_session_from_idle(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        ws.conn.execute(
            """
            UPDATE scan_session
            SET status = 'completed',
                finished_at = CURRENT_TIMESTAMP
            WHERE status IN ('pending', 'running', 'paused', 'interrupted')
            """
        )
        ws.conn.commit()

        client = TestClient(create_app(workspace=ws.root))
        response = client.post("/api/scan/start_or_resume")
        assert response.status_code == 200
        body = response.json()
        assert body["session_id"] is not None
        assert body["status"] == "running"
        assert body["mode"] == "incremental"
    finally:
        ws.close()


def test_people_reviews_export_and_logs_read_workspace_db(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        client = TestClient(create_app(workspace=ws.root))

        people_resp = client.get("/api/people")
        reviews_resp = client.get("/api/reviews")
        exports_resp = client.get("/api/export/templates")
        logs_resp = client.get("/api/logs/events")

        assert people_resp.status_code == 200
        assert reviews_resp.status_code == 200
        assert exports_resp.status_code == 200
        assert logs_resp.status_code == 200

        people = people_resp.json()
        assert [row["display_name"] for row in people] == [
            row["display_name"] for row in ws.person_repo.list_people()
        ]

        reviews = reviews_resp.json()
        assert len(reviews) == ws.review_repo.count()
        assert reviews[0]["review_type"] == "new_person"

        templates = exports_resp.json()
        assert len(templates) == ws.export_repo.count_templates()
        assert templates[0]["name"] == "家庭模板"
        assert isinstance(templates[0]["include_group"], bool)
        assert isinstance(templates[0]["export_live_mov"], bool)
        assert isinstance(templates[0]["enabled"], bool)

        events = logs_resp.json()
        assert len(events) == ws.ops_event_repo.count()
        assert events[0]["event_type"] == "seed_ready"
    finally:
        ws.close()


def test_logs_events_limit_out_of_range_returns_422(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        client = TestClient(create_app(workspace=ws.root))
        assert client.get("/api/logs/events", params={"limit": 0}).status_code == 422
        assert client.get("/api/logs/events", params={"limit": -1}).status_code == 422
        assert client.get("/api/logs/events", params={"limit": 1001}).status_code == 422
    finally:
        ws.close()
