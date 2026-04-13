from __future__ import annotations

import sys
import time
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


def test_review_dismiss_action_sets_dismissed_and_resolved_at(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        client = TestClient(create_app(workspace=ws.root))

        response = client.post("/api/reviews/1/actions/dismiss")

        assert response.status_code == 200
        row = ws.get_review_item(1)
        assert row is not None
        assert row["status"] == "dismissed"
        assert row["resolved_at"] is not None
    finally:
        ws.close()


def test_review_dismiss_action_returns_404_for_missing_id(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        client = TestClient(create_app(workspace=ws.root))

        response = client.post("/api/reviews/999/actions/dismiss")

        assert response.status_code == 404
    finally:
        ws.close()


def test_review_dismiss_action_is_idempotent_and_keeps_first_resolved_at(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        client = TestClient(create_app(workspace=ws.root))

        first = client.post("/api/reviews/1/actions/dismiss")
        assert first.status_code == 200
        first_resolved_at = first.json()["resolved_at"]
        assert first_resolved_at is not None

        time.sleep(1.1)
        second = client.post("/api/reviews/1/actions/dismiss")
        assert second.status_code == 200

        row = ws.get_review_item(1)
        assert row is not None
        assert row["resolved_at"] == first_resolved_at
    finally:
        ws.close()
