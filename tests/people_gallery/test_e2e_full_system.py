from __future__ import annotations

import re
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from fastapi.testclient import TestClient

from hikbox_pictures.api.app import create_app
from hikbox_pictures.cli import main

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_seed_workspace = _MODULE.build_seed_workspace
build_seed_workspace_with_mock_embeddings = _MODULE.build_seed_workspace_with_mock_embeddings


def test_full_system_webui_flow_keeps_review_queue_available(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_export_assets=True, seed_media_assets=True)
    try:
        assert ws.media_photo_id is not None
        client = TestClient(create_app(workspace=ws.root))
        baseline_reviews = client.get("/api/reviews")
        assert baseline_reviews.status_code == 200
        baseline_ids = {int(item["id"]) for item in baseline_reviews.json()}
        assert baseline_ids

        rename_resp = client.post("/api/people/1/actions/rename", json={"display_name": "爸爸"})
        assert rename_resp.status_code == 200

        people_html = client.get("/").text
        assert "爸爸" in people_html

        detail_html = client.get("/people/1").text
        reviews_html = client.get("/reviews").text
        exports_html = client.get("/exports").text
        assert 'data-viewer-layer="original"' in detail_html
        assert 'data-action="viewer-next"' in reviews_html
        assert "export-preview-sample" in exports_html

        ws.inject_broken_image_for_photo(int(ws.media_photo_id))
        broken_preview = client.get(f"/api/photos/{ws.media_photo_id}/preview")
        assert broken_preview.status_code == 422

        reviews_after_error = client.get("/reviews")
        assert reviews_after_error.status_code == 200
        html = reviews_after_error.text
        assert "queue-block" in html
        for review_id in sorted(baseline_ids):
            assert f"review #{review_id}" in html
    finally:
        ws.close()


def test_full_system_mock_embedding_flow_has_people_and_export_sample(tmp_path) -> None:
    ws_root = tmp_path / "ws"
    seeded = build_seed_workspace_with_mock_embeddings(ws_root)
    template_id = int(seeded["template_id"])

    client = TestClient(create_app(workspace=ws_root))
    people_html = client.get("/").text
    exports_html = client.get("/exports").text
    preview = client.get(f"/api/export/templates/{template_id}/preview")

    assert "人物甲" in people_html
    assert "人物乙" in people_html
    assert "export-preview-sample" in exports_html
    assert preview.status_code == 200
    assert preview.json()["matched_only_count"] >= 1


def test_full_system_control_plane_happy_path_init_to_logs(tmp_path, capsys) -> None:
    workspace = tmp_path / "workspace"
    rc_init = main(["init", "--workspace", str(workspace)])
    assert rc_init == 0
    capsys.readouterr()

    ws = build_seed_workspace(workspace, seed_export_assets=True, seed_media_assets=True)
    try:
        client = TestClient(create_app(workspace=workspace))
        sources_html = client.get("/sources").text
        assert "iCloud" in sources_html
        assert "NAS" in sources_html

        rc_scan = main(["scan", "--workspace", str(workspace)])
        assert rc_scan == 0
        out_scan = capsys.readouterr().out
        assert "scan session_id=" in out_scan
        assert "mode=incremental" in out_scan

        reviews_before = client.get("/api/reviews")
        assert reviews_before.status_code == 200
        review_items = reviews_before.json()
        assert len(review_items) >= 1
        review_id = int(review_items[0]["id"])
        dismiss_resp = client.post(f"/api/reviews/{review_id}/actions/dismiss")
        assert dismiss_resp.status_code == 200
        reviews_after = client.get("/api/reviews")
        assert reviews_after.status_code == 200
        assert len(reviews_after.json()) == len(review_items) - 1

        rc_export = main(
            ["export", "run", "--workspace", str(workspace), "--template-id", str(ws.export_template_id)]
        )
        assert rc_export == 0
        out_export = capsys.readouterr().out
        assert "matched_only=2" in out_export
        assert "matched_group=1" in out_export
        assert "failed=0" in out_export

        run_id_match = re.search(r"run_id=(\d+)", out_export)
        assert run_id_match is not None
        run_id = run_id_match.group(1)
        logs_resp = client.get("/api/logs/events", params={"run_kind": "export", "run_id": run_id, "limit": 200})
        assert logs_resp.status_code == 200
        event_types = {str(item["event_type"]) for item in logs_resp.json()}
        assert "export.delivery.started" in event_types
        assert "export.delivery.completed" in event_types
        assert "export.delivery.failed" not in event_types

        rc_logs_prune = main(["logs", "prune", "--workspace", str(workspace), "--days", "90"])
        assert rc_logs_prune == 0
        out_logs = capsys.readouterr().out
        assert "logs pruned=" in out_logs
        assert "days=90" in out_logs
    finally:
        ws.close()
