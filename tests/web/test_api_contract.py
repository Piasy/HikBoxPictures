from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hikbox_pictures.product.config import WorkspaceLayout, initialize_workspace
from hikbox_pictures.product.service_registry import build_service_container
from hikbox_pictures.web.app import create_app
from tests.product.task6_test_support import create_task6_workspace, seed_face_observations


@pytest.fixture()
def api_env(tmp_path: Path) -> tuple[TestClient, WorkspaceLayout, dict[str, int | list[int]]]:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    face_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (210, 180, 160)},
            {"asset_index": 0, "color": (220, 190, 170)},
            {"asset_index": 1, "color": (180, 180, 210)},
            {"asset_index": 1, "color": (170, 170, 220), "pending_reassign": True},
        ],
    )
    seeded = _seed_api_data(layout, session_id, face_ids)
    _execute_sql(
        layout.library_db,
        "UPDATE scan_session SET status='completed', finished_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (session_id,),
    )
    client = TestClient(create_app(build_service_container(layout)))
    return client, layout, seeded


@pytest.fixture()
def scan_api_env(tmp_path: Path) -> tuple[TestClient, WorkspaceLayout]:
    layout = initialize_workspace(
        workspace_root=tmp_path / "workspace",
        external_root=tmp_path / "external",
    )
    client = TestClient(create_app(build_service_container(layout)))
    return client, layout


def test_scan_start_or_resume_contract_data_fields_and_db_side_effect(scan_api_env) -> None:
    client, layout = scan_api_env

    response = client.post(
        "/api/scan/start_or_resume",
        json={"run_kind": "scan_full", "triggered_by": "manual_webui"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert set(payload["data"]) == {"session_id", "status", "resumed"}
    assert payload["data"]["status"] == "completed"
    assert payload["data"]["resumed"] is False

    row = _fetchone(
        layout.library_db,
        "SELECT status, triggered_by, finished_at FROM scan_session WHERE id=?",
        (payload["data"]["session_id"],),
    )
    assert row[0] == "completed"
    assert row[1] == "manual_webui"
    assert row[2] is not None
    assignment_run = _fetchone(
        layout.library_db,
        "SELECT status FROM assignment_run WHERE scan_session_id=?",
        (payload["data"]["session_id"],),
    )
    assert assignment_run == ("completed",)


def test_scan_start_or_resume_resumes_interrupted_session_and_updates_db(scan_api_env) -> None:
    client, layout = scan_api_env
    interrupted_id = _execute_insert(
        layout.library_db,
        """
        INSERT INTO scan_session(
          run_kind, status, triggered_by, created_at, updated_at
        ) VALUES ('scan_full', 'interrupted', 'manual_webui', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
    )

    response = client.post(
        "/api/scan/start_or_resume",
        json={"run_kind": "scan_resume", "triggered_by": "manual_webui"},
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "session_id": interrupted_id,
        "status": "completed",
        "resumed": True,
    }
    row = _fetchone(layout.library_db, "SELECT status, finished_at FROM scan_session WHERE id=?", (interrupted_id,))
    assert row[0] == "completed"
    assert row[1] is not None
    assignment_run = _fetchone(
        layout.library_db,
        "SELECT status FROM assignment_run WHERE scan_session_id=?",
        (interrupted_id,),
    )
    assert assignment_run == ("completed",)


def test_scan_start_or_resume_invalid_payload_returns_validation_error(scan_api_env) -> None:
    client, _layout = scan_api_env

    response = client.post(
        "/api/scan/start_or_resume",
        json={"run_kind": "bad-kind", "triggered_by": "manual_webui"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_scan_start_new_contract_data_fields_and_db_side_effect(scan_api_env) -> None:
    client, layout = scan_api_env
    interrupted_id = int(
        _execute_insert(
            layout.library_db,
            """
            INSERT INTO scan_session(
              run_kind, status, triggered_by, created_at, updated_at
            ) VALUES ('scan_full', 'interrupted', 'manual_webui', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        )
    )

    response = client.post(
        "/api/scan/start_new",
        json={"run_kind": "scan_incremental", "triggered_by": "manual_webui"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert set(payload["data"]) == {"session_id", "status"}
    assert payload["data"]["status"] == "completed"

    new_session = _fetchone(
        layout.library_db,
        "SELECT status, run_kind, triggered_by, finished_at FROM scan_session WHERE id=?",
        (payload["data"]["session_id"],),
    )
    interrupted = _fetchone(layout.library_db, "SELECT status FROM scan_session WHERE id=?", (interrupted_id,))
    assert new_session[0] == "completed"
    assert new_session[1] == "scan_incremental"
    assert new_session[2] == "manual_webui"
    assert new_session[3] is not None
    assert interrupted == ("abandoned",)
    assignment_run = _fetchone(
        layout.library_db,
        "SELECT status FROM assignment_run WHERE scan_session_id=?",
        (payload["data"]["session_id"],),
    )
    assert assignment_run == ("completed",)


def test_scan_start_new_active_conflict_returns_scan_active_conflict(api_env) -> None:
    client, layout, _seeded = api_env
    _execute_insert(
        layout.library_db,
        """
        INSERT INTO scan_session(
          run_kind, status, triggered_by, created_at, updated_at
        ) VALUES ('scan_full', 'running', 'manual_webui', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
    )

    response = client.post(
        "/api/scan/start_new",
        json={"run_kind": "scan_incremental", "triggered_by": "manual_webui"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SCAN_ACTIVE_CONFLICT"


def test_scan_abort_contract_data_fields_and_db_side_effect(api_env) -> None:
    client, layout, _seeded = api_env
    session_id = int(
        _execute_insert(
            layout.library_db,
            """
            INSERT INTO scan_session(
              run_kind, status, triggered_by, created_at, updated_at
            ) VALUES ('scan_full', 'running', 'manual_webui', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        )
    )

    response = client.post("/api/scan/abort", json={"session_id": session_id})

    assert response.status_code == 200
    payload = response.json()
    assert payload == {"ok": True, "data": {"session_id": session_id, "status": "aborting"}}
    row = _fetchone(layout.library_db, "SELECT status FROM scan_session WHERE id=?", (session_id,))
    assert row == ("aborting",)


def test_scan_abort_missing_session_returns_scan_session_not_found(api_env) -> None:
    client, _layout, _seeded = api_env

    response = client.post("/api/scan/abort", json={"session_id": 999999})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SCAN_SESSION_NOT_FOUND"


def test_people_rename_contract_data_fields_and_db_side_effect(api_env) -> None:
    client, layout, seeded = api_env

    response = client.post(
        f"/api/people/{seeded['rename_person_id']}/actions/rename",
        json={"display_name": "Alice Renamed"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"] == {
        "person_id": seeded["rename_person_id"],
        "display_name": "Alice Renamed",
        "is_named": True,
    }
    row = _fetchone(
        layout.library_db,
        "SELECT display_name, is_named FROM person WHERE id=?",
        (seeded["rename_person_id"],),
    )
    assert row == ("Alice Renamed", 1)


def test_people_rename_empty_name_returns_validation_error(api_env) -> None:
    client, _layout, seeded = api_env

    response = client.post(
        f"/api/people/{seeded['rename_person_id']}/actions/rename",
        json={"display_name": "   "},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_people_exclude_assignment_contract_data_fields_and_db_side_effect(api_env) -> None:
    client, layout, seeded = api_env
    person_id = int(seeded["exclude_person_id"])
    face_id = int(seeded["exclude_face_id"])

    response = client.post(
        f"/api/people/{person_id}/actions/exclude-assignment",
        json={"face_observation_id": face_id},
    )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "person_id": person_id,
        "face_observation_id": face_id,
        "pending_reassign": 1,
    }
    assignment = _fetchone(
        layout.library_db,
        "SELECT active FROM person_face_assignment WHERE person_id=? AND face_observation_id=? ORDER BY id DESC LIMIT 1",
        (person_id, face_id),
    )
    exclusion = _fetchone(
        layout.library_db,
        "SELECT active FROM person_face_exclusion WHERE person_id=? AND face_observation_id=? ORDER BY id DESC LIMIT 1",
        (person_id, face_id),
    )
    face = _fetchone(layout.library_db, "SELECT pending_reassign FROM face_observation WHERE id=?", (face_id,))
    assert assignment == (0,)
    assert exclusion == (1,)
    assert face == (1,)


def test_people_exclude_assignment_repeated_returns_conflict_error(api_env) -> None:
    client, _layout, seeded = api_env
    person_id = int(seeded["exclude_person_id"])
    face_id = int(seeded["exclude_face_id"])
    first = client.post(
        f"/api/people/{person_id}/actions/exclude-assignment",
        json={"face_observation_id": face_id},
    )
    assert first.status_code == 200

    response = client.post(
        f"/api/people/{person_id}/actions/exclude-assignment",
        json={"face_observation_id": face_id},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ILLEGAL_STATE"


def test_people_exclude_assignments_contract_data_fields_and_db_side_effect(api_env) -> None:
    client, layout, seeded = api_env
    person_id = int(seeded["exclude_batch_person_id"])
    face_ids = list(seeded["exclude_batch_face_ids"])

    response = client.post(
        f"/api/people/{person_id}/actions/exclude-assignments",
        json={"face_observation_ids": face_ids},
    )

    assert response.status_code == 200
    assert response.json()["data"] == {"person_id": person_id, "excluded_count": 2}
    count = _fetchone(
        layout.library_db,
        "SELECT COUNT(*) FROM person_face_exclusion WHERE person_id=? AND active=1",
        (person_id,),
    )
    pending = _fetchall(
        layout.library_db,
        "SELECT id, pending_reassign FROM face_observation WHERE id IN (?, ?) ORDER BY id ASC",
        (face_ids[0], face_ids[1]),
    )
    assert count == (2,)
    assert pending == [(face_ids[0], 1), (face_ids[1], 1)]


def test_people_exclude_assignments_empty_list_returns_validation_error(api_env) -> None:
    client, _layout, seeded = api_env

    response = client.post(
        f"/api/people/{seeded['exclude_batch_person_id']}/actions/exclude-assignments",
        json={"face_observation_ids": []},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_people_merge_batch_contract_data_fields_and_db_side_effect(api_env) -> None:
    client, layout, seeded = api_env
    selected_person_ids = [int(seeded["merge_winner_person_id"]), int(seeded["merge_loser_person_id"])]

    response = client.post(
        "/api/people/actions/merge-batch",
        json={"selected_person_ids": selected_person_ids},
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert set(payload) == {"merge_operation_id", "winner_person_id", "winner_person_uuid"}
    merge_row = _fetchone(
        layout.library_db,
        "SELECT winner_person_id, winner_person_uuid, status FROM merge_operation WHERE id=?",
        (payload["merge_operation_id"],),
    )
    assert merge_row == (payload["winner_person_id"], payload["winner_person_uuid"], "applied")


def test_people_merge_batch_requires_at_least_two_people(api_env) -> None:
    client, _layout, seeded = api_env

    response = client.post(
        "/api/people/actions/merge-batch",
        json={"selected_person_ids": [seeded["merge_winner_person_id"]]},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_people_undo_last_merge_contract_data_fields_and_db_side_effect(api_env) -> None:
    client, layout, seeded = api_env
    merge_response = client.post(
        "/api/people/actions/merge-batch",
        json={
            "selected_person_ids": [
                int(seeded["merge_winner_person_id"]),
                int(seeded["merge_loser_person_id"]),
            ]
        },
    )
    merge_operation_id = merge_response.json()["data"]["merge_operation_id"]

    response = client.post("/api/people/actions/undo-last-merge")

    assert response.status_code == 200
    assert response.json()["data"] == {"merge_operation_id": merge_operation_id, "status": "undone"}
    row = _fetchone(
        layout.library_db,
        "SELECT status FROM merge_operation WHERE id=?",
        (merge_operation_id,),
    )
    assert row == ("undone",)


def test_people_undo_last_merge_without_applied_merge_returns_not_found_code(api_env) -> None:
    client, _layout, _seeded = api_env

    response = client.post("/api/people/actions/undo-last-merge")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MERGE_OPERATION_NOT_FOUND"


def test_export_templates_list_contract_data_fields_and_db_side_effect(api_env) -> None:
    client, layout, seeded = api_env

    response = client.get("/api/export/templates")

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["id"] == seeded["template_id"]
    db_rows = _fetchall(layout.library_db, "SELECT id FROM export_template ORDER BY id ASC")
    assert [item["id"] for item in items] == [row[0] for row in db_rows]


def test_export_templates_list_invalid_limit_returns_validation_error(api_env) -> None:
    client, _layout, _seeded = api_env

    response = client.get("/api/export/templates?limit=0")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_export_template_create_contract_data_fields_and_db_side_effect(api_env) -> None:
    client, layout, seeded = api_env

    response = client.post(
        "/api/export/templates",
        json={
            "name": "新模板",
            "output_root": str(layout.workspace_root / "export-created"),
            "person_ids": [seeded["template_person_id"]],
        },
    )

    assert response.status_code == 200
    template_id = response.json()["data"]["template_id"]
    row = _fetchone(
        layout.library_db,
        "SELECT name, output_root FROM export_template WHERE id=?",
        (template_id,),
    )
    assert row == ("新模板", str(layout.workspace_root / "export-created"))


def test_export_template_create_duplicate_returns_duplicate_code(api_env) -> None:
    client, layout, seeded = api_env

    response = client.post(
        "/api/export/templates",
        json={
            "name": "模板一",
            "output_root": str(layout.workspace_root / "export-dup"),
            "person_ids": [seeded["template_person_id"]],
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EXPORT_TEMPLATE_DUPLICATE"


def test_export_template_update_contract_data_fields_and_db_side_effect(api_env) -> None:
    client, layout, seeded = api_env

    response = client.put(
        f"/api/export/templates/{seeded['template_id']}",
        json={"name": "模板已更新"},
    )

    assert response.status_code == 200
    assert response.json()["data"] == {"template_id": seeded["template_id"], "updated": True}
    row = _fetchone(layout.library_db, "SELECT name FROM export_template WHERE id=?", (seeded["template_id"],))
    assert row == ("模板已更新",)


def test_export_template_update_missing_template_returns_not_found_code(api_env) -> None:
    client, _layout, _seeded = api_env

    response = client.put("/api/export/templates/999999", json={"name": "missing"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "EXPORT_TEMPLATE_NOT_FOUND"


def test_export_template_run_contract_data_fields_and_db_side_effect(api_env) -> None:
    client, layout, seeded = api_env

    response = client.post(f"/api/export/templates/{seeded['template_id']}/actions/run")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "running"
    row = _fetchone(
        layout.library_db,
        "SELECT status, template_id FROM export_run WHERE id=?",
        (data["export_run_id"],),
    )
    assert row == ("running", seeded["template_id"])


def test_export_run_execute_contract_data_fields_and_db_side_effect(api_env) -> None:
    client, layout, seeded = api_env
    output_root = str(layout.workspace_root / "export-run-execute")
    create_template_response = client.post(
        "/api/export/templates",
        json={
            "name": "执行导出模板",
            "output_root": output_root,
            "person_ids": [seeded["exclude_person_id"]],
        },
    )
    template_id = int(create_template_response.json()["data"]["template_id"])
    create_run_response = client.post(f"/api/export/templates/{template_id}/actions/run")
    export_run_id = int(create_run_response.json()["data"]["export_run_id"])

    response = client.post(f"/api/export/runs/{export_run_id}/actions/execute")

    assert create_template_response.status_code == 200
    assert response.status_code == 200
    assert response.json()["data"] == {
        "export_run_id": export_run_id,
        "status": "completed",
        "exported_count": 1,
        "skipped_exists_count": 0,
        "failed_count": 0,
    }
    row = _fetchone(
        layout.library_db,
        "SELECT status, finished_at FROM export_run WHERE id=?",
        (export_run_id,),
    )
    delivery = _fetchone(
        layout.library_db,
        """
        SELECT delivery_status
        FROM export_delivery
        WHERE export_run_id=?
        ORDER BY id ASC
        LIMIT 1
        """,
        (export_run_id,),
    )
    assert row is not None
    assert row[0] == "completed"
    assert row[1] is not None
    assert delivery == ("exported",)


def test_export_template_run_missing_template_returns_not_found_code(api_env) -> None:
    client, _layout, _seeded = api_env

    response = client.post("/api/export/templates/999999/actions/run")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "EXPORT_TEMPLATE_NOT_FOUND"


def test_export_run_execute_missing_run_returns_not_found_code(api_env) -> None:
    client, _layout, _seeded = api_env

    response = client.post("/api/export/runs/999999/actions/execute")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "EXPORT_RUN_NOT_FOUND"


def test_scan_audit_items_contract_data_fields_and_db_side_effect(api_env) -> None:
    client, layout, seeded = api_env

    response = client.get(f"/api/scan/{seeded['seeded_session_id']}/audit-items")

    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["face_observation_id"] == seeded["audit_face_id"]
    db_count = _fetchone(
        layout.library_db,
        "SELECT COUNT(*) FROM scan_audit_item WHERE scan_session_id=?",
        (seeded["seeded_session_id"],),
    )
    assert db_count == (1,)


def test_scan_audit_items_returns_not_found_for_missing_session(api_env) -> None:
    client, _layout, _seeded = api_env

    response = client.get("/api/scan/999999/audit-items")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SCAN_SESSION_NOT_FOUND"


def test_delete_export_template_route_is_absent(api_env) -> None:
    client, _layout, _seeded = api_env
    delete_routes = [
        route
        for route in client.app.routes
        if getattr(route, "path", "") == "/api/export/templates/{template_id}"
        and "DELETE" in getattr(route, "methods", set())
    ]
    assert delete_routes == []


def _seed_api_data(layout: WorkspaceLayout, session_id: int, face_ids: list[int]) -> dict[str, int | list[int]]:
    conn = sqlite3.connect(layout.library_db)
    try:
        rename_person_id = _insert_person(conn, display_name="Rename Me", is_named=True)
        exclude_person_id = _insert_person(conn, display_name="Exclude One", is_named=True)
        exclude_batch_person_id = _insert_person(conn, display_name="Exclude Batch", is_named=True)
        merge_winner_person_id = _insert_person(conn, display_name="Winner", is_named=True)
        merge_loser_person_id = _insert_person(conn, display_name="Loser", is_named=True)
        template_person_id = _insert_person(conn, display_name="Template Person", is_named=True)

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
                (exclude_person_id, face_ids[0], assignment_run_id, 0.93, 0.11),
                (exclude_batch_person_id, face_ids[1], assignment_run_id, 0.94, 0.12),
                (exclude_batch_person_id, face_ids[2], assignment_run_id, 0.95, 0.13),
                (merge_winner_person_id, face_ids[3], assignment_run_id, 0.96, 0.14),
            ],
        )

        merge_extra_face_id = int(
            conn.execute(
                """
                INSERT INTO face_observation(
                  photo_asset_id, face_index, crop_relpath, aligned_relpath, context_relpath,
                  bbox_x1, bbox_y1, bbox_x2, bbox_y2,
                  detector_confidence, face_area_ratio, magface_quality, quality_score,
                  active, inactive_reason, pending_reassign, created_at, updated_at
                ) VALUES (2, 99, 'artifacts/crops/m99.jpg', 'artifacts/aligned/m99.png', 'artifacts/context/m99.jpg',
                  10, 10, 80, 80, 0.98, 0.3, 1.2, 0.9, 1, NULL, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO person_face_assignment(
              person_id, face_observation_id, assignment_run_id, assignment_source, active, confidence, margin, created_at, updated_at
            ) VALUES (?, ?, ?, 'hdbscan', 1, 0.97, 0.15, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (merge_loser_person_id, merge_extra_face_id, assignment_run_id),
        )

        template_id = int(
            conn.execute(
                """
                INSERT INTO export_template(name, output_root, enabled, created_at, updated_at)
                VALUES ('模板一', ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (str(layout.workspace_root / "exports-template"),),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO export_template_person(template_id, person_id, created_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            """,
            (template_id, template_person_id),
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
                exclude_person_id,
                json.dumps({"person_id": exclude_person_id, "face_observation_id": face_ids[0]}, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "seeded_session_id": session_id,
        "rename_person_id": rename_person_id,
        "exclude_person_id": exclude_person_id,
        "exclude_face_id": face_ids[0],
        "exclude_batch_person_id": exclude_batch_person_id,
        "exclude_batch_face_ids": [face_ids[1], face_ids[2]],
        "merge_winner_person_id": merge_winner_person_id,
        "merge_loser_person_id": merge_loser_person_id,
        "template_person_id": template_person_id,
        "template_id": template_id,
        "audit_face_id": face_ids[0],
    }


def _insert_person(conn: sqlite3.Connection, *, display_name: str, is_named: bool) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO person(person_uuid, display_name, is_named, status, created_at, updated_at)
            VALUES (?, ?, ?, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (str(uuid.uuid4()), display_name, 1 if is_named else 0),
        ).lastrowid
    )


def _fetchone(db_path: Path, sql: str, params: tuple[object, ...]) -> tuple[object, ...] | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(sql, params).fetchone()
    finally:
        conn.close()
    return None if row is None else tuple(row)


def _fetchall(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [tuple(row) for row in rows]


def _execute_insert(db_path: Path, sql: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(sql)
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def _execute_sql(db_path: Path, sql: str, params: tuple[object, ...]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()
