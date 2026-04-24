from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from hikbox_pictures.product.config import WorkspaceLayout
from hikbox_pictures.product.service_registry import build_service_container
from hikbox_pictures.web.app import create_app
from tests.product.task6_test_support import create_task6_workspace, seed_face_observations


@pytest.fixture()
def page_env(tmp_path: Path) -> tuple[TestClient, WorkspaceLayout, int, list[int], int, int]:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    face_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (210, 180, 160)},
            {"asset_index": 0, "color": (220, 190, 170)},
            {"asset_index": 1, "color": (180, 180, 210)},
        ],
    )
    named_person_id, anonymous_person_id, export_run_id = _seed_page_data(layout, session_id, face_ids)
    client = TestClient(create_app(build_service_container(layout)))
    return client, layout, session_id, face_ids, named_person_id, export_run_id


def test_home_page_binds_named_anonymous_sections_without_search_and_merge_controls(
    page_env: tuple[TestClient, WorkspaceLayout, int, list[int], int, int],
) -> None:
    client, _layout, _session_id, _face_ids, _named_person_id, _export_run_id = page_env

    response = client.get("/")
    dom = BeautifulSoup(response.text, "html.parser")

    assert response.status_code == 200
    assert dom.select_one('[data-testid="named-people-section"]') is not None
    assert dom.select_one('[data-testid="anonymous-people-section"]') is not None
    assert dom.select_one('[data-testid="people-search-form"]') is None
    assert dom.select_one('[data-testid="merge-selected-action"]')["data-enabled"] == "true"
    assert dom.select_one('[data-testid="undo-last-merge-action"]')["data-enabled"] == "true"


def test_people_detail_page_uses_review_style_reimplementation_and_expand_exclude_controls(
    page_env: tuple[TestClient, WorkspaceLayout, int, list[int], int, int],
) -> None:
    client, _layout, _session_id, face_ids, named_person_id, _export_run_id = page_env

    response = client.get(f"/people/{named_person_id}")
    dom = BeautifulSoup(response.text, "html.parser")

    assert response.status_code == 200
    assert dom.select_one('[data-testid="person-detail-topbar"]') is not None
    assert dom.select_one('[data-testid="person-detail-panel"]') is not None
    assert dom.select_one('[data-testid="person-samples"]')["class"] == ["face-grid"]
    sample_cards = dom.select('[data-testid="person-sample-card"]')
    assert sample_cards[0]["id"] == f"sample-{face_ids[0]}"
    assert sample_cards[0]["data-default-view"] == "context"
    assert sample_cards[0]["data-live"] == "true"
    assert sample_cards[0].select_one('[data-testid="sample-context-link"]') is not None
    assert sample_cards[0].select_one('[data-testid="sample-thumb-grid"]')["class"] == ["thumb-grid"]
    assert sample_cards[0].select_one('[data-testid="sample-expand-toggle"]')["data-target"] == "crop-context"
    assert sample_cards[0].select_one('[data-testid="sample-exclude-action"]')["data-face-observation-id"] == str(face_ids[0])
    assert dom.select_one('[data-testid="sample-batch-exclude-action"]')["data-selected-count"] == "2"


def test_sources_audit_page_binds_session_status_source_progress_failure_stats_and_scan_params(
    page_env: tuple[TestClient, WorkspaceLayout, int, list[int], int, int],
) -> None:
    client, _layout, session_id, face_ids, named_person_id, _export_run_id = page_env

    response = client.get(f"/sources/{session_id}/audit")
    dom = BeautifulSoup(response.text, "html.parser")

    session_node = dom.select_one('[data-testid="scan-session-state"]')
    assert session_node["data-session-id"] == str(session_id)
    assert session_node["data-status"] == "running"
    assert session_node["data-failed-count"] == "1"
    progress_rows = dom.select('[data-testid="source-progress-row"]')
    assert len(progress_rows) == 1
    assert progress_rows[0]["data-source-id"] == "1"
    assert progress_rows[0]["data-processed"] == "2"
    assert progress_rows[0]["data-total"] == "2"
    params = dom.select_one('[data-testid="scan-params"]')
    assert params["data-det-size"] == "640"
    assert params["data-workers"] == "4"
    assert params["data-batch-size"] == "300"
    assert dom.select_one('[data-testid="scan-action-resume"]')["data-enabled"] == "false"
    assert dom.select_one('[data-testid="scan-action-abort"]')["data-enabled"] == "true"
    assert dom.select_one('[data-testid="scan-action-abandon-new"]')["data-enabled"] == "true"
    jump_link = dom.select_one('[data-testid="audit-jump-to-person"]')
    assert jump_link["href"] == f"/people/{named_person_id}#sample-{face_ids[0]}"


def test_exports_page_binds_template_list_preview_history_and_people_lock_semantics(
    page_env: tuple[TestClient, WorkspaceLayout, int, list[int], int, int],
) -> None:
    client, _layout, _session_id, _face_ids, named_person_id, export_run_id = page_env

    response = client.get("/exports")
    dom = BeautifulSoup(response.text, "html.parser")

    template_rows = dom.select('[data-testid="export-template-row"]')
    assert [row["data-template-id"] for row in template_rows] == ["1", "2"]
    assert dom.select_one('[data-testid="export-template-create"]')["data-enabled"] == "true"
    assert dom.select_one('[data-testid="export-template-edit-1"]')["data-enabled"] == "true"
    only_stats = dom.select_one('[data-testid="preview-only-stats"]')
    group_stats = dom.select_one('[data-testid="preview-group-stats"]')
    assert only_stats["data-candidate-count"] == "1"
    assert group_stats["data-candidate-count"] == "1"
    samples = dom.select('[data-testid="preview-sample-item"]')
    assert len(samples) == 2
    sample_paths = {sample.text.strip() for sample in samples}
    assert "partial.jpg" not in sample_paths
    history_rows = dom.select('[data-testid="export-run-history-row"]')
    assert history_rows[0]["data-export-run-id"] == str(export_run_id)
    assert history_rows[0]["data-status"] == "running"
    assert dom.select_one('[data-testid="people-assign-action"]')["data-enabled"] == "false"
    assert dom.select_one('[data-testid="people-merge-action"]')["data-enabled"] == "false"
    lock_tip = dom.select_one('[data-testid="people-write-lock-tip"]')
    assert lock_tip["data-locked"] == "true"
    assert "导出运行中" in lock_tip.text
    assert str(named_person_id) in response.text


def test_logs_page_binds_run_filters_and_rows(
    page_env: tuple[TestClient, WorkspaceLayout, int, list[int], int, int],
) -> None:
    client, _layout, session_id, _face_ids, _named_person_id, export_run_id = page_env

    response = client.get(f"/logs?scan_session_id={session_id}&export_run_id={export_run_id}&severity=warning")
    dom = BeautifulSoup(response.text, "html.parser")

    filters = dom.select_one('[data-testid="logs-filter"]')
    assert filters["data-scan-session-id"] == str(session_id)
    assert filters["data-export-run-id"] == str(export_run_id)
    assert filters["data-severity"] == "warning"
    rows = dom.select('[data-testid="log-row"]')
    assert len(rows) == 1
    assert rows[0]["data-scan-session-id"] == str(session_id)
    assert rows[0]["data-export-run-id"] == str(export_run_id)
    assert rows[0]["data-severity"] == "warning"


def _seed_page_data(layout: WorkspaceLayout, session_id: int, face_ids: list[int]) -> tuple[int, int, int]:
    conn = sqlite3.connect(layout.library_db)
    try:
        named_person_id = int(
            conn.execute(
                """
                INSERT INTO person(person_uuid, display_name, is_named, status, created_at, updated_at)
                VALUES (?, 'Alice', 1, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (str(uuid.uuid4()),),
            ).lastrowid
        )
        anonymous_person_id = int(
            conn.execute(
                """
                INSERT INTO person(person_uuid, display_name, is_named, status, created_at, updated_at)
                VALUES (?, NULL, 0, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (str(uuid.uuid4()),),
            ).lastrowid
        )
        second_named_person_id = int(
            conn.execute(
                """
                INSERT INTO person(person_uuid, display_name, is_named, status, created_at, updated_at)
                VALUES (?, 'Bob', 1, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (str(uuid.uuid4()),),
            ).lastrowid
        )
        third_named_person_id = int(
            conn.execute(
                """
                INSERT INTO person(person_uuid, display_name, is_named, status, created_at, updated_at)
                VALUES (?, 'Carol', 1, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (str(uuid.uuid4()),),
            ).lastrowid
        )
        conn.execute("UPDATE photo_asset SET is_live_photo=1, live_mov_path='live.mov' WHERE id=1")
        conn.execute("UPDATE photo_asset SET is_live_photo=0 WHERE id=2")
        partial_asset_id = int(
            conn.execute(
                """
                INSERT INTO photo_asset(
                  library_source_id, primary_path, primary_fingerprint, fingerprint_algo, file_size, mtime_ns, asset_status,
                  created_at, updated_at
                ) VALUES (1, 'partial.jpg', 'fp-partial', 'sha256', 120, 210, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            ).lastrowid
        )
        group_asset_id = int(
            conn.execute(
                """
                INSERT INTO photo_asset(
                  library_source_id, primary_path, primary_fingerprint, fingerprint_algo, file_size, mtime_ns, asset_status,
                  created_at, updated_at
                ) VALUES (1, 'group.jpg', 'fp-group', 'sha256', 130, 220, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            ).lastrowid
        )
        only_asset_id = int(
            conn.execute(
                """
                INSERT INTO photo_asset(
                  library_source_id, primary_path, primary_fingerprint, fingerprint_algo, file_size, mtime_ns, asset_status,
                  created_at, updated_at
                ) VALUES (1, 'only.jpg', 'fp-only', 'sha256', 140, 230, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            ).lastrowid
        )
        assignment_run_id = int(
            conn.execute(
                """
                INSERT INTO assignment_run(
                  scan_session_id, algorithm_version, param_snapshot_json, run_kind, started_at, finished_at, status, updated_at
                ) VALUES (?, 'frozen_v5', ?, 'scan_full', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'completed', CURRENT_TIMESTAMP)
                """,
                (
                    session_id,
                    json.dumps({"det_size": 640, "workers": 4, "batch_size": 300}, ensure_ascii=False),
                ),
            ).lastrowid
        )
        conn.executemany(
            """
            INSERT INTO person_face_assignment(
              person_id, face_observation_id, assignment_run_id, assignment_source, active, confidence, margin, created_at, updated_at
            ) VALUES (?, ?, ?, 'hdbscan', 1, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            [
                (named_person_id, face_ids[0], assignment_run_id, 0.99, 0.40),
                (anonymous_person_id, face_ids[1], assignment_run_id, 0.95, 0.20),
                (named_person_id, face_ids[2], assignment_run_id, 0.91, 0.10),
            ],
        )
        partial_face_id = int(
            conn.execute(
                """
                INSERT INTO face_observation(
                  photo_asset_id, face_index, crop_relpath, aligned_relpath, context_relpath,
                  bbox_x1, bbox_y1, bbox_x2, bbox_y2,
                  detector_confidence, face_area_ratio, magface_quality, quality_score,
                  active, inactive_reason, pending_reassign, created_at, updated_at
                ) VALUES (?, 1, 'artifacts/crops/partial.jpg', 'artifacts/aligned/partial.png', 'artifacts/context/partial.jpg',
                  10, 10, 80, 80, 0.92, 0.25, 1.2, 0.6, 1, NULL, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (partial_asset_id,),
            ).lastrowid
        )
        group_face_ids = [
            int(
                conn.execute(
                    """
                    INSERT INTO face_observation(
                      photo_asset_id, face_index, crop_relpath, aligned_relpath, context_relpath,
                      bbox_x1, bbox_y1, bbox_x2, bbox_y2,
                      detector_confidence, face_area_ratio, magface_quality, quality_score,
                      active, inactive_reason, pending_reassign, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0.92, 0.25, 1.2, 0.6, 1, NULL, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (
                        group_asset_id,
                        index,
                        f"artifacts/crops/group-{index}.jpg",
                        f"artifacts/aligned/group-{index}.png",
                        f"artifacts/context/group-{index}.jpg",
                        *bbox,
                    ),
                ).lastrowid
            )
            for index, bbox in enumerate(
                [
                    (10, 10, 80, 80),
                    (90, 10, 160, 80),
                    (10, 90, 200, 220),
                ],
                start=1,
            )
        ]
        only_face_ids = [
            int(
                conn.execute(
                    """
                    INSERT INTO face_observation(
                      photo_asset_id, face_index, crop_relpath, aligned_relpath, context_relpath,
                      bbox_x1, bbox_y1, bbox_x2, bbox_y2,
                      detector_confidence, face_area_ratio, magface_quality, quality_score,
                      active, inactive_reason, pending_reassign, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0.92, 0.25, 1.2, 0.6, 1, NULL, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (
                        only_asset_id,
                        index,
                        f"artifacts/crops/only-{index}.jpg",
                        f"artifacts/aligned/only-{index}.png",
                        f"artifacts/context/only-{index}.jpg",
                        *bbox,
                    ),
                ).lastrowid
            )
            for index, bbox in enumerate(
                [
                    (10, 10, 80, 80),
                    (90, 10, 160, 80),
                ],
                start=1,
            )
        ]
        conn.executemany(
            """
            INSERT INTO person_face_assignment(
              person_id, face_observation_id, assignment_run_id, assignment_source, active, confidence, margin, created_at, updated_at
            ) VALUES (?, ?, ?, 'hdbscan', 1, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            [
                (second_named_person_id, partial_face_id, assignment_run_id, 0.88, 0.08),
                (second_named_person_id, group_face_ids[0], assignment_run_id, 0.87, 0.07),
                (third_named_person_id, group_face_ids[1], assignment_run_id, 0.86, 0.06),
                (anonymous_person_id, group_face_ids[2], assignment_run_id, 0.85, 0.05),
                (second_named_person_id, only_face_ids[0], assignment_run_id, 0.84, 0.04),
                (third_named_person_id, only_face_ids[1], assignment_run_id, 0.83, 0.03),
            ],
        )
        conn.execute(
            """
            INSERT INTO scan_audit_item(
              scan_session_id, assignment_run_id, audit_type, face_observation_id, person_id, evidence_json, created_at
            ) VALUES (?, ?, 'reassign_after_exclusion', ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                session_id,
                assignment_run_id,
                face_ids[0],
                named_person_id,
                json.dumps({"person_id": named_person_id, "face_observation_id": face_ids[0]}, ensure_ascii=False),
            ),
        )
        conn.execute(
            """
            INSERT INTO export_template(name, output_root, enabled, created_at, updated_at)
            VALUES ('模板一', ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (str(layout.workspace_root / "exports-a"),),
        )
        conn.execute(
            """
            INSERT INTO export_template(name, output_root, enabled, created_at, updated_at)
            VALUES ('模板二', ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (str(layout.workspace_root / "exports-b"),),
        )
        conn.execute(
            """
            INSERT INTO export_template_person(template_id, person_id, created_at)
            VALUES (1, ?, CURRENT_TIMESTAMP), (1, ?, CURRENT_TIMESTAMP), (2, ?, CURRENT_TIMESTAMP)
            """,
            (second_named_person_id, third_named_person_id, named_person_id),
        )
        export_run_id = int(
            conn.execute(
                """
                INSERT INTO export_run(template_id, status, summary_json, started_at, finished_at)
                VALUES (1, 'running', ?, CURRENT_TIMESTAMP, NULL)
                """,
                (
                    json.dumps(
                        {"exported_count": 0, "skipped_exists_count": 0, "failed_count": 0},
                        ensure_ascii=False,
                    ),
                ),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO ops_event(event_type, severity, scan_session_id, export_run_id, payload_json, created_at)
            VALUES ('export.progress', 'warning', ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                session_id,
                export_run_id,
                json.dumps({"message": "warning"}, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return named_person_id, anonymous_person_id, export_run_id
