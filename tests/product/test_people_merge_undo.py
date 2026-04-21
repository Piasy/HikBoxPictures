from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hikbox_pictures.product.db.schema_bootstrap import bootstrap_library_schema
from hikbox_pictures.product.people.repository import SQLitePeopleRepository
from hikbox_pictures.product.people.service import MergeOperationNotFoundError, PeopleService

NOW = "2026-04-22T00:00:00+00:00"


def _assert_people_maintenance_schema_exists(db_path: Path) -> None:
    required_tables = {
        "person_face_exclusion",
        "merge_operation",
        "merge_operation_person_delta",
        "merge_operation_assignment_delta",
        "merge_operation_exclusion_delta",
    }
    required_indexes = {
        "uq_person_face_exclusion_active",
        "idx_exclusion_face",
    }
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT name, type
            FROM sqlite_master
            WHERE type IN ('table', 'index')
              AND name IN (
                'person_face_exclusion',
                'merge_operation',
                'merge_operation_person_delta',
                'merge_operation_assignment_delta',
                'merge_operation_exclusion_delta',
                'uq_person_face_exclusion_active',
                'idx_exclusion_face'
              )
            """,
        ).fetchall()
    existing_tables = {str(row[0]) for row in rows if str(row[1]) == "table"}
    existing_indexes = {str(row[0]) for row in rows if str(row[1]) == "index"}
    missing_tables = sorted(required_tables - existing_tables)
    missing_indexes = sorted(required_indexes - existing_indexes)
    assert not missing_tables and not missing_indexes, (
        "bootstrap_library_schema 缺少人物维护 schema: "
        f"missing_tables={missing_tables}, missing_indexes={missing_indexes}"
    )


def _seed_people_fixture(db_path: Path) -> dict[str, int]:
    bootstrap_library_schema(db_path)
    _assert_people_maintenance_schema_exists(db_path)
    with sqlite3.connect(db_path) as conn:
        source_id = _insert_source(conn)
        face_1 = _insert_face_observation(conn, source_id=source_id, face_index=1)
        face_2 = _insert_face_observation(conn, source_id=source_id, face_index=2)
        face_3 = _insert_face_observation(conn, source_id=source_id, face_index=3)
        person_1 = _insert_person(conn, person_uuid="person-1", display_name="甲")
        person_2 = _insert_person(conn, person_uuid="person-2", display_name="乙")
        person_3 = _insert_person(conn, person_uuid="person-3", display_name="丙")
        assignment_run_id = _insert_assignment_run(conn)
        _insert_active_assignment(
            conn,
            person_id=person_1,
            face_observation_id=face_1,
            assignment_run_id=assignment_run_id,
        )
        _insert_active_assignment(
            conn,
            person_id=person_2,
            face_observation_id=face_2,
            assignment_run_id=assignment_run_id,
        )
        _insert_active_assignment(
            conn,
            person_id=person_3,
            face_observation_id=face_3,
            assignment_run_id=assignment_run_id,
        )
        conn.execute(
            """
            INSERT INTO person_face_exclusion(person_id, face_observation_id, reason, active, created_at, updated_at)
            VALUES (?, ?, 'manual_exclude', 1, ?, ?)
            """,
            (person_2, face_2, NOW, NOW),
        )
        conn.commit()

    return {
        "person_1": person_1,
        "person_2": person_2,
        "person_3": person_3,
        "face_1": face_1,
        "face_2": face_2,
        "face_3": face_3,
    }


def _insert_source(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        """
        INSERT INTO library_source(root_path, label, enabled, status, last_discovered_at, created_at, updated_at)
        VALUES ('/tmp/photos', '测试目录', 1, 'active', NULL, ?, ?)
        """,
        (NOW, NOW),
    )
    return int(cursor.lastrowid)


def _insert_face_observation(conn: sqlite3.Connection, *, source_id: int, face_index: int) -> int:
    photo_cursor = conn.execute(
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
        VALUES (?, ?, ?, 'sha256', 100, 200, NULL, NULL, 0, NULL, NULL, NULL, 'active', ?, ?)
        """,
        (source_id, f"IMG_{face_index:04d}.HEIC", f"fp-{face_index}", NOW, NOW),
    )
    photo_id = int(photo_cursor.lastrowid)
    face_cursor = conn.execute(
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
        VALUES (?, 0, ?, ?, ?, 0.1, 0.1, 0.9, 0.9, 0.98, 0.20, 30.0, 0.95, 1, NULL, 0, ?, ?)
        """,
        (
            photo_id,
            f"crop/{face_index}.jpg",
            f"aligned/{face_index}.jpg",
            f"context/{face_index}.jpg",
            NOW,
            NOW,
        ),
    )
    return int(face_cursor.lastrowid)


def _insert_person(conn: sqlite3.Connection, *, person_uuid: str, display_name: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO person(person_uuid, display_name, is_named, status, merged_into_person_id, created_at, updated_at)
        VALUES (?, ?, 1, 'active', NULL, ?, ?)
        """,
        (person_uuid, display_name, NOW, NOW),
    )
    return int(cursor.lastrowid)


def _insert_assignment_run(conn: sqlite3.Connection) -> int:
    session_cursor = conn.execute(
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
        VALUES ('scan_full', 'completed', 'manual_cli', NULL, ?, ?, NULL, ?, ?)
        """,
        (NOW, NOW, NOW, NOW),
    )
    session_id = int(session_cursor.lastrowid)
    run_cursor = conn.execute(
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
    return int(run_cursor.lastrowid)


def _insert_active_assignment(
    conn: sqlite3.Connection,
    *,
    person_id: int,
    face_observation_id: int,
    assignment_run_id: int,
) -> int:
    cursor = conn.execute(
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
        VALUES (?, ?, ?, 'hdbscan', 1, 0.99, 0.20, ?, ?)
        """,
        (person_id, face_observation_id, assignment_run_id, NOW, NOW),
    )
    return int(cursor.lastrowid)


def test_merge_migrates_exclusions_and_undo_restores(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    ids = _seed_people_fixture(db_path)

    service = PeopleService(SQLitePeopleRepository(db_path))
    merge = service.merge_people(selected_person_ids=[ids["person_1"], ids["person_2"]])

    assert merge.winner_person_id == ids["person_1"]

    with sqlite3.connect(db_path) as conn:
        loser_status = conn.execute(
            "SELECT status, merged_into_person_id FROM person WHERE id=?",
            (ids["person_2"],),
        ).fetchone()
        migrated_assignment = conn.execute(
            "SELECT person_id FROM person_face_assignment WHERE face_observation_id=? AND active=1",
            (ids["face_2"],),
        ).fetchone()
        exclusion_rows_after_merge = conn.execute(
            "SELECT person_id, active FROM person_face_exclusion WHERE face_observation_id=? ORDER BY person_id",
            (ids["face_2"],),
        ).fetchall()
        exclusion_delta_count = conn.execute(
            "SELECT COUNT(*) FROM merge_operation_exclusion_delta WHERE merge_operation_id=?",
            (merge.merge_operation_id,),
        ).fetchone()

    assert loser_status == ("merged", ids["person_1"])
    assert migrated_assignment == (ids["person_1"],)
    assert exclusion_rows_after_merge == [(ids["person_1"], 1), (ids["person_2"], 0)]
    assert exclusion_delta_count == (1,)

    undone = service.undo_last_merge()
    assert undone.merge_operation_id == merge.merge_operation_id
    assert undone.status == "undone"

    with sqlite3.connect(db_path) as conn:
        restored_loser = conn.execute(
            "SELECT status, merged_into_person_id FROM person WHERE id=?",
            (ids["person_2"],),
        ).fetchone()
        restored_assignment = conn.execute(
            "SELECT person_id FROM person_face_assignment WHERE face_observation_id=? AND active=1",
            (ids["face_2"],),
        ).fetchone()
        exclusion_rows_after_undo = conn.execute(
            "SELECT person_id, active FROM person_face_exclusion WHERE face_observation_id=? ORDER BY person_id",
            (ids["face_2"],),
        ).fetchall()

    assert restored_loser == ("active", None)
    assert restored_assignment == (ids["person_2"],)
    assert exclusion_rows_after_undo == [(ids["person_1"], 0), (ids["person_2"], 1)]


def test_tie_break_uses_first_selected_person_id(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    ids = _seed_people_fixture(db_path)

    service = PeopleService(SQLitePeopleRepository(db_path))
    merge = service.merge_people(selected_person_ids=[ids["person_2"], ids["person_1"]])

    assert merge.winner_person_id == ids["person_2"]


def test_only_last_merge_can_be_undone(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    ids = _seed_people_fixture(db_path)

    service = PeopleService(SQLitePeopleRepository(db_path))
    _merge_1 = service.merge_people(selected_person_ids=[ids["person_1"], ids["person_2"]])
    merge_2 = service.merge_people(selected_person_ids=[ids["person_1"], ids["person_3"]])

    undone = service.undo_last_merge()
    assert undone.merge_operation_id == merge_2.merge_operation_id

    with pytest.raises(MergeOperationNotFoundError):
        service.undo_last_merge()


def test_undo_does_not_override_winner_rename_after_merge(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    ids = _seed_people_fixture(db_path)

    service = PeopleService(SQLitePeopleRepository(db_path))
    merge = service.merge_people(selected_person_ids=[ids["person_1"], ids["person_2"]])
    assert merge.winner_person_id == ids["person_1"]

    renamed = service.rename_person(person_id=ids["person_1"], display_name="胜者新名")
    assert renamed.display_name == "胜者新名"

    undone = service.undo_last_merge()
    assert undone.merge_operation_id == merge.merge_operation_id
    assert undone.status == "undone"

    with sqlite3.connect(db_path) as conn:
        winner_row = conn.execute(
            "SELECT display_name, is_named, status, merged_into_person_id FROM person WHERE id=?",
            (ids["person_1"],),
        ).fetchone()
        loser_row = conn.execute(
            "SELECT status, merged_into_person_id FROM person WHERE id=?",
            (ids["person_2"],),
        ).fetchone()

    assert winner_row == ("胜者新名", 1, "active", None)
    assert loser_row == ("active", None)


def test_repository_merge_rejects_single_person_after_dedup(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    ids = _seed_people_fixture(db_path)
    repo = SQLitePeopleRepository(db_path)

    with pytest.raises(ValueError, match="至少需要 2"):
        repo.merge_people(selected_person_ids=[ids["person_1"], ids["person_1"]], now=NOW)
