from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace_ops_filters", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_seed_workspace = _MODULE.build_seed_workspace


def test_ops_event_repo_filters_run_kind_and_event_type(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        ws.ops_event_repo.append_event(
            level="info",
            component="scanner",
            event_type="scan.session.started",
            run_kind="scan",
            run_id="scan-101",
            message="scan started",
        )
        ws.ops_event_repo.append_event(
            level="info",
            component="scanner",
            event_type="scan.session.started",
            run_kind="scan",
            run_id="scan-102",
            message="scan started",
        )
        ws.ops_event_repo.append_event(
            level="info",
            component="exporter",
            event_type="export.delivery.started",
            run_kind="export",
            run_id="export-9",
            message="export started",
        )
        ws.conn.commit()

        rows = ws.ops_event_repo.list_recent(limit=20, run_kind="scan", event_type="scan.session.started")

        assert len(rows) == 2
        assert all(row["run_kind"] == "scan" for row in rows)
        assert all(row["event_type"] == "scan.session.started" for row in rows)
    finally:
        ws.close()


def test_ops_event_repo_filters_run_id_and_level(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        ws.ops_event_repo.append_event(
            level="error",
            component="exporter",
            event_type="export.delivery.failed",
            run_kind="export",
            run_id="export-7",
            message="failed 1",
        )
        ws.ops_event_repo.append_event(
            level="info",
            component="exporter",
            event_type="export.delivery.completed",
            run_kind="export",
            run_id="export-7",
            message="completed",
        )
        ws.ops_event_repo.append_event(
            level="error",
            component="exporter",
            event_type="export.delivery.failed",
            run_kind="export",
            run_id="export-8",
            message="failed 2",
        )
        ws.conn.commit()

        rows = ws.ops_event_repo.list_recent(limit=20, run_id="export-7", level="error")

        assert len(rows) == 1
        assert rows[0]["run_id"] == "export-7"
        assert rows[0]["level"] == "error"
        assert rows[0]["event_type"] == "export.delivery.failed"
    finally:
        ws.close()
