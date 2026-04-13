from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from hikbox_pictures.repositories.export_repo import ExportRepo
from hikbox_pictures.services.action_service import ActionService

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_seed_workspace = _MODULE.build_seed_workspace


def test_export_preview_returns_real_only_group_counts(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_export_assets=True)
    try:
        preview = ActionService(ws.conn).preview_export_template(template_id=ws.export_template_id)
        assert preview["matched_only_count"] == 2
        assert preview["matched_group_count"] == 1
    finally:
        ws.close()


def test_export_run_skips_already_delivered_asset(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_export_assets=True)
    try:
        service = ActionService(ws.conn)
        first = service.run_export_template(template_id=ws.export_template_id)
        second = service.run_export_template(template_id=ws.export_template_id)

        assert first["spec_hash"] == second["spec_hash"]
        assert second["skipped_count"] >= 1
        assert second["failed_count"] == 0
    finally:
        ws.close()


def test_export_finalize_failure_keeps_delivery_ledger(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = build_seed_workspace(tmp_path, seed_export_assets=True)
    try:
        def _raise_finalize(self, export_run_id: int, **kwargs) -> int:
            raise RuntimeError("finalize boom")

        monkeypatch.setattr(
            ExportRepo,
            "finish_export_run",
            _raise_finalize,
        )

        with pytest.raises(RuntimeError, match="finalize boom"):
            ActionService(ws.conn).run_export_template(template_id=ws.export_template_id)

        rows = ws.conn.execute(
            """
            SELECT target_path, status
            FROM export_delivery
            WHERE template_id = ?
              AND status IN ('ok', 'skipped', 'failed')
            ORDER BY id ASC
            """,
            (int(ws.export_template_id),),
        ).fetchall()
        assert len(rows) >= 1
        assert any(Path(str(row["target_path"])).exists() for row in rows)
    finally:
        ws.close()
