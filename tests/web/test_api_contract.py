from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from hikbox_pictures.product.audit.service import AssignmentAuditInput, AuditSamplingService
from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.export import ensure_export_schema
from hikbox_pictures.product.export.template_service import ExportTemplateService
from hikbox_pictures.web.app import ServiceContainer, create_app

NOW = "2026-04-22T00:00:00+00:00"


def _build_client(tmp_path: Path) -> tuple[TestClient, Path]:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    app = create_app(ServiceContainer.from_library_db(layout.library_db_path))
    return TestClient(app), layout.library_db_path


def _insert_scan_session(db_path: Path, *, status: str, run_kind: str = "scan_full") -> int:
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
            VALUES (?, ?, 'manual_cli', NULL, ?, NULL, NULL, ?, ?)
            """,
            (run_kind, status, NOW, NOW, NOW),
        )
        conn.commit()
        return int(cursor.lastrowid)


def _insert_scan_session_in_conn(conn: sqlite3.Connection, *, status: str, run_kind: str = "scan_full") -> int:
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
            VALUES (?, ?, 'manual_cli', NULL, ?, NULL, NULL, ?, ?)
            """,
            (run_kind, status, NOW, NOW, NOW),
        )
    return int(cursor.lastrowid)


def _insert_source(conn: sqlite3.Connection, root: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO library_source(root_path, label, enabled, status, last_discovered_at, created_at, updated_at)
        VALUES (?, '测试源', 1, 'active', NULL, ?, ?)
        """,
        (root, NOW, NOW),
    )
    return int(cursor.lastrowid)


def _insert_photo(conn: sqlite3.Connection, *, source_id: int, name: str) -> int:
    cursor = conn.execute(
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
        VALUES (?, ?, ?, 'sha256', 100, 1710000000000000000, '2026-03-14T12:00:00+08:00', '2026-03', 0, NULL, NULL, NULL, 'active', ?, ?)
        """,
        (source_id, name, f"fp-{name}", NOW, NOW),
    )
    return int(cursor.lastrowid)


def _insert_face(conn: sqlite3.Connection, *, photo_id: int, face_index: int) -> int:
    cursor = conn.execute(
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
        VALUES (?, ?, 'crop/a.jpg', 'aligned/a.jpg', 'context/a.jpg', 0.1, 0.1, 0.9, 0.9, 0.98, 0.2, 30.0, 0.95, 1, NULL, 0, ?, ?)
        """,
        (photo_id, face_index, NOW, NOW),
    )
    return int(cursor.lastrowid)


def _insert_person(conn: sqlite3.Connection, *, person_uuid: str, display_name: str | None, is_named: int) -> int:
    cursor = conn.execute(
        """
        INSERT INTO person(person_uuid, display_name, is_named, status, merged_into_person_id, created_at, updated_at)
        VALUES (?, ?, ?, 'active', NULL, ?, ?)
        """,
        (person_uuid, display_name, is_named, NOW, NOW),
    )
    return int(cursor.lastrowid)


def _insert_assignment_run(conn: sqlite3.Connection, *, session_id: int) -> int:
    cursor = conn.execute(
        """
        INSERT INTO assignment_run(
          scan_session_id,
          algorithm_version,
          param_snapshot_json,
          run_kind,
          started_at,
          finished_at,
          status
        )
        VALUES (?, 'v5.2026-04-21', '{}', 'scan_full', ?, ?, 'completed')
        """,
        (session_id, NOW, NOW),
    )
    return int(cursor.lastrowid)


def _insert_assignment(conn: sqlite3.Connection, *, person_id: int, face_id: int, run_id: int) -> None:
    conn.execute(
        """
        INSERT INTO person_face_assignment(
          person_id,
          face_observation_id,
          assignment_run_id,
          assignment_source,
          active,
          confidence,
          margin,
          created_at,
          updated_at
        )
        VALUES (?, ?, ?, 'hdbscan', 1, 0.9, 0.2, ?, ?)
        """,
        (person_id, face_id, run_id, NOW, NOW),
    )


def _seed_people_scene(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        ensure_export_schema(conn)
        source_id = _insert_source(conn, "/tmp/photos")
        photo_1 = _insert_photo(conn, source_id=source_id, name="a.heic")
        photo_2 = _insert_photo(conn, source_id=source_id, name="b.heic")
        photo_3 = _insert_photo(conn, source_id=source_id, name="c.heic")
        face_1 = _insert_face(conn, photo_id=photo_1, face_index=0)
        face_2 = _insert_face(conn, photo_id=photo_2, face_index=1)
        face_3 = _insert_face(conn, photo_id=photo_3, face_index=2)
        person_1 = _insert_person(conn, person_uuid="00000000-0000-0000-0000-000000000101", display_name="甲", is_named=1)
        person_2 = _insert_person(conn, person_uuid="00000000-0000-0000-0000-000000000102", display_name="乙", is_named=1)
        person_3 = _insert_person(conn, person_uuid="00000000-0000-0000-0000-000000000103", display_name=None, is_named=0)
        session_id = _insert_scan_session_in_conn(conn, status="completed")
        run_id = _insert_assignment_run(conn, session_id=session_id)
        _insert_assignment(conn, person_id=person_1, face_id=face_1, run_id=run_id)
        _insert_assignment(conn, person_id=person_1, face_id=face_2, run_id=run_id)
        _insert_assignment(conn, person_id=person_2, face_id=face_3, run_id=run_id)
        conn.commit()
    return {
        "person_1": person_1,
        "person_2": person_2,
        "person_3": person_3,
        "face_1": face_1,
        "face_2": face_2,
        "face_3": face_3,
        "scan_session_id": session_id,
        "assignment_run_id": run_id,
    }


def _start_running_export_run(db_path: Path, *, person_id: int, name_suffix: str) -> int:
    with sqlite3.connect(db_path) as conn:
        ensure_export_schema(conn)
        template_id = int(
            conn.execute(
                """
                INSERT INTO export_template(name, output_root, enabled, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?)
                """,
                (f"模板-lock-{name_suffix}", f"/tmp/export-lock-{name_suffix}", NOW, NOW),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO export_template_person(template_id, person_id, created_at)
            VALUES (?, ?, ?)
            """,
            (template_id, int(person_id), NOW),
        )
        export_run_id = int(
            conn.execute(
                """
                INSERT INTO export_run(template_id, status, summary_json, started_at, finished_at)
                VALUES (?, 'running', '{}', ?, NULL)
                """,
                (template_id, NOW),
            ).lastrowid
        )
        conn.commit()
    return export_run_id


def test_scan_start_or_resume_contract_data_fields_and_db_side_effect(tmp_path: Path) -> None:
    client, db_path = _build_client(tmp_path)
    interrupted_id = _insert_scan_session(db_path, status="interrupted", run_kind="scan_resume")

    resp = client.post("/api/scan/start_or_resume", json={})
    body = resp.json()

    assert resp.status_code == 200
    assert body["ok"] is True
    assert set(body["data"].keys()) >= {"session_id", "status", "resumed"}
    assert body["data"]["session_id"] == interrupted_id
    assert body["data"]["status"] == "running"
    assert body["data"]["resumed"] is True

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT status FROM scan_session WHERE id=?", (interrupted_id,)).fetchone()
    assert row == ("running",)


def test_scan_start_or_resume_invalid_run_kind_returns_validation_error(tmp_path: Path) -> None:
    client, _db_path = _build_client(tmp_path)
    resp = client.post("/api/scan/start_or_resume", json={"run_kind": "scan_unsupported"})
    body = resp.json()
    assert resp.status_code == 400
    assert body["ok"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


def test_scan_start_new_contract_data_fields_and_db_side_effect(tmp_path: Path) -> None:
    client, db_path = _build_client(tmp_path)
    interrupted_id = _insert_scan_session(db_path, status="interrupted", run_kind="scan_resume")

    running_id = _insert_scan_session(db_path, status="running")
    conflict_2 = client.post("/api/scan/start_new", json={"run_kind": "scan_full"})
    assert conflict_2.status_code == 409
    assert conflict_2.json()["error"]["code"] == "SCAN_ACTIVE_CONFLICT"

    abort_resp = client.post("/api/scan/abort", json={"session_id": running_id})
    assert abort_resp.status_code == 200

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE scan_session SET status='completed', finished_at=?, updated_at=? WHERE id=?",
            (NOW, NOW, running_id),
        )
        conn.commit()

    resp = client.post("/api/scan/start_new", json={"run_kind": "scan_full"})
    body = resp.json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert set(body["data"].keys()) >= {"session_id", "status"}

    with sqlite3.connect(db_path) as conn:
        old_row = conn.execute("SELECT status FROM scan_session WHERE id=?", (interrupted_id,)).fetchone()
        new_row = conn.execute("SELECT status FROM scan_session WHERE id=?", (body["data"]["session_id"],)).fetchone()
    assert old_row == ("abandoned",)
    assert new_row == ("running",)


def test_scan_abort_contract_data_fields_and_db_side_effect(tmp_path: Path) -> None:
    client, db_path = _build_client(tmp_path)
    session_id = _insert_scan_session(db_path, status="running")

    missing = client.post("/api/scan/abort", json={"session_id": 9999})
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "SCAN_SESSION_NOT_FOUND"

    with sqlite3.connect(db_path) as conn:
        before = conn.execute("SELECT updated_at FROM scan_session WHERE id=?", (session_id,)).fetchone()

    resp = client.post("/api/scan/abort", json={"session_id": session_id})
    body = resp.json()
    assert resp.status_code == 200
    assert body["data"] == {"session_id": session_id, "status": "aborting"}

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT status, updated_at FROM scan_session WHERE id=?", (session_id,)).fetchone()
    assert row is not None
    assert row[0] == "aborting"
    assert row[1] != before[0]


def test_people_rename_contract_data_fields_and_db_side_effect(tmp_path: Path) -> None:
    client, db_path = _build_client(tmp_path)
    ids = _seed_people_scene(db_path)

    invalid = client.post(f"/api/people/{ids['person_3']}/actions/rename", json={"display_name": "   "})
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"

    resp = client.post(f"/api/people/{ids['person_3']}/actions/rename", json={"display_name": "新名字"})
    body = resp.json()
    assert resp.status_code == 200
    assert body["data"] == {"person_id": ids["person_3"], "display_name": "新名字", "is_named": True}

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT display_name, is_named FROM person WHERE id=?", (ids["person_3"],)).fetchone()
    assert row == ("新名字", 1)


def test_people_rename_returns_export_running_lock_when_export_running(tmp_path: Path) -> None:
    client, db_path = _build_client(tmp_path)
    ids = _seed_people_scene(db_path)
    _start_running_export_run(db_path, person_id=ids["person_1"], name_suffix="rename")
    resp = client.post(f"/api/people/{ids['person_3']}/actions/rename", json={"display_name": "改名"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "EXPORT_RUNNING_LOCK"


def test_people_exclude_assignment_contract_data_fields_and_db_side_effect(tmp_path: Path) -> None:
    client, db_path = _build_client(tmp_path)
    ids = _seed_people_scene(db_path)

    resp = client.post(
        f"/api/people/{ids['person_1']}/actions/exclude-assignment",
        json={"face_observation_id": ids["face_1"]},
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["data"] == {
        "person_id": ids["person_1"],
        "face_observation_id": ids["face_1"],
        "pending_reassign": 1,
    }

    with sqlite3.connect(db_path) as conn:
        exclusion = conn.execute(
            "SELECT active FROM person_face_exclusion WHERE person_id=? AND face_observation_id=?",
            (ids["person_1"], ids["face_1"]),
        ).fetchone()
        pending = conn.execute("SELECT pending_reassign FROM face_observation WHERE id=?", (ids["face_1"],)).fetchone()
    assert exclusion == (1,)
    assert pending == (1,)

    duplicate = client.post(
        f"/api/people/{ids['person_1']}/actions/exclude-assignment",
        json={"face_observation_id": ids["face_1"]},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "ILLEGAL_STATE"


def test_people_exclude_assignment_illegal_state_does_not_depend_on_error_message(tmp_path: Path) -> None:
    client, db_path = _build_client(tmp_path)
    ids = _seed_people_scene(db_path)
    client.post(
        f"/api/people/{ids['person_1']}/actions/exclude-assignment",
        json={"face_observation_id": ids["face_1"]},
    )
    resp = client.post(
        f"/api/people/{ids['person_1']}/actions/exclude-assignment",
        json={"face_observation_id": ids["face_1"]},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "ILLEGAL_STATE"


def test_people_exclude_assignment_illegal_state_maps_409_even_when_service_error_message_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client, db_path = _build_client(tmp_path)
    ids = _seed_people_scene(db_path)
    first = client.post(
        f"/api/people/{ids['person_1']}/actions/exclude-assignment",
        json={"face_observation_id": ids["face_1"]},
    )
    assert first.status_code == 200

    def _raise_unexpected_message(*_args, **_kwargs):
        raise ValueError("some totally different message")

    monkeypatch.setattr(
        client.app.state.services.people_service,
        "exclude_assignment",
        _raise_unexpected_message,
    )

    resp = client.post(
        f"/api/people/{ids['person_1']}/actions/exclude-assignment",
        json={"face_observation_id": ids["face_1"]},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "ILLEGAL_STATE"


def test_people_exclude_assignment_returns_export_running_lock_when_export_running(tmp_path: Path) -> None:
    client, db_path = _build_client(tmp_path)
    ids = _seed_people_scene(db_path)
    _start_running_export_run(db_path, person_id=ids["person_1"], name_suffix="exclude-one")
    resp = client.post(
        f"/api/people/{ids['person_1']}/actions/exclude-assignment",
        json={"face_observation_id": ids["face_1"]},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "EXPORT_RUNNING_LOCK"


def test_people_exclude_assignments_contract_data_fields_and_db_side_effect(tmp_path: Path) -> None:
    client, db_path = _build_client(tmp_path)
    ids = _seed_people_scene(db_path)

    invalid = client.post(f"/api/people/{ids['person_1']}/actions/exclude-assignments", json={"face_observation_ids": []})
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"

    resp = client.post(
        f"/api/people/{ids['person_1']}/actions/exclude-assignments",
        json={"face_observation_ids": [ids["face_1"], ids["face_2"]]},
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["data"] == {"person_id": ids["person_1"], "excluded_count": 2}

    with sqlite3.connect(db_path) as conn:
        count_row = conn.execute(
            "SELECT COUNT(*) FROM person_face_exclusion WHERE person_id=? AND active=1",
            (ids["person_1"],),
        ).fetchone()
    assert count_row == (2,)


def test_people_exclude_assignments_illegal_state_does_not_depend_on_error_message(tmp_path: Path) -> None:
    client, db_path = _build_client(tmp_path)
    ids = _seed_people_scene(db_path)
    client.post(
        f"/api/people/{ids['person_1']}/actions/exclude-assignment",
        json={"face_observation_id": ids["face_1"]},
    )
    resp = client.post(
        f"/api/people/{ids['person_1']}/actions/exclude-assignments",
        json={"face_observation_ids": [ids["face_1"], ids["face_2"]]},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "ILLEGAL_STATE"


def test_people_exclude_assignments_returns_export_running_lock_when_export_running(tmp_path: Path) -> None:
    client, db_path = _build_client(tmp_path)
    ids = _seed_people_scene(db_path)
    _start_running_export_run(db_path, person_id=ids["person_1"], name_suffix="exclude-batch")
    resp = client.post(
        f"/api/people/{ids['person_1']}/actions/exclude-assignments",
        json={"face_observation_ids": [ids["face_1"], ids["face_2"]]},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "EXPORT_RUNNING_LOCK"


def test_people_merge_batch_contract_data_fields_and_db_side_effect(tmp_path: Path) -> None:
    client, db_path = _build_client(tmp_path)
    ids = _seed_people_scene(db_path)

    invalid = client.post("/api/people/actions/merge-batch", json={"selected_person_ids": [ids["person_1"]]})
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"

    resp = client.post(
        "/api/people/actions/merge-batch",
        json={"selected_person_ids": [ids["person_1"], ids["person_2"]]},
    )
    body = resp.json()
    assert resp.status_code == 200
    assert set(body["data"].keys()) == {"merge_operation_id", "winner_person_id", "winner_person_uuid"}

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT winner_person_id, winner_person_uuid FROM merge_operation WHERE id=?",
            (body["data"]["merge_operation_id"],),
        ).fetchone()
    assert row == (body["data"]["winner_person_id"], body["data"]["winner_person_uuid"])


def test_people_merge_batch_returns_export_running_lock_when_export_running(tmp_path: Path) -> None:
    client, db_path = _build_client(tmp_path)
    ids = _seed_people_scene(db_path)
    _start_running_export_run(db_path, person_id=ids["person_1"], name_suffix="merge")
    resp = client.post(
        "/api/people/actions/merge-batch",
        json={"selected_person_ids": [ids["person_1"], ids["person_2"]]},
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "EXPORT_RUNNING_LOCK"


def test_people_undo_last_merge_contract_data_fields_and_db_side_effect(tmp_path: Path) -> None:
    client, db_path = _build_client(tmp_path)
    ids = _seed_people_scene(db_path)

    missing = client.post("/api/people/actions/undo-last-merge", json={})
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "MERGE_OPERATION_NOT_FOUND"

    merge = client.post(
        "/api/people/actions/merge-batch",
        json={"selected_person_ids": [ids["person_1"], ids["person_2"]]},
    )
    merge_operation_id = merge.json()["data"]["merge_operation_id"]

    resp = client.post("/api/people/actions/undo-last-merge", json={})
    body = resp.json()
    assert resp.status_code == 200
    assert body["data"] == {"merge_operation_id": merge_operation_id, "status": "undone"}

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT status FROM merge_operation WHERE id=?", (merge_operation_id,)).fetchone()
    assert row == ("undone",)


def test_people_undo_last_merge_returns_export_running_lock_when_export_running(tmp_path: Path) -> None:
    client, db_path = _build_client(tmp_path)
    ids = _seed_people_scene(db_path)
    _start_running_export_run(db_path, person_id=ids["person_1"], name_suffix="undo")
    resp = client.post("/api/people/actions/undo-last-merge", json={})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "EXPORT_RUNNING_LOCK"


def test_export_templates_list_contract_data_fields_and_db_side_effect(tmp_path: Path) -> None:
    client, db_path = _build_client(tmp_path)
    ids = _seed_people_scene(db_path)
    service = ExportTemplateService(db_path)
    item_1 = service.create_template(name="模板-1", output_root=(tmp_path / "out-1").resolve(), person_ids=[ids["person_1"]])
    item_2 = service.create_template(name="模板-2", output_root=(tmp_path / "out-2").resolve(), person_ids=[ids["person_2"]])

    invalid = client.get("/api/export/templates?limit=0")
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"

    resp = client.get("/api/export/templates?limit=10")
    body = resp.json()
    assert resp.status_code == 200
    assert "items" in body["data"]
    returned_ids = {int(item["template_id"]) for item in body["data"]["items"]}

    with sqlite3.connect(db_path) as conn:
        db_ids = {int(row[0]) for row in conn.execute("SELECT id FROM export_template").fetchall()}

    assert returned_ids == db_ids == {item_1.id, item_2.id}


def test_export_template_create_contract_data_fields_and_db_side_effect(tmp_path: Path) -> None:
    client, db_path = _build_client(tmp_path)
    ids = _seed_people_scene(db_path)

    first = client.post(
        "/api/export/templates",
        json={"name": "模板-create", "output_root": str((tmp_path / "out-create").resolve()), "person_ids": [ids["person_1"]]},
    )
    assert first.status_code == 200

    duplicate = client.post(
        "/api/export/templates",
        json={"name": "模板-create", "output_root": str((tmp_path / "out-create-2").resolve()), "person_ids": [ids["person_1"]]},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "EXPORT_TEMPLATE_DUPLICATE"

    resp = client.post(
        "/api/export/templates",
        json={"name": "模板-create-2", "output_root": str((tmp_path / "out-create-3").resolve()), "person_ids": [ids["person_2"]]},
    )
    body = resp.json()
    assert resp.status_code == 200
    assert set(body["data"].keys()) == {"template_id"}

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT id FROM export_template WHERE id=?", (body["data"]["template_id"],)).fetchone()
    assert row == (body["data"]["template_id"],)


def test_export_template_update_contract_data_fields_and_db_side_effect(tmp_path: Path) -> None:
    client, db_path = _build_client(tmp_path)
    ids = _seed_people_scene(db_path)

    missing = client.put(
        "/api/export/templates/9999",
        json={"name": "missing", "output_root": str((tmp_path / "out-missing").resolve()), "person_ids": [ids["person_1"]]},
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "EXPORT_TEMPLATE_NOT_FOUND"

    create = client.post(
        "/api/export/templates",
        json={"name": "模板-update", "output_root": str((tmp_path / "out-update").resolve()), "person_ids": [ids["person_1"]]},
    )
    template_id = create.json()["data"]["template_id"]
    create_2 = client.post(
        "/api/export/templates",
        json={"name": "模板-update-dup", "output_root": str((tmp_path / "out-update-dup").resolve()), "person_ids": [ids["person_1"]]},
    )
    assert create_2.status_code == 200

    duplicate = client.put(
        f"/api/export/templates/{template_id}",
        json={"name": "模板-update-dup"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "EXPORT_TEMPLATE_DUPLICATE"

    same_name = client.put(
        f"/api/export/templates/{template_id}",
        json={"name": "模板-update"},
    )
    assert same_name.status_code == 200
    assert same_name.json()["data"] == {"template_id": template_id, "updated": True}

    resp = client.put(
        f"/api/export/templates/{template_id}",
        json={"name": "模板-update-2", "output_root": str((tmp_path / "out-update-2").resolve()), "person_ids": [ids["person_2"]]},
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["data"] == {"template_id": template_id, "updated": True}

    with sqlite3.connect(db_path) as conn:
        template_row = conn.execute("SELECT name, output_root FROM export_template WHERE id=?", (template_id,)).fetchone()
        person_rows = conn.execute(
            "SELECT person_id FROM export_template_person WHERE template_id=? ORDER BY person_id",
            (template_id,),
        ).fetchall()
    assert template_row == ("模板-update-2", str((tmp_path / "out-update-2").resolve()))
    assert person_rows == [(ids["person_2"],)]


def test_export_template_run_contract_data_fields_and_db_side_effect(tmp_path: Path) -> None:
    client, db_path = _build_client(tmp_path)
    ids = _seed_people_scene(db_path)

    missing = client.post("/api/export/templates/9999/actions/run", json={})
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "EXPORT_TEMPLATE_NOT_FOUND"

    create = client.post(
        "/api/export/templates",
        json={"name": "模板-run", "output_root": str((tmp_path / "out-run").resolve()), "person_ids": [ids["person_1"]]},
    )
    template_id = create.json()["data"]["template_id"]

    resp = client.post(f"/api/export/templates/{template_id}/actions/run", json={})
    body = resp.json()
    assert resp.status_code == 200
    assert body["data"] == {"export_run_id": body["data"]["export_run_id"], "status": "running"}

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT status FROM export_run WHERE id=?", (body["data"]["export_run_id"],)).fetchone()
    assert row == ("running",)


def test_scan_audit_items_contract_data_fields_and_db_side_effect(tmp_path: Path) -> None:
    client, db_path = _build_client(tmp_path)
    ids = _seed_people_scene(db_path)

    service = AuditSamplingService(db_path)
    service.sample_assignment_run(
        scan_session_id=ids["scan_session_id"],
        assignment_run_id=ids["assignment_run_id"],
        assignments=[
            AssignmentAuditInput(
                face_observation_id=ids["face_1"],
                person_id=ids["person_1"],
                assignment_source="hdbscan",
                margin=0.01,
                evidence={"note": "low-margin"},
            ),
            AssignmentAuditInput(
                face_observation_id=ids["face_2"],
                person_id=ids["person_1"],
                assignment_source="hdbscan",
                reassign_after_exclusion=True,
                evidence={"note": "reassign"},
            ),
        ],
    )

    missing = client.get("/api/scan/9999/audit-items")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "SCAN_SESSION_NOT_FOUND"

    resp = client.get(f"/api/scan/{ids['scan_session_id']}/audit-items")
    body = resp.json()
    assert resp.status_code == 200
    assert "items" in body["data"]

    with sqlite3.connect(db_path) as conn:
        db_rows = conn.execute(
            "SELECT audit_type, face_observation_id, person_id FROM scan_audit_item WHERE scan_session_id=?",
            (ids["scan_session_id"],),
        ).fetchall()

    api_rows = {
        (item["audit_type"], int(item["face_observation_id"]), item["person_id"])
        for item in body["data"]["items"]
    }
    assert api_rows == set(db_rows)
