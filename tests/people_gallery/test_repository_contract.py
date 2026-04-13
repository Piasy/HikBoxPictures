from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import hikbox_pictures
import pytest

from hikbox_pictures.db import connection as db_connection

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_seed_workspace = _MODULE.build_seed_workspace


def test_imports_bind_to_current_workspace_source_tree() -> None:
    project_root = Path(__file__).resolve().parents[2]
    module_file = Path(hikbox_pictures.__file__).resolve()
    assert module_file.is_relative_to(project_root / "src")


def test_seed_workspace_counts(tmp_path):
    ws = build_seed_workspace(tmp_path)
    try:
        counts = ws.counts()

        assert counts["library_source"] == 2
        assert counts["person"] == 3
        assert counts["review_item"] == 4
        assert counts["export_template"] == 1
    finally:
        ws.close()


def test_latest_resumable_scan_session(tmp_path):
    ws = build_seed_workspace(tmp_path)
    try:
        latest = ws.scan_repo.latest_resumable_session()
        assert latest is not None
        assert latest["status"] == "paused"

        ws.scan_repo.create_session(mode="incremental", status="abandoned", started=True)
        later = ws.scan_repo.latest_resumable_session()
        assert later is not None
        assert later["status"] == "paused"
    finally:
        ws.close()


def test_ops_event_list_recent_clamps_limit(tmp_path):
    ws = build_seed_workspace(tmp_path)
    try:
        assert ws.ops_event_repo.count() == 1
        for i in range(5):
            ws.ops_event_repo.append_event(
                level="info",
                component="seed",
                event_type=f"seed.extra.{i}",
                message="extra",
            )
        ws.conn.commit()

        neg = ws.ops_event_repo.list_recent(limit=-10)
        assert len(neg) == 1

        huge = ws.ops_event_repo.list_recent(limit=100_000)
        assert len(huge) == 6
    finally:
        ws.close()


def test_review_repo_rejects_invalid_payload_json(tmp_path):
    ws = build_seed_workspace(tmp_path)
    try:
        with pytest.raises(ValueError, match="payload_json 必须是合法 JSON 字符串"):
            ws.review_repo.create_review_item(
                "new_person",
                payload_json="{bad-json",
            )
    finally:
        ws.close()


def test_source_repo_unique_root_path_constraint(tmp_path):
    ws = build_seed_workspace(tmp_path)
    try:
        with pytest.raises(db_connection.sqlite3.IntegrityError):
            ws.source_repo.add_source(name="dup", root_path="/data/a", root_fingerprint="fp-dup", active=True)
    finally:
        ws.close()
