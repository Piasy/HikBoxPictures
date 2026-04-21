from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hikbox_pictures.product.db.schema_bootstrap import bootstrap_library_schema
from hikbox_pictures.product.people.repository import SQLitePeopleRepository
from hikbox_pictures.product.people.service import PeopleService

NOW = "2026-04-22T00:00:00+00:00"


def _prepare_db(db_path: Path) -> tuple[int, int, int, int, int]:
    bootstrap_library_schema(db_path)
    _assert_people_maintenance_schema_exists(db_path)
    with sqlite3.connect(db_path) as conn:
        source_id = _insert_source(conn)
        face_1 = _insert_face_observation(conn, source_id=source_id, face_index=0)
        face_2 = _insert_face_observation(conn, source_id=source_id, face_index=1)
        person_1 = _insert_person(conn, person_uuid="person-1", display_name="A")
        person_2 = _insert_person(conn, person_uuid="person-2", display_name="B")
        assignment_run_id = _insert_assignment_run(conn)
        _insert_active_assignment(
            conn,
            person_id=person_1,
            face_observation_id=face_1,
            assignment_run_id=assignment_run_id,
        )
        _insert_active_assignment(
            conn,
            person_id=person_1,
            face_observation_id=face_2,
            assignment_run_id=assignment_run_id,
        )
        conn.commit()
    return person_1, person_2, face_1, face_2, assignment_run_id


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


def _insert_person(conn: sqlite3.Connection, *, person_uuid: str, display_name: str | None) -> int:
    cursor = conn.execute(
        """
        INSERT INTO person(person_uuid, display_name, is_named, status, merged_into_person_id, created_at, updated_at)
        VALUES (?, ?, ?, 'active', NULL, ?, ?)
        """,
        (person_uuid, display_name, int(display_name is not None), NOW, NOW),
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


def test_exclude_transaction_deactivates_assignment_activates_exclusion_and_sets_pending_reassign(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    person_1, _person_2, face_1, _face_2, _run_id = _prepare_db(db_path)

    service = PeopleService(SQLitePeopleRepository(db_path))
    result = service.exclude_assignment(person_id=person_1, face_observation_id=face_1)

    with sqlite3.connect(db_path) as conn:
        assignment_active = conn.execute(
            "SELECT active FROM person_face_assignment WHERE person_id=? AND face_observation_id=?",
            (person_1, face_1),
        ).fetchone()
        exclusion_active = conn.execute(
            "SELECT active FROM person_face_exclusion WHERE person_id=? AND face_observation_id=?",
            (person_1, face_1),
        ).fetchone()
        pending_reassign = conn.execute(
            "SELECT pending_reassign FROM face_observation WHERE id=?",
            (face_1,),
        ).fetchone()

    assert result.person_id == person_1
    assert result.face_observation_id == face_1
    assert result.pending_reassign == 1
    assert assignment_active == (0,)
    assert exclusion_active == (1,)
    assert pending_reassign == (1,)


def test_exclude_assignments_batch_updates_all_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    person_1, _person_2, face_1, face_2, _run_id = _prepare_db(db_path)

    service = PeopleService(SQLitePeopleRepository(db_path))
    result = service.exclude_assignments(person_id=person_1, face_observation_ids=[face_1, face_2])

    assert result.person_id == person_1
    assert result.excluded_count == 2

    with sqlite3.connect(db_path) as conn:
        assignment_rows = conn.execute(
            "SELECT face_observation_id, active FROM person_face_assignment WHERE person_id=? ORDER BY face_observation_id",
            (person_1,),
        ).fetchall()
        exclusion_rows = conn.execute(
            "SELECT face_observation_id, active FROM person_face_exclusion WHERE person_id=? ORDER BY face_observation_id",
            (person_1,),
        ).fetchall()
        pending_rows = conn.execute(
            "SELECT id, pending_reassign FROM face_observation WHERE id IN (?, ?) ORDER BY id",
            (face_1, face_2),
        ).fetchall()

    assert assignment_rows == [(face_1, 0), (face_2, 0)]
    assert exclusion_rows == [(face_1, 1), (face_2, 1)]
    assert pending_rows == [(face_1, 1), (face_2, 1)]


def test_rename_allows_duplicate_display_name(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    person_1, person_2, _face_1, _face_2, _run_id = _prepare_db(db_path)

    service = PeopleService(SQLitePeopleRepository(db_path))
    renamed = service.rename_person(person_id=person_2, display_name="A")

    assert renamed.id == person_2
    assert renamed.display_name == "A"
    assert renamed.is_named is True

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, display_name, is_named FROM person WHERE id IN (?, ?) ORDER BY id",
            (person_1, person_2),
        ).fetchall()

    assert rows == [(person_1, "A", 1), (person_2, "A", 1)]


def test_exclude_assignment_conflicts_when_exclusion_already_active(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    person_1, _person_2, face_1, _face_2, _run_id = _prepare_db(db_path)

    service = PeopleService(SQLitePeopleRepository(db_path))
    service.exclude_assignment(person_id=person_1, face_observation_id=face_1)

    with pytest.raises(ValueError, match="已排除"):
        service.exclude_assignment(person_id=person_1, face_observation_id=face_1)


def test_repository_rename_person_rejects_blank_display_name(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    person_1, _person_2, _face_1, _face_2, _run_id = _prepare_db(db_path)
    repo = SQLitePeopleRepository(db_path)

    with pytest.raises(ValueError, match="display_name 不能为空"):
        repo.rename_person(person_id=person_1, display_name="   ", now=NOW)


def test_repository_exclude_assignments_rejects_empty_list(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    person_1, _person_2, _face_1, _face_2, _run_id = _prepare_db(db_path)
    repo = SQLitePeopleRepository(db_path)

    with pytest.raises(ValueError, match="face_observation_ids 不能为空"):
        repo.exclude_assignments(person_id=person_1, face_observation_ids=[], now=NOW)


def test_exclude_assignments_rolls_back_all_when_one_face_invalid(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    person_1, _person_2, face_1, face_2, _run_id = _prepare_db(db_path)

    service = PeopleService(SQLitePeopleRepository(db_path))
    with pytest.raises(ValueError, match="未找到 active assignment"):
        service.exclude_assignments(
            person_id=person_1,
            face_observation_ids=[face_1, 999999],
        )

    with sqlite3.connect(db_path) as conn:
        assignment_rows = conn.execute(
            "SELECT face_observation_id, active FROM person_face_assignment WHERE person_id=? ORDER BY face_observation_id",
            (person_1,),
        ).fetchall()
        exclusion_rows = conn.execute(
            "SELECT person_id, face_observation_id, active FROM person_face_exclusion ORDER BY id",
        ).fetchall()
        pending_rows = conn.execute(
            "SELECT id, pending_reassign FROM face_observation WHERE id IN (?, ?) ORDER BY id",
            (face_1, face_2),
        ).fetchall()

    assert assignment_rows == [(face_1, 1), (face_2, 1)]
    assert exclusion_rows == []
    assert pending_rows == [(face_1, 0), (face_2, 0)]
