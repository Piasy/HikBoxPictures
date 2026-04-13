from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from fastapi.testclient import TestClient

from hikbox_pictures.api.app import create_app
from hikbox_pictures.services.asset_stage_runner import AssetStageRunner

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


def test_scan_status_reports_source_progress(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        session_id = ws.scan_repo.create_session(mode="incremental", status="running", started=True)
        source_id = int(ws.source_repo.list_sources(active=True)[0]["id"])
        session_source_id = ws.scan_repo.create_session_source(session_id, source_id, status="running")
        baseline_assets = ws.asset_repo.count_assets_for_source(source_id)
        ws.seed_source_assets(source_id, ["/tmp/a.jpg", "/tmp/b.jpg"])

        runner = AssetStageRunner(ws.conn)
        runner.run_stage(session_source_id, "metadata")
        runner.run_stage(session_source_id, "faces")
        runner.run_stage(session_source_id, "embeddings")

        client = TestClient(create_app(workspace=ws.root))
        response = client.get("/api/scan/status")

        assert response.status_code == 200
        body = response.json()
        source_rows = [row for row in body["sources"] if row["id"] == session_source_id]
        assert len(source_rows) == 1
        source = source_rows[0]
        expected = baseline_assets + 2
        assert source["discovered_count"] == expected
        assert source["metadata_done_count"] == expected
        assert source["faces_done_count"] == expected
        assert source["embeddings_done_count"] == expected
        assert source["assignment_done_count"] == baseline_assets
        assert source["progress"] == {
            "discovered": expected,
            "metadata_done": expected,
            "faces_done": expected,
            "embeddings_done": expected,
            "assignment_done": baseline_assets,
        }
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


def test_export_preview_contains_real_counts(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_export_assets=True)
    try:
        client = TestClient(create_app(workspace=ws.root))
        response = client.get(f"/api/export/templates/{ws.export_template_id}/preview")

        assert response.status_code == 200
        body = response.json()
        assert body["template_id"] == ws.export_template_id
        assert body["matched_only_count"] == 2
        assert body["matched_group_count"] == 1
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


def test_logs_api_filter_event_type(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        ws.ops_event_repo.append_event(
            level="info",
            component="scanner",
            event_type="scan.session.started",
            run_kind="scan",
            run_id="scan-200",
            message="scan started",
        )
        ws.ops_event_repo.append_event(
            level="info",
            component="exporter",
            event_type="export.delivery.started",
            run_kind="export",
            run_id="export-200",
            message="export started",
        )
        ws.conn.commit()

        client = TestClient(create_app(workspace=ws.root))
        response = client.get(
            "/api/logs/events",
            params={"event_type": "scan.session.started", "run_kind": "scan", "limit": 50},
        )

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["event_type"] == "scan.session.started"
        assert body[0]["run_kind"] == "scan"
    finally:
        ws.close()


def test_people_api_matches_people_page(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        client = TestClient(create_app(workspace=ws.root))
        people_response = client.get("/api/people")
        page_response = client.get("/")

        assert people_response.status_code == 200
        assert page_response.status_code == 200

        api_people = people_response.json()
        html = page_response.text
        assert html.count("person-card") == len(api_people)
        for person in api_people:
            assert str(person["display_name"]) in html
            assert f"/people/{person['id']}" in html
    finally:
        ws.close()


def test_media_original_missing_returns_structured_error(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_media_assets=True)
    try:
        assert ws.media_photo_id is not None
        ws.break_original_for_photo(int(ws.media_photo_id))
        client = TestClient(create_app(workspace=ws.root))

        response = client.get(f"/api/photos/{ws.media_photo_id}/original")

        assert response.status_code == 404
        payload = response.json()
        assert payload["error_code"] == "preview.asset.missing"
        assert "message" in payload
    finally:
        ws.close()
