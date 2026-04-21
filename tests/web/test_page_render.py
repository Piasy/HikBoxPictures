from __future__ import annotations

import sqlite3
from pathlib import Path

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.export import ensure_export_schema
from hikbox_pictures.web.app import ServiceContainer, create_app

NOW = "2026-04-22T00:00:00+00:00"


def _build_client(tmp_path: Path) -> tuple[TestClient, Path]:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    app = create_app(ServiceContainer.from_library_db(layout.library_db_path))
    return TestClient(app), layout.library_db_path


def _insert_scan_session(db_path: Path, *, status: str) -> int:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO scan_session(
              run_kind,
              status,
              triggered_by,
              resume_from_session_id,
              started_at,
              finished_at,
              last_error,
              created_at,
              updated_at
            )
            VALUES ('scan_full', ?, 'manual_cli', NULL, ?, NULL, NULL, ?, ?)
            """,
            (status, NOW, NOW, NOW),
        )
        conn.commit()
        return int(cursor.lastrowid)


def _seed_sources_audit_data(db_path: Path) -> int:
    session_id = _insert_scan_session(db_path, status="running")
    with sqlite3.connect(db_path) as conn:
        src1 = conn.execute(
            """
            INSERT INTO library_source(root_path, label, enabled, status, last_discovered_at, created_at, updated_at)
            VALUES ('/tmp/src-1', '源一', 1, 'active', NULL, ?, ?)
            """,
            (NOW, NOW),
        ).lastrowid
        src2 = conn.execute(
            """
            INSERT INTO library_source(root_path, label, enabled, status, last_discovered_at, created_at, updated_at)
            VALUES ('/tmp/src-2', '源二', 1, 'active', NULL, ?, ?)
            """,
            (NOW, NOW),
        ).lastrowid

        def insert_photo(source_id: int, idx: int) -> int:
            return int(
                conn.execute(
                    """
                    INSERT INTO photo_asset(
                      library_source_id,
                      primary_path,
                      primary_fingerprint,
                      fingerprint_algo,
                      file_size,
                      mtime_ns,
                      capture_datetime,
                      capture_month,
                      is_live_photo,
                      live_mov_path,
                      live_mov_size,
                      live_mov_mtime_ns,
                      asset_status,
                      created_at,
                      updated_at
                    )
                    VALUES (?, ?, ?, 'sha256', 100, 1710000000000000000, NULL, NULL, 0, NULL, NULL, NULL, 'active', ?, ?)
                    """,
                    (source_id, f"img-{source_id}-{idx}.jpg", f"fp-{source_id}-{idx}", NOW, NOW),
                ).lastrowid
            )

        p1 = insert_photo(int(src1), 1)
        p2 = insert_photo(int(src1), 2)
        p3 = insert_photo(int(src2), 3)
        p4 = insert_photo(int(src2), 4)

        def insert_batch(batch_id: int, photo_id: int, item_order: int, status: str, err: str | None) -> None:
            conn.execute(
                """
                INSERT INTO scan_batch_item(scan_batch_id, photo_asset_id, item_order, status, error_message, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (batch_id, photo_id, item_order, status, err, NOW),
            )

        b1 = int(
            conn.execute(
                """
                INSERT INTO scan_batch(scan_session_id, stage, worker_slot, claim_token, status, retry_count, claimed_at, started_at, acked_at, error_message)
                VALUES (?, 'detect', 0, 't1', 'acked', 0, ?, ?, ?, NULL)
                """,
                (session_id, NOW, NOW, NOW),
            ).lastrowid
        )
        b2 = int(
            conn.execute(
                """
                INSERT INTO scan_batch(scan_session_id, stage, worker_slot, claim_token, status, retry_count, claimed_at, started_at, acked_at, error_message)
                VALUES (?, 'detect', 1, 't2', 'acked', 0, ?, ?, ?, NULL)
                """,
                (session_id, NOW, NOW, NOW),
            ).lastrowid
        )
        insert_batch(b1, p1, 0, "done", None)
        insert_batch(b1, p2, 1, "failed", "bad")
        insert_batch(b2, p3, 0, "done", None)
        insert_batch(b2, p4, 1, "failed", "bad")

        conn.commit()
    return session_id


def _seed_exports_data(db_path: Path, tmp_path: Path) -> list[int]:
    with sqlite3.connect(db_path) as conn:
        ensure_export_schema(conn)
        person_1 = int(
            conn.execute(
                """
                INSERT INTO person(person_uuid, display_name, is_named, status, merged_into_person_id, created_at, updated_at)
                VALUES ('00000000-0000-0000-0000-000000000501', '张三', 1, 'active', NULL, ?, ?)
                """,
                (NOW, NOW),
            ).lastrowid
        )
        person_2 = int(
            conn.execute(
                """
                INSERT INTO person(person_uuid, display_name, is_named, status, merged_into_person_id, created_at, updated_at)
                VALUES ('00000000-0000-0000-0000-000000000502', '李四', 1, 'active', NULL, ?, ?)
                """,
                (NOW, NOW),
            ).lastrowid
        )

        t1 = int(
            conn.execute(
                """
                INSERT INTO export_template(name, output_root, enabled, created_at, updated_at)
                VALUES ('模板一', ?, 1, ?, ?)
                """,
                (str((tmp_path / "out-1").resolve()), NOW, NOW),
            ).lastrowid
        )
        t2 = int(
            conn.execute(
                """
                INSERT INTO export_template(name, output_root, enabled, created_at, updated_at)
                VALUES ('模板二', ?, 1, ?, ?)
                """,
                (str((tmp_path / "out-2").resolve()), NOW, NOW),
            ).lastrowid
        )
        conn.execute(
            "INSERT INTO export_template_person(template_id, person_id, created_at) VALUES (?, ?, ?)",
            (t1, person_1, NOW),
        )
        conn.execute(
            "INSERT INTO export_template_person(template_id, person_id, created_at) VALUES (?, ?, ?)",
            (t2, person_2, NOW),
        )

        conn.execute(
            """
            INSERT INTO export_run(template_id, status, summary_json, started_at, finished_at)
            VALUES (?, 'completed', '{"exported":4,"skipped_exists":1,"failed":0}', ?, ?)
            """,
            (t2, NOW, NOW),
        )
        running = int(
            conn.execute(
                """
                INSERT INTO export_run(template_id, status, summary_json, started_at, finished_at)
                VALUES (?, 'running', '{"exported":0,"skipped_exists":0,"failed":0}', ?, NULL)
                """,
                (t1, NOW),
            ).lastrowid
        )

        src = int(
            conn.execute(
                """
                INSERT INTO library_source(root_path, label, enabled, status, last_discovered_at, created_at, updated_at)
                VALUES ('/tmp/export-src', '导出源', 1, 'active', NULL, ?, ?)
                """,
                (NOW, NOW),
            ).lastrowid
        )

        def add_photo(name: str) -> int:
            return int(
                conn.execute(
                    """
                    INSERT INTO photo_asset(
                      library_source_id,
                      primary_path,
                      primary_fingerprint,
                      fingerprint_algo,
                      file_size,
                      mtime_ns,
                      capture_datetime,
                      capture_month,
                      is_live_photo,
                      live_mov_path,
                      live_mov_size,
                      live_mov_mtime_ns,
                      asset_status,
                      created_at,
                      updated_at
                    )
                    VALUES (?, ?, ?, 'sha256', 100, 1710000000000000000, NULL, NULL, 0, NULL, NULL, NULL, 'active', ?, ?)
                    """,
                    (src, name, f"fp-{name}", NOW, NOW),
                ).lastrowid
            )

        p1 = add_photo("only-a.jpg")
        p2 = add_photo("group-a.jpg")
        p3 = add_photo("group-b.jpg")

        def add_face(photo_id: int, idx: int) -> int:
            return int(
                conn.execute(
                    """
                    INSERT INTO face_observation(
                      photo_asset_id,
                      face_index,
                      crop_relpath,
                      aligned_relpath,
                      context_relpath,
                      bbox_x1,
                      bbox_y1,
                      bbox_x2,
                      bbox_y2,
                      detector_confidence,
                      face_area_ratio,
                      magface_quality,
                      quality_score,
                      active,
                      inactive_reason,
                      pending_reassign,
                      created_at,
                      updated_at
                    )
                    VALUES (?, ?, 'crop/x.jpg', 'aligned/x.jpg', 'context/x.jpg', 0.1, 0.1, 0.9, 0.9, 0.9, 0.2, 30.0, 0.9, 1, NULL, 0, ?, ?)
                    """,
                    (photo_id, idx, NOW, NOW),
                ).lastrowid
            )

        f1 = add_face(p1, 0)
        f2 = add_face(p2, 1)
        f3 = add_face(p2, 2)
        f4 = add_face(p3, 3)

        scan_session = int(
            conn.execute(
                """
                INSERT INTO scan_session(run_kind,status,triggered_by,resume_from_session_id,started_at,finished_at,last_error,created_at,updated_at)
                VALUES ('scan_full','completed','manual_cli',NULL,?,?,NULL,?,?)
                """,
                (NOW, NOW, NOW, NOW),
            ).lastrowid
        )
        run = int(
            conn.execute(
                """
                INSERT INTO assignment_run(scan_session_id,algorithm_version,param_snapshot_json,run_kind,started_at,finished_at,status)
                VALUES (?, 'v5.2026-04-21', '{}', 'scan_full', ?, ?, 'completed')
                """,
                (scan_session, NOW, NOW),
            ).lastrowid
        )
        conn.execute(
            "INSERT INTO person_face_assignment(person_id,face_observation_id,assignment_run_id,assignment_source,active,confidence,margin,created_at,updated_at) VALUES (?, ?, ?, 'hdbscan', 1, 0.9, 0.1, ?, ?)",
            (person_1, f1, run, NOW, NOW),
        )
        conn.execute(
            "INSERT INTO person_face_assignment(person_id,face_observation_id,assignment_run_id,assignment_source,active,confidence,margin,created_at,updated_at) VALUES (?, ?, ?, 'hdbscan', 1, 0.9, 0.1, ?, ?)",
            (person_1, f2, run, NOW, NOW),
        )
        conn.execute(
            "INSERT INTO person_face_assignment(person_id,face_observation_id,assignment_run_id,assignment_source,active,confidence,margin,created_at,updated_at) VALUES (?, ?, ?, 'hdbscan', 1, 0.9, 0.1, ?, ?)",
            (person_2, f3, run, NOW, NOW),
        )
        conn.execute(
            "INSERT INTO person_face_assignment(person_id,face_observation_id,assignment_run_id,assignment_source,active,confidence,margin,created_at,updated_at) VALUES (?, ?, ?, 'hdbscan', 1, 0.9, 0.1, ?, ?)",
            (person_2, f4, run, NOW, NOW),
        )
        conn.commit()

    return [t1, t2, running]


def test_sources_audit_page_binds_session_status_source_progress_failure_stats_and_scan_params(tmp_path: Path) -> None:
    client, db_path = _build_client(tmp_path)
    session_id = _seed_sources_audit_data(db_path)

    resp = client.get(f"/sources/{session_id}/audit")
    dom = BeautifulSoup(resp.text, "html.parser")

    session_node = dom.select_one('[data-testid="scan-session-state"]')
    assert session_node is not None
    assert session_node["data-session-id"] == str(session_id)
    assert session_node["data-status"] == "running"
    assert session_node["data-failed-count"] == "2"

    progress_rows = dom.select('[data-testid="source-progress-row"]')
    assert len(progress_rows) == 2
    assert {row["data-source-id"] for row in progress_rows} == {"1", "2"}
    row_by_source_id = {row["data-source-id"]: row for row in progress_rows}
    assert row_by_source_id["1"]["data-processed"] == "2"
    assert row_by_source_id["1"]["data-total"] == "2"
    assert row_by_source_id["2"]["data-processed"] == "2"
    assert row_by_source_id["2"]["data-total"] == "2"

    params = dom.select_one('[data-testid="scan-params"]')
    assert params is not None
    assert params["data-det-size"] == "640"
    assert params["data-workers"].isdigit()
    assert params["data-batch-size"] == "300"


def test_sources_audit_page_binds_resume_abort_abandon_new_action_states(tmp_path: Path) -> None:
    client, db_path = _build_client(tmp_path)
    session_id = _seed_sources_audit_data(db_path)

    resp = client.get(f"/sources/{session_id}/audit")
    dom = BeautifulSoup(resp.text, "html.parser")

    assert dom.select_one('[data-testid="scan-action-resume"]')["data-enabled"] == "false"
    assert dom.select_one('[data-testid="scan-action-abort"]')["data-enabled"] == "true"
    assert dom.select_one('[data-testid="scan-action-abandon-new"]')["data-enabled"] == "true"


def test_exports_page_binds_template_list_create_edit_preview_history_and_people_lock_semantics(tmp_path: Path) -> None:
    client, db_path = _build_client(tmp_path)
    template_ids = _seed_exports_data(db_path, tmp_path)

    resp = client.get("/exports")
    dom = BeautifulSoup(resp.text, "html.parser")

    template_rows = dom.select('[data-testid="export-template-row"]')
    assert [row["data-template-id"] for row in template_rows] == [str(template_ids[0]), str(template_ids[1])]
    assert dom.select_one('[data-testid="export-template-create"]')["data-enabled"] == "true"
    assert dom.select_one(f'[data-testid="export-template-edit-{template_ids[0]}"]')["data-enabled"] == "true"

    only_stats = dom.select_one('[data-testid="preview-only-stats"]')
    group_stats = dom.select_one('[data-testid="preview-group-stats"]')
    assert only_stats is not None
    assert group_stats is not None
    assert int(only_stats["data-candidate-count"]) >= 1
    assert int(group_stats["data-candidate-count"]) >= 1

    samples = dom.select('[data-testid="preview-sample-item"]')
    assert len(samples) >= 2

    history_rows = dom.select('[data-testid="export-run-history-row"]')
    assert history_rows[0]["data-status"] == "running"
    assert dom.select_one('[data-testid="people-assign-action"]')["data-enabled"] == "false"
    assert dom.select_one('[data-testid="people-merge-action"]')["data-enabled"] == "false"
    lock_tip = dom.select_one('[data-testid="people-write-lock-tip"]')
    assert lock_tip is not None
    assert lock_tip["data-locked"] == "true"
    assert "导出进行中，暂不可修改" in lock_tip.text
