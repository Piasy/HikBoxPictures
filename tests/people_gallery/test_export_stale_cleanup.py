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


def test_export_rule_change_marks_previous_delivery_stale(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_export_assets=True)
    try:
        service = ActionService(ws.conn)
        first = service.run_export_template(template_id=ws.export_template_id)
        ws.export_repo.update_template_include_group(template_id=ws.export_template_id, include_group=False)
        second = service.run_export_template(template_id=ws.export_template_id)

        assert second["spec_hash"] != first["spec_hash"]
        assert ws.export_repo.count_stale_deliveries(template_id=ws.export_template_id) > 0
    finally:
        ws.close()


def test_export_assignment_change_marks_same_spec_delivery_stale(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_export_assets=True)
    try:
        service = ActionService(ws.conn)
        first = service.run_export_template(template_id=ws.export_template_id)

        asset_row = ws.conn.execute(
            """
            SELECT id
            FROM photo_asset
            WHERE primary_path LIKE ?
            LIMIT 1
            """,
            ("%IMG_ONLY_1.jpg",),
        ).fetchone()
        assert asset_row is not None
        asset_id = int(asset_row["id"])

        ws.conn.execute(
            """
            UPDATE person_face_assignment
            SET active = 0
            WHERE person_id = (
                SELECT id FROM person WHERE display_name = '人物B' LIMIT 1
            )
              AND active = 1
              AND face_observation_id IN (
                SELECT id FROM face_observation
                WHERE photo_asset_id = ?
                  AND active = 1
              )
            """,
            (asset_id,),
        )
        ws.conn.commit()

        second = service.run_export_template(template_id=ws.export_template_id)
        assert second["spec_hash"] == first["spec_hash"]

        row = ws.conn.execute(
            """
            SELECT status
            FROM export_delivery
            WHERE template_id = ?
              AND spec_hash = ?
              AND photo_asset_id = ?
              AND asset_variant = 'primary'
            LIMIT 1
            """,
            (int(ws.export_template_id), str(first["spec_hash"]), asset_id),
        ).fetchone()
        assert row is not None
        assert row["status"] == "stale"
    finally:
        ws.close()


def test_export_capture_month_change_recomputes_target_path(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_export_assets=True)
    try:
        service = ActionService(ws.conn)
        first = service.run_export_template(template_id=ws.export_template_id)

        asset_row = ws.conn.execute(
            """
            SELECT id
            FROM photo_asset
            WHERE primary_path LIKE ?
            LIMIT 1
            """,
            ("%IMG_ONLY_1.jpg",),
        ).fetchone()
        assert asset_row is not None
        asset_id = int(asset_row["id"])

        before = ws.conn.execute(
            """
            SELECT bucket, target_path, status
            FROM export_delivery
            WHERE template_id = ?
              AND spec_hash = ?
              AND photo_asset_id = ?
              AND asset_variant = 'primary'
            LIMIT 1
            """,
            (int(ws.export_template_id), str(first["spec_hash"]), asset_id),
        ).fetchone()
        assert before is not None
        before_target_path = Path(str(before["target_path"]))
        assert "2025-04" in before_target_path.parts

        ws.conn.execute(
            """
            UPDATE photo_asset
            SET capture_datetime = ?,
                capture_month = ?
            WHERE id = ?
            """,
            ("2025-05-01T08:00:00+08:00", "2025-05", asset_id),
        )
        ws.conn.commit()

        second = service.run_export_template(template_id=ws.export_template_id)
        assert second["spec_hash"] == first["spec_hash"]

        after = ws.conn.execute(
            """
            SELECT bucket, target_path, status
            FROM export_delivery
            WHERE template_id = ?
              AND spec_hash = ?
              AND photo_asset_id = ?
              AND asset_variant = 'primary'
            LIMIT 1
            """,
            (int(ws.export_template_id), str(first["spec_hash"]), asset_id),
        ).fetchone()
        assert after is not None
        after_target_path = Path(str(after["target_path"]))
        assert str(after["bucket"]) == str(before["bucket"])
        assert after_target_path != before_target_path
        assert "2025-05" in after_target_path.parts
        assert after["status"] == "ok"
        assert after_target_path.exists()
    finally:
        ws.close()
