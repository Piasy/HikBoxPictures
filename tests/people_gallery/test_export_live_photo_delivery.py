from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from hikbox_pictures.services.action_service import ActionService

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_seed_workspace = _MODULE.build_seed_workspace


def test_export_run_upserts_live_photo_mov_delivery(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_export_assets=True)
    try:
        assert ws.export_live_photo_asset_id is not None
        summary = ActionService(ws.conn).run_export_template(template_id=ws.export_template_id)
        rows = ws.conn.execute(
            """
            SELECT asset_variant, target_path, status
            FROM export_delivery
            WHERE template_id = ?
              AND photo_asset_id = ?
            ORDER BY asset_variant ASC
            """,
            (int(ws.export_template_id), int(ws.export_live_photo_asset_id)),
        ).fetchall()

        assert summary["failed_count"] == 0
        assert [row["asset_variant"] for row in rows] == ["live_mov", "primary"]
        assert all(Path(str(row["target_path"])).exists() for row in rows)
    finally:
        ws.close()
