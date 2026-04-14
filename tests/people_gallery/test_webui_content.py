from __future__ import annotations

import json
import re
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from hikbox_pictures.api.app import create_app
from hikbox_pictures.cli import main
from hikbox_pictures.deepface_engine import embedding_to_blob
from hikbox_pictures.services.web_query_service import WebQueryService
from tests.people_gallery.real_image_helper import copy_raw_face_image

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_seed_workspace = _MODULE.build_seed_workspace
build_seed_workspace_with_mock_embeddings = _MODULE.build_seed_workspace_with_mock_embeddings


def _append_new_person_review(ws, *, file_name: str, vector: np.ndarray) -> tuple[int, int]:
    source_id = int(ws.source_repo.list_sources(active=True)[0]["id"])
    asset_id = ws.asset_repo.add_photo_asset(
        source_id,
        str((ws.root / file_name).resolve()),
        processing_status="assignment_done",
    )
    observation_id = int(
        ws.conn.execute(
            """
            INSERT INTO face_observation(
                photo_asset_id,
                bbox_top,
                bbox_right,
                bbox_bottom,
                bbox_left,
                face_area_ratio,
                detector_key,
                detector_version,
                active
            )
            VALUES (?, 0.1, 0.9, 0.9, 0.1, 0.22, 'retinaface', 'MockArcFace', 1)
            RETURNING id
            """,
            (asset_id,),
        ).fetchone()["id"]
    )
    ws.conn.execute(
        """
        INSERT INTO face_embedding(
            face_observation_id,
            feature_type,
            model_key,
            dimension,
            vector_blob,
            normalized
        )
        VALUES (?, 'face', 'MockArcFace@retinaface', ?, ?, 1)
        """,
        (
            observation_id,
            int(vector.size),
            embedding_to_blob(vector),
        ),
    )
    payload = json.dumps(
        {
            "face_observation_id": observation_id,
            "candidates": [],
            "model_key": "MockArcFace@retinaface",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    review_id = ws.review_repo.create_review_item(
        "new_person",
        payload_json=payload,
        priority=15,
        face_observation_id=observation_id,
    )
    return int(review_id), observation_id


def test_people_page_has_cards_and_real_names(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        client = TestClient(create_app(workspace=ws.root))
        html = client.get("/").text

        assert "person-card" in html
        assert "进入维护" in html
        assert "person-empty-state" not in html
        assert 'data-viewer-layer="original"' not in html
        for row in ws.person_repo.list_people():
            assert str(row["display_name"]) in html
    finally:
        ws.close()


def test_people_page_shows_cover_and_metrics_when_assignments_exist(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_export_assets=True)
    try:
        client = TestClient(create_app(workspace=ws.root))
        html = client.get("/").text

        assert 'class="person-card-cover"' in html
        assert 'class="person-card-image"' in html
        assert 'alt="人物A 封面"' in html
        assert "待审核 2" in html
        assert "照片 4 · 样本 4" in html
        assert "/api/observations/" in html
    finally:
        ws.close()


def test_reviews_page_has_typed_queues(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        client = TestClient(create_app(workspace=ws.root))
        html = client.get("/reviews").text

        assert "review-hero" in html
        assert "review-layout" in html
        assert "review-inspector" in html
        assert "当前共有 4 条待处理项" in html
        assert "queue-new_person" in html
        assert "queue-possible_merge" in html
        assert "queue-possible_split" in html
        assert "queue-low_confidence_assignment" in html
        assert "people-gallery-viewer" not in html
        assert 'data-queue-toggle' in html
        assert 'data-review-queue-sticky-stack' in html
        assert 'data-action="viewer-prev"' in html
        assert 'data-action="viewer-next"' in html
        assert 'data-action="viewer-toggle-bbox"' not in html
        assert 'data-action="review-create-person"' in html
        assert 'data-action="review-assign-person"' in html
        assert "P15" not in html
        queue_tags = re.findall(r'(<details\s+id="queue-[^"]+"[^>]*>)', html)
        assert len(queue_tags) == 4
        assert all(" open" not in tag for tag in queue_tags)
        new_person_section = re.search(
            r'(<details\s+id="queue-new_person"[\s\S]*?</details>)',
            html,
        )
        assert new_person_section is not None
        assert 'data-action="review-create-person"' in new_person_section.group(1)
        assert 'data-action="review-assign-person"' in new_person_section.group(1)
        assert 'data-action="review-ignore"' in new_person_section.group(1)
        assert 'data-action="review-dismiss"' not in new_person_section.group(1)
    finally:
        ws.close()


def test_reviews_page_links_queue_cards_to_viewer_when_samples_exist(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    build_seed_workspace_with_mock_embeddings(workspace)

    client = TestClient(create_app(workspace=workspace))
    html = client.get("/reviews").text

    assert "review-hero" in html
    assert "queue-face" in html
    assert "queue-item-headline" in html
    assert "查看证据" in html
    assert 'data-review-focus-index="' in html
    assert 'data-viewer-current-label' in html
    assert "review #1" in html
    assert "/api/observations/" in html
    assert "人物甲" in html


def test_reviews_page_groups_similar_new_person_samples(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        review_a, _ = _append_new_person_review(
            ws,
            file_name="cluster-a.jpg",
            vector=np.asarray([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
        )
        review_b, _ = _append_new_person_review(
            ws,
            file_name="cluster-b.jpg",
            vector=np.asarray([0.98, 0.18, 0.0, 0.0], dtype=np.float32),
        )
        review_c, _ = _append_new_person_review(
            ws,
            file_name="cluster-c.jpg",
            vector=np.asarray([0.96, 0.27, 0.0, 0.0], dtype=np.float32),
        )
        review_d, _ = _append_new_person_review(
            ws,
            file_name="cluster-d.jpg",
            vector=np.asarray([0.94, 0.34, 0.0, 0.0], dtype=np.float32),
        )
        review_e, _ = _append_new_person_review(
            ws,
            file_name="cluster-e.jpg",
            vector=np.asarray([-1.0, 0.0, 0.0, 0.0], dtype=np.float32),
        )
        ws.conn.commit()

        page = WebQueryService(ws.conn).get_review_page()
        new_person_queue = next(queue for queue in page["queues"] if queue["review_type"] == "new_person")
        grouped_item = next(
            item
            for item in new_person_queue["items"]
            if set(item["review_ids"]) == {review_a, review_b, review_c, review_d}
        )

        assert new_person_queue["raw_count"] == 6
        assert new_person_queue["count"] == 3
        assert grouped_item["observation_label"] == "4 张样本"
        assert grouped_item["review_label"] == f"review #{review_a} 等 4 条"
        assert grouped_item["preview_total_count"] == 4
        assert grouped_item["preview_visible_count"] == 3
        assert grouped_item["preview_summary"] == "预览 3 / 4"
        assert len(grouped_item["preview_faces"]) == 4

        client = TestClient(create_app(workspace=ws.root))
        html = client.get("/reviews").text

        assert "已自动归成" in html
        assert f"review #{review_a} 等 4 条" in html
        assert "预览 3 / 4" in html
        assert 'data-preview-shift="-1"' in html
        assert 'data-preview-shift="1"' in html
        assert f'data-review-ids="{review_a},{review_b},{review_c},{review_d}"' in html
        assert f"review #{review_e}" in html
    finally:
        ws.close()


def test_reviews_page_sorts_new_person_queue_by_sample_count_desc(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        review_small_a, _ = _append_new_person_review(
            ws,
            file_name="small-cluster-a.jpg",
            vector=np.asarray([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
        )
        review_small_b, _ = _append_new_person_review(
            ws,
            file_name="small-cluster-b.jpg",
            vector=np.asarray([0.98, 0.18, 0.0, 0.0], dtype=np.float32),
        )
        review_large_a, _ = _append_new_person_review(
            ws,
            file_name="large-cluster-a.jpg",
            vector=np.asarray([-1.0, 0.0, 0.0, 0.0], dtype=np.float32),
        )
        review_large_b, _ = _append_new_person_review(
            ws,
            file_name="large-cluster-b.jpg",
            vector=np.asarray([-0.98, -0.18, 0.0, 0.0], dtype=np.float32),
        )
        review_large_c, _ = _append_new_person_review(
            ws,
            file_name="large-cluster-c.jpg",
            vector=np.asarray([-0.96, -0.27, 0.0, 0.0], dtype=np.float32),
        )
        ws.conn.commit()

        page = WebQueryService(ws.conn).get_review_page()
        new_person_queue = next(queue for queue in page["queues"] if queue["review_type"] == "new_person")

        ordered_review_groups = [set(item["review_ids"]) for item in new_person_queue["items"]]
        assert ordered_review_groups[0] == {review_large_a, review_large_b, review_large_c}
        assert ordered_review_groups[1] == {review_small_a, review_small_b}
        assert len(ordered_review_groups[2]) == 1

        client = TestClient(create_app(workspace=ws.root))
        html = client.get("/reviews").text
        large_label = f"review #{review_large_a} 等 3 条"
        small_label = f"review #{review_small_a} 等 2 条"

        assert large_label in html
        assert small_label in html
        assert html.index(large_label) < html.index(small_label)
    finally:
        ws.close()


def test_reviews_fall_back_to_active_cover_when_review_observation_is_inactive(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_export_assets=True)
    try:
        review_row = ws.conn.execute(
            """
            SELECT id, primary_person_id
            FROM review_item
            WHERE review_type = 'new_person'
            ORDER BY id ASC
            LIMIT 1
            """
        ).fetchone()
        assert review_row is not None
        assert review_row["primary_person_id"] is not None

        service = WebQueryService(ws.conn)
        primary_person = next(
            person
            for person in service.list_people()
            if int(person["id"]) == int(review_row["primary_person_id"])
        )
        cover_observation_id = int(primary_person["cover_observation_id"])
        cover_photo_id = ws.conn.execute(
            "SELECT photo_asset_id FROM face_observation WHERE id = ?",
            (cover_observation_id,),
        ).fetchone()["photo_asset_id"]

        inactive_observation_id = ws.conn.execute(
            """
            INSERT INTO face_observation(
                photo_asset_id,
                bbox_top,
                bbox_right,
                bbox_bottom,
                bbox_left,
                active
            )
            VALUES (?, 0.15, 0.45, 0.55, 0.05, 0)
            RETURNING id
            """,
            (int(cover_photo_id),),
        ).fetchone()["id"]
        ws.conn.execute(
            "UPDATE review_item SET face_observation_id = ? WHERE id = ?",
            (int(inactive_observation_id), int(review_row["id"])),
        )
        ws.conn.commit()

        page = WebQueryService(ws.conn).get_review_page()
        new_person_queue = next(queue for queue in page["queues"] if queue["review_type"] == "new_person")
        item = next(queue_item for queue_item in new_person_queue["items"] if int(queue_item["id"]) == int(review_row["id"]))

        assert item["viewer_index"] is not None
        viewer_item = page["viewer_items"][int(item["viewer_index"])]
        assert viewer_item["crop_url"] == f"/api/observations/{cover_observation_id}/crop"
        assert viewer_item["context_url"] == f"/api/observations/{cover_observation_id}/context"
    finally:
        ws.close()


def test_reviews_possible_merge_preview_faces_bind_distinct_viewer_targets(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path, seed_export_assets=True)
    try:
        client = TestClient(create_app(workspace=ws.root))
        html = client.get("/reviews").text
        block_match = re.search(
            r'<details\s+id="queue-possible_merge".*?</details>',
            html,
            flags=re.DOTALL,
        )
        assert block_match is not None
        block = block_match.group(0)

        assert 'data-preview-count="2"' in block
        indices = re.findall(
            r'class="queue-face".*?data-review-focus-index="(\d+)"',
            block,
            flags=re.DOTALL,
        )
        # 两个预览缩略图应能切换到不同证据，而不是都映射到同一 index。
        assert len(set(indices)) >= 2
    finally:
        ws.close()


def test_sources_exports_logs_pages_bind_real_data(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        client = TestClient(create_app(workspace=ws.root))
        sources_html = client.get("/sources").text
        exports_html = client.get("/exports").text
        logs_html = client.get("/logs").text

        for source in ws.source_repo.list_sources(active=True):
            assert str(source["name"]) in sources_html
        assert "paused" in sources_html

        template = ws.export_repo.get_template(ws.export_template_id)
        assert template is not None
        assert str(template["name"]) in exports_html
        assert "新建模板" in exports_html
        assert "管理已有模板" in exports_html
        assert 'data-export-form' in exports_html
        assert 'data-export-mode="create"' in exports_html
        assert 'data-export-mode="update"' in exports_html
        assert 'data-action="export-delete-template"' in exports_html

        assert "seed_ready" in logs_html
    finally:
        ws.close()


def test_pages_render_empty_state_with_fresh_workspace(tmp_path) -> None:
    client = TestClient(create_app(workspace=tmp_path))

    people_html = client.get("/").text
    reviews_html = client.get("/reviews").text
    sources_html = client.get("/sources").text
    exports_html = client.get("/exports").text
    logs_html = client.get("/logs").text

    assert "人物库" in people_html
    assert "共 0 人" in people_html
    assert "暂时还没有人物卡片" in people_html
    assert "去管理源目录与扫描" in people_html
    assert 'data-viewer-layer="original"' not in people_html

    assert "待审核" in reviews_html
    assert "review-hero" in reviews_html
    assert "review-inspector" in reviews_html
    assert "queue-new_person" in reviews_html
    assert "当前队列为空" in reviews_html

    assert "源目录与扫描" in sources_html
    assert "当前会话：idle" in sources_html
    assert "已注册源目录（0）" in sources_html

    assert "导出模板" in exports_html
    assert "新建模板" in exports_html
    assert "输出目录" in exports_html

    assert "运行日志" in logs_html
    assert "事件类型" in logs_html


def test_people_page_empty_state_hides_random_samples(tmp_path) -> None:
    client = TestClient(create_app(workspace=tmp_path))
    html = client.get("/").text

    assert "person-empty-state" in html
    assert "暂时还没有人物卡片" in html
    assert "person-grid" not in html
    assert "person-card" not in html
    assert "people-gallery-viewer" not in html
    assert 'data-action="viewer-next"' not in html


def test_sources_page_keeps_latest_completed_session_visible(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source_root = tmp_path / "scan-input"
    source_root.mkdir(parents=True, exist_ok=True)
    copy_raw_face_image(source_root / "a.jpg", index=0)

    assert main(["init", "--workspace", str(workspace)]) == 0
    assert (
        main(
            [
                "source",
                "add",
                "--workspace",
                str(workspace),
                "--name",
                "scan-source",
                "--root-path",
                str(source_root),
            ]
        )
        == 0
    )
    assert main(["scan", "--workspace", str(workspace)]) == 0

    client = TestClient(create_app(workspace=workspace))
    html = client.get("/sources").text

    assert "当前会话：" in html
    assert "completed" in html
    assert "scan-source" in html
    assert ">1<" in html
