from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from fastapi.testclient import TestClient

from hikbox_pictures.api.app import create_app
from hikbox_pictures.cli import main
from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.workspace import ensure_workspace_layout

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_seed_workspace_with_mock_embeddings = _MODULE.build_seed_workspace_with_mock_embeddings


def test_number_images_mock_embedding_pipeline_end_to_end(tmp_path) -> None:
    workspace = tmp_path / "ws"

    assert main(["init", "--workspace", str(workspace), "--external-root", str(workspace / ".hikbox")]) == 0
    seeded = build_seed_workspace_with_mock_embeddings(workspace)
    template_id = int(seeded["template_id"])

    rc_rebuild = main(["rebuild-artifacts", "--workspace", str(workspace)])
    assert rc_rebuild == 0

    rc_export = main(["export", "run", "--workspace", str(workspace), "--template-id", str(template_id)])
    assert rc_export == 0

    paths = ensure_workspace_layout(workspace)
    assert (paths.artifacts_dir / "ann" / "prototype_index.npz").exists()

    conn = connect_db(paths.db_path)
    try:
        run_row = conn.execute(
            """
            SELECT template_id, matched_only_count, matched_group_count, exported_count, failed_count
            FROM export_run
            WHERE template_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (template_id,),
        ).fetchone()
        assert run_row is not None
        assert int(run_row["template_id"]) == template_id
        assert int(run_row["matched_only_count"]) == 1
        assert int(run_row["matched_group_count"]) == 0
        assert int(run_row["exported_count"]) >= 1
        assert int(run_row["failed_count"]) == 0
    finally:
        conn.close()


def test_mock_embedding_path_visible_in_webui(tmp_path) -> None:
    workspace = tmp_path / "ws"
    assert main(["init", "--workspace", str(workspace), "--external-root", str(workspace / ".hikbox")]) == 0
    seeded = build_seed_workspace_with_mock_embeddings(workspace)
    template_id = int(seeded["template_id"])
    review_id = int(seeded["review_id"])
    review_payload = str(seeded["review_payload"])

    client = TestClient(create_app(workspace=workspace))
    people_html = client.get("/").text
    exports_html = client.get("/exports").text
    reviews_html = client.get("/reviews").text
    reviews_response = client.get("/api/reviews")

    assert "人物甲" in people_html
    assert "人物乙" in people_html
    assert "export-preview-tile" in exports_html
    assert "queue-block" in reviews_html
    assert "queue-new_person" in reviews_html
    assert "review #" in reviews_html
    assert reviews_response.status_code == 200
    review_items = reviews_response.json()
    assert isinstance(review_items, list)
    assert len(review_items) >= 1
    target_reviews = [item for item in review_items if int(item.get("id", 0)) == review_id]
    assert len(target_reviews) >= 1
    assert any(str(item.get("payload_json")) == review_payload for item in target_reviews)
    assert f"review #{review_id}" in reviews_html

    preview = client.get(f"/api/export/templates/{template_id}/preview")
    assert preview.status_code == 200
    body = preview.json()
    assert body["matched_only_count"] == 1
    assert body["matched_group_count"] == 0


def test_mock_embedding_repeat_injection_is_idempotent(tmp_path) -> None:
    workspace = tmp_path / "ws"
    assert main(["init", "--workspace", str(workspace), "--external-root", str(workspace / ".hikbox")]) == 0

    first = build_seed_workspace_with_mock_embeddings(workspace)
    second = build_seed_workspace_with_mock_embeddings(workspace)

    assert int(second["template_id"]) == int(first["template_id"])

    paths = ensure_workspace_layout(workspace)
    conn = connect_db(paths.db_path)
    try:
        row = conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM face_observation WHERE detector_key = 'mock_embedding_fixture') AS obs_count,
              (
                SELECT COUNT(*)
                FROM person_face_assignment AS pfa
                JOIN face_observation AS fo
                  ON fo.id = pfa.face_observation_id
                WHERE fo.detector_key = 'mock_embedding_fixture'
                  AND pfa.active = 1
              ) AS assignment_count,
              (
                SELECT COUNT(*)
                FROM export_template
                WHERE name = '甲乙模板'
                  AND output_root = ?
              ) AS template_count,
              (
                SELECT COUNT(*)
                FROM review_item
                WHERE status = 'open'
                  AND payload_json LIKE '%\"mock_marker\"%'
              ) AS review_count
            """,
            (str(paths.exports_dir / "mock"),),
        ).fetchone()
        assert row is not None
        assert int(row["obs_count"]) == 3
        assert int(row["assignment_count"]) == 3
        assert int(row["template_count"]) == 1
        assert int(row["review_count"]) == 1
    finally:
        conn.close()
