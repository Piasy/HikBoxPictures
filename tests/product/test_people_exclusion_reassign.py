import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import sqlite3

from hikbox_pictures.product.scan.cluster_repository import ClusterRepository
from hikbox_pictures.product.scan.incremental_assignment_service import IncrementalAssignmentService
from hikbox_pictures.product.people.service import PeopleExcludeConflictError, PeopleService
from hikbox_pictures.product.people.repository import PeopleRepository

from tests.product.task6_test_support import (
    create_task6_workspace,
    embedding_from_seed,
    seed_face_observations,
    upsert_face_embeddings,
)


def test_exclude_faces_deactivates_assignments_activates_exclusions_and_marks_pending_reassign(
    tmp_path: Path,
) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    face_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (210, 180, 160)},
            {"asset_index": 1, "color": (212, 182, 162)},
        ],
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        person_id = _insert_person(conn, display_name="Alice")
        assignment_run_id = _insert_assignment_run(conn, scan_session_id=session_id)
        for face_id in face_ids:
            _insert_assignment(conn, person_id=person_id, face_observation_id=face_id, assignment_run_id=assignment_run_id)
        conn.commit()
    finally:
        conn.close()

    service = PeopleService(PeopleRepository(layout.library_db))
    result = service.exclude_faces(person_id=person_id, face_observation_ids=face_ids)

    conn = sqlite3.connect(layout.library_db)
    try:
        assignment_rows = conn.execute(
            """
            SELECT face_observation_id, active
            FROM person_face_assignment
            WHERE person_id=?
            ORDER BY face_observation_id ASC, id ASC
            """,
            (person_id,),
        ).fetchall()
        exclusion_rows = conn.execute(
            """
            SELECT person_id, face_observation_id, active
            FROM person_face_exclusion
            ORDER BY face_observation_id ASC, id ASC
            """
        ).fetchall()
        pending_rows = conn.execute(
            """
            SELECT id, pending_reassign
            FROM face_observation
            WHERE id IN (?, ?)
            ORDER BY id ASC
            """,
            (face_ids[0], face_ids[1]),
        ).fetchall()
    finally:
        conn.close()

    assert result.person_id == person_id
    assert result.face_observation_ids == face_ids
    assert [(int(row[0]), int(row[1])) for row in assignment_rows] == [
        (face_ids[0], 0),
        (face_ids[1], 0),
    ]
    assert [(int(row[0]), int(row[1]), int(row[2])) for row in exclusion_rows] == [
        (person_id, face_ids[0], 1),
        (person_id, face_ids[1], 1),
    ]
    assert [(int(row[0]), int(row[1])) for row in pending_rows] == [
        (face_ids[0], 1),
        (face_ids[1], 1),
    ]


def test_rename_person_allows_duplicate_display_name(tmp_path: Path) -> None:
    layout, _, _ = create_task6_workspace(tmp_path)
    conn = sqlite3.connect(layout.library_db)
    try:
        left_person_id = _insert_person(conn, display_name="Alice")
        right_person_id = _insert_person(conn, display_name="Bob")
        conn.commit()
    finally:
        conn.close()

    service = PeopleService(PeopleRepository(layout.library_db))
    renamed = service.rename_person(right_person_id, "Alice")

    conn = sqlite3.connect(layout.library_db)
    try:
        duplicate_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM person WHERE display_name='Alice' AND status='active'"
            ).fetchone()[0]
        )
        row = conn.execute(
            "SELECT display_name, is_named FROM person WHERE id=?",
            (right_person_id,),
        ).fetchone()
    finally:
        conn.close()

    assert renamed.id == right_person_id
    assert renamed.display_name == "Alice"
    assert renamed.is_named is True
    assert duplicate_count == 2
    assert (str(row[0]), int(row[1]), left_person_id) == ("Alice", 1, left_person_id)


def test_excluded_faces_are_hard_filtered_in_next_incremental_assignment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    face_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (220, 180, 160), "quality_score": 0.95},
            {"asset_index": 0, "color": (221, 181, 161), "quality_score": 0.94, "pending_reassign": True},
            {"asset_index": 1, "color": (222, 182, 162), "quality_score": 0.93, "pending_reassign": True},
        ],
    )
    anchor_face_id, excluded_face_id, sibling_face_id = face_ids
    shared_main = embedding_from_seed(3101)
    shared_flip = embedding_from_seed(3102)
    for face_id in face_ids:
        upsert_face_embeddings(
            layout.embedding_db,
            face_observation_id=face_id,
            main=shared_main,
            flip=shared_flip,
        )

    conn = sqlite3.connect(layout.library_db)
    try:
        person_id = _insert_person(conn, display_name="Alice")
        assignment_run_id = _insert_assignment_run(conn, scan_session_id=session_id)
        _insert_assignment(conn, person_id=person_id, face_observation_id=anchor_face_id, assignment_run_id=assignment_run_id)
        _insert_assignment(conn, person_id=person_id, face_observation_id=excluded_face_id, assignment_run_id=assignment_run_id)
        _insert_cluster(
            conn,
            person_id=person_id,
            assignment_run_id=assignment_run_id,
            member_face_ids=[anchor_face_id, excluded_face_id],
            rep_face_ids=[anchor_face_id],
        )
        conn.commit()
    finally:
        conn.close()

    PeopleService(PeopleRepository(layout.library_db)).exclude_face(person_id=person_id, face_observation_id=excluded_face_id)

    def fake_run(*, faces, params):
        ids = sorted(int(face["face_observation_id"]) for face in faces)
        if ids == sorted([excluded_face_id, sibling_face_id]):
            return {
                "clusters": [
                    {
                        "member_face_observation_ids": [excluded_face_id, sibling_face_id],
                        "representative_face_observation_ids": [excluded_face_id],
                    }
                ]
            }
        if ids == sorted([anchor_face_id, excluded_face_id, sibling_face_id]):
            return {
                "faces": [
                    {
                        "face_observation_id": anchor_face_id,
                        "person_temp_key": "keep-anchor",
                    },
                    {
                        "face_observation_id": excluded_face_id,
                        "person_temp_key": "new-person",
                    },
                    {
                        "face_observation_id": sibling_face_id,
                        "person_temp_key": "new-person",
                    },
                ]
            }
        raise AssertionError(f"未预期的 runtime 输入: {ids}")

    monkeypatch.setattr(
        "hikbox_pictures.product.scan.incremental_assignment_service.run_frozen_v5_assignment",
        fake_run,
    )
    service = IncrementalAssignmentService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        cluster_repo=ClusterRepository(layout.library_db),
    )

    result = service.run(
        assignment_run_id=assignment_run_id,
        face_observation_ids=[excluded_face_id, sibling_face_id],
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        active_assignments = conn.execute(
            """
            SELECT face_observation_id, person_id
            FROM person_face_assignment
            WHERE active=1
            ORDER BY face_observation_id ASC
            """
        ).fetchall()
        active_person_count = int(
            conn.execute("SELECT COUNT(*) FROM person WHERE status='active'").fetchone()[0]
        )
    finally:
        conn.close()

    assert result.attached_count == 2
    assert result.local_rebuild_count == 1
    assert [(int(row[0]), int(row[1])) for row in active_assignments] == [
        (anchor_face_id, person_id),
        (excluded_face_id, person_id + 1),
        (sibling_face_id, person_id + 1),
    ]
    assert active_person_count == 2


def test_exclude_faces_rejects_non_current_assignment_and_keeps_state_unchanged(tmp_path: Path) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    face_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (210, 180, 160)},
            {"asset_index": 1, "color": (212, 182, 162)},
        ],
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        alice_id = _insert_person(conn, display_name="Alice")
        bob_id = _insert_person(conn, display_name="Bob")
        assignment_run_id = _insert_assignment_run(conn, scan_session_id=session_id)
        _insert_assignment(conn, person_id=bob_id, face_observation_id=face_ids[0], assignment_run_id=assignment_run_id)
        conn.commit()
    finally:
        conn.close()

    service = PeopleService(PeopleRepository(layout.library_db))
    try:
        service.exclude_face(person_id=alice_id, face_observation_id=face_ids[0])
        raise AssertionError("预期 exclude_face 拒绝非当前归属的人脸")
    except PeopleExcludeConflictError:
        pass

    conn = sqlite3.connect(layout.library_db)
    try:
        active_assignment = conn.execute(
            """
            SELECT person_id
            FROM person_face_assignment
            WHERE face_observation_id=? AND active=1
            """,
            (face_ids[0],),
        ).fetchone()
        exclusion_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM person_face_exclusion WHERE face_observation_id=?",
                (face_ids[0],),
            ).fetchone()[0]
        )
        pending_reassign = int(
            conn.execute(
                "SELECT pending_reassign FROM face_observation WHERE id=?",
                (face_ids[0],),
            ).fetchone()[0]
        )
    finally:
        conn.close()

    assert int(active_assignment[0]) == bob_id
    assert exclusion_count == 0
    assert pending_reassign == 0


def test_exclude_faces_prunes_cluster_truth_layer_so_similar_new_face_is_not_attached_back(
    tmp_path: Path,
) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    face_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (220, 180, 160), "quality_score": 0.95},
            {"asset_index": 1, "color": (221, 181, 161), "quality_score": 0.94},
        ],
    )
    excluded_face_id, new_face_id = face_ids
    shared_main = embedding_from_seed(7101)
    shared_flip = embedding_from_seed(7102)
    upsert_face_embeddings(layout.embedding_db, face_observation_id=excluded_face_id, main=shared_main, flip=shared_flip)
    upsert_face_embeddings(layout.embedding_db, face_observation_id=new_face_id, main=shared_main, flip=shared_flip)

    conn = sqlite3.connect(layout.library_db)
    try:
        person_id = _insert_person(conn, display_name="Alice")
        assignment_run_id = _insert_assignment_run(conn, scan_session_id=session_id)
        _insert_assignment(conn, person_id=person_id, face_observation_id=excluded_face_id, assignment_run_id=assignment_run_id)
        _insert_cluster(
            conn,
            person_id=person_id,
            assignment_run_id=assignment_run_id,
            member_face_ids=[excluded_face_id],
            rep_face_ids=[excluded_face_id],
        )
        conn.commit()
    finally:
        conn.close()

    PeopleService(PeopleRepository(layout.library_db)).exclude_face(person_id=person_id, face_observation_id=excluded_face_id)

    service = IncrementalAssignmentService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        cluster_repo=ClusterRepository(layout.library_db),
    )
    result = service.run(
        assignment_run_id=assignment_run_id,
        face_observation_ids=[new_face_id],
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        active_assignment = conn.execute(
            """
            SELECT person_id
            FROM person_face_assignment
            WHERE face_observation_id=? AND active=1
            """,
            (new_face_id,),
        ).fetchone()
        cluster_rows = conn.execute(
            """
            SELECT c.id, m.face_observation_id, r.face_observation_id
            FROM face_cluster AS c
            LEFT JOIN face_cluster_member AS m ON m.face_cluster_id = c.id
            LEFT JOIN face_cluster_rep_face AS r ON r.face_cluster_id = c.id
            WHERE c.person_id=? AND c.status='active'
            ORDER BY c.id ASC
            """,
            (person_id,),
        ).fetchall()
    finally:
        conn.close()

    assert result.attached_count == 0
    assert active_assignment is None
    assert [(int(row[0]), row[1], row[2]) for row in cluster_rows] == [
        (1, None, None),
    ]


def _insert_person(conn: sqlite3.Connection, *, display_name: str | None = None) -> int:
    cursor = conn.execute(
        """
        INSERT INTO person(
          person_uuid, display_name, is_named, status, merged_into_person_id, created_at, updated_at
        ) VALUES (?, ?, ?, 'active', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (str(uuid.uuid4()), display_name, 1 if display_name else 0),
    )
    return int(cursor.lastrowid)


def _insert_assignment_run(conn: sqlite3.Connection, *, scan_session_id: int) -> int:
    cursor = conn.execute(
        """
        INSERT INTO assignment_run(
          scan_session_id, algorithm_version, param_snapshot_json, run_kind, started_at, finished_at, status, updated_at
        ) VALUES (?, 'test_manual_people', '{}', 'scan_full', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'completed', CURRENT_TIMESTAMP)
        """,
        (scan_session_id,),
    )
    return int(cursor.lastrowid)


def _insert_assignment(
    conn: sqlite3.Connection,
    *,
    person_id: int,
    face_observation_id: int,
    assignment_run_id: int,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO person_face_assignment(
          person_id, face_observation_id, assignment_run_id, assignment_source,
          active, confidence, margin, created_at, updated_at
        ) VALUES (?, ?, ?, 'hdbscan', 1, 0.95, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (person_id, face_observation_id, assignment_run_id),
    )
    return int(cursor.lastrowid)


def _insert_cluster(
    conn: sqlite3.Connection,
    *,
    person_id: int,
    assignment_run_id: int,
    member_face_ids: list[int],
    rep_face_ids: list[int],
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO face_cluster(
          cluster_uuid, person_id, status, rebuild_scope,
          created_assignment_run_id, updated_assignment_run_id, created_at, updated_at
        ) VALUES (?, ?, 'active', 'full', ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (str(uuid.uuid4()), person_id, assignment_run_id, assignment_run_id),
    )
    cluster_id = int(cursor.lastrowid)
    for face_id in member_face_ids:
        conn.execute(
            """
            INSERT INTO face_cluster_member(face_cluster_id, face_observation_id, assignment_run_id, created_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (cluster_id, face_id, assignment_run_id),
        )
    for rank, face_id in enumerate(rep_face_ids, start=1):
        conn.execute(
            """
            INSERT INTO face_cluster_rep_face(face_cluster_id, face_observation_id, rep_rank, assignment_run_id, created_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (cluster_id, face_id, rank, assignment_run_id),
        )
    return cluster_id
