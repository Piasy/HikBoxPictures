import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import sqlite3

from hikbox_pictures.product.people.repository import PeopleRepository
from hikbox_pictures.product.people.service import PeopleService, PeopleUndoMergeConflictError
from hikbox_pictures.product.engine.param_snapshot import build_frozen_v5_param_snapshot
from hikbox_pictures.product.scan.assignment_stage import AssignmentStageService
from hikbox_pictures.product.scan.cluster_repository import ClusterRepository
from hikbox_pictures.product.scan.incremental_assignment_service import IncrementalAssignmentService

from tests.product.task6_test_support import (
    create_task6_workspace,
    embedding_from_seed,
    seed_face_observations,
    upsert_face_embeddings,
)


def test_merge_migrates_exclusions_and_undo_restores(tmp_path: Path) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    face_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (220, 180, 160)},
            {"asset_index": 0, "color": (218, 178, 158)},
            {"asset_index": 1, "color": (150, 210, 220)},
        ],
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        winner_person_id = _insert_person(conn, display_name="Winner")
        loser_person_id = _insert_person(conn, display_name="Loser")
        assignment_run_id = _insert_assignment_run(conn, scan_session_id=session_id)
        _insert_assignment(conn, person_id=winner_person_id, face_observation_id=face_ids[0], assignment_run_id=assignment_run_id)
        _insert_assignment(conn, person_id=winner_person_id, face_observation_id=face_ids[1], assignment_run_id=assignment_run_id)
        _insert_assignment(conn, person_id=loser_person_id, face_observation_id=face_ids[2], assignment_run_id=assignment_run_id)
        _insert_exclusion(conn, person_id=loser_person_id, face_observation_id=face_ids[1], active=True)
        conn.commit()
    finally:
        conn.close()

    service = PeopleService(PeopleRepository(layout.library_db))
    merge_result = service.merge_people([loser_person_id, winner_person_id])

    conn = sqlite3.connect(layout.library_db)
    try:
        merged_persons = conn.execute(
            "SELECT id, status, merged_into_person_id FROM person ORDER BY id ASC"
        ).fetchall()
        active_assignments_after_merge = conn.execute(
            """
            SELECT face_observation_id, person_id, assignment_source
            FROM person_face_assignment
            WHERE active=1
            ORDER BY face_observation_id ASC
            """
        ).fetchall()
        active_exclusions_after_merge = conn.execute(
            """
            SELECT person_id, face_observation_id
            FROM person_face_exclusion
            WHERE active=1
            ORDER BY person_id ASC, face_observation_id ASC
            """
        ).fetchall()
        exclusion_delta_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM merge_operation_exclusion_delta WHERE merge_operation_id=?",
                (merge_result.merge_operation_id,),
            ).fetchone()[0]
        )
        pending_rows_after_merge = conn.execute(
            "SELECT id, pending_reassign FROM face_observation ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()

    assert merge_result.winner_person_id == winner_person_id
    assert [(int(row[0]), str(row[1]), row[2]) for row in merged_persons] == [
        (winner_person_id, "active", None),
        (loser_person_id, "merged", winner_person_id),
    ]
    assert [(int(row[0]), int(row[1]), str(row[2])) for row in active_assignments_after_merge] == [
        (face_ids[0], winner_person_id, "hdbscan"),
        (face_ids[2], winner_person_id, "merge"),
    ]
    assert [(int(row[0]), int(row[1])) for row in active_exclusions_after_merge] == [
        (winner_person_id, face_ids[1]),
    ]
    assert [(int(row[0]), int(row[1])) for row in pending_rows_after_merge] == [
        (face_ids[0], 0),
        (face_ids[1], 1),
        (face_ids[2], 0),
    ]
    assert exclusion_delta_count == 2

    undo_result = service.undo_last_merge()

    conn = sqlite3.connect(layout.library_db)
    try:
        persons_after_undo = conn.execute(
            "SELECT id, status, merged_into_person_id FROM person ORDER BY id ASC"
        ).fetchall()
        active_assignments_after_undo = conn.execute(
            """
            SELECT face_observation_id, person_id, assignment_source
            FROM person_face_assignment
            WHERE active=1
            ORDER BY face_observation_id ASC
            """
        ).fetchall()
        active_exclusions_after_undo = conn.execute(
            """
            SELECT person_id, face_observation_id
            FROM person_face_exclusion
            WHERE active=1
            ORDER BY person_id ASC, face_observation_id ASC
            """
        ).fetchall()
        merge_status = conn.execute(
            "SELECT status FROM merge_operation WHERE id=?",
            (undo_result.merge_operation_id,),
        ).fetchone()
        pending_rows_after_undo = conn.execute(
            "SELECT id, pending_reassign FROM face_observation ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()

    assert undo_result.merge_operation_id == merge_result.merge_operation_id
    assert [(int(row[0]), str(row[1]), row[2]) for row in persons_after_undo] == [
        (winner_person_id, "active", None),
        (loser_person_id, "active", None),
    ]
    assert [(int(row[0]), int(row[1]), str(row[2])) for row in active_assignments_after_undo] == [
        (face_ids[0], winner_person_id, "hdbscan"),
        (face_ids[1], winner_person_id, "hdbscan"),
        (face_ids[2], loser_person_id, "hdbscan"),
    ]
    assert [(int(row[0]), int(row[1])) for row in active_exclusions_after_undo] == [
        (loser_person_id, face_ids[1]),
    ]
    assert [(int(row[0]), int(row[1])) for row in pending_rows_after_undo] == [
        (face_ids[0], 0),
        (face_ids[1], 0),
        (face_ids[2], 0),
    ]
    assert str(merge_status[0]) == "undone"


def test_tie_break_uses_first_selected_person_id(tmp_path: Path) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    face_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (200, 180, 160)},
            {"asset_index": 1, "color": (160, 200, 220)},
        ],
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        first_person_id = _insert_person(conn, display_name="First")
        second_person_id = _insert_person(conn, display_name="Second")
        assignment_run_id = _insert_assignment_run(conn, scan_session_id=session_id)
        _insert_assignment(conn, person_id=first_person_id, face_observation_id=face_ids[0], assignment_run_id=assignment_run_id)
        _insert_assignment(conn, person_id=second_person_id, face_observation_id=face_ids[1], assignment_run_id=assignment_run_id)
        conn.commit()
    finally:
        conn.close()

    service = PeopleService(PeopleRepository(layout.library_db))
    result = service.merge_people([second_person_id, first_person_id])

    assert result.winner_person_id == second_person_id


def test_only_last_merge_can_be_undone(tmp_path: Path) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    face_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (220, 180, 160)},
            {"asset_index": 0, "color": (218, 178, 158)},
            {"asset_index": 1, "color": (150, 210, 220)},
        ],
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        person_a = _insert_person(conn, display_name="A")
        person_b = _insert_person(conn, display_name="B")
        person_c = _insert_person(conn, display_name="C")
        assignment_run_id = _insert_assignment_run(conn, scan_session_id=session_id)
        _insert_assignment(conn, person_id=person_a, face_observation_id=face_ids[0], assignment_run_id=assignment_run_id)
        _insert_assignment(conn, person_id=person_b, face_observation_id=face_ids[1], assignment_run_id=assignment_run_id)
        _insert_assignment(conn, person_id=person_c, face_observation_id=face_ids[2], assignment_run_id=assignment_run_id)
        conn.commit()
    finally:
        conn.close()

    service = PeopleService(PeopleRepository(layout.library_db))
    first_merge = service.merge_people([person_a, person_b])
    second_merge = service.merge_people([person_a, person_c])
    undone = service.undo_last_merge()

    conn = sqlite3.connect(layout.library_db)
    try:
        statuses = conn.execute(
            "SELECT id, status FROM merge_operation ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()

    assert first_merge.merge_operation_id != second_merge.merge_operation_id
    assert undone.merge_operation_id == second_merge.merge_operation_id
    assert [(int(row[0]), str(row[1])) for row in statuses] == [
        (first_merge.merge_operation_id, "applied"),
        (second_merge.merge_operation_id, "undone"),
    ]


def test_merge_repoints_active_clusters_and_incremental_attach_uses_winner(tmp_path: Path) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    face_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (220, 180, 160), "quality_score": 0.95},
            {"asset_index": 0, "color": (150, 210, 220), "quality_score": 0.95},
            {"asset_index": 1, "color": (151, 211, 221), "quality_score": 0.94},
        ],
    )
    winner_anchor_face_id, loser_anchor_face_id, new_face_id = face_ids
    winner_main = embedding_from_seed(4101)
    winner_flip = embedding_from_seed(4102)
    loser_main = embedding_from_seed(4201)
    loser_flip = embedding_from_seed(4202)
    upsert_face_embeddings(layout.embedding_db, face_observation_id=winner_anchor_face_id, main=winner_main, flip=winner_flip)
    upsert_face_embeddings(layout.embedding_db, face_observation_id=loser_anchor_face_id, main=loser_main, flip=loser_flip)
    upsert_face_embeddings(layout.embedding_db, face_observation_id=new_face_id, main=loser_main, flip=loser_flip)

    conn = sqlite3.connect(layout.library_db)
    try:
        winner_person_id = _insert_person(conn, display_name="Winner")
        loser_person_id = _insert_person(conn, display_name="Loser")
        assignment_run_id = _insert_assignment_run(conn, scan_session_id=session_id)
        _insert_assignment(conn, person_id=winner_person_id, face_observation_id=winner_anchor_face_id, assignment_run_id=assignment_run_id)
        _insert_assignment(conn, person_id=loser_person_id, face_observation_id=loser_anchor_face_id, assignment_run_id=assignment_run_id)
        _insert_cluster(
            conn,
            person_id=winner_person_id,
            assignment_run_id=assignment_run_id,
            member_face_ids=[winner_anchor_face_id],
            rep_face_ids=[winner_anchor_face_id],
        )
        _insert_cluster(
            conn,
            person_id=loser_person_id,
            assignment_run_id=assignment_run_id,
            member_face_ids=[loser_anchor_face_id],
            rep_face_ids=[loser_anchor_face_id],
        )
        conn.commit()
    finally:
        conn.close()

    PeopleService(PeopleRepository(layout.library_db)).merge_people([winner_person_id, loser_person_id])

    conn = sqlite3.connect(layout.library_db)
    try:
        active_clusters_after_merge = conn.execute(
            "SELECT id, person_id FROM face_cluster WHERE status='active' ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()

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
            SELECT person_id, assignment_source
            FROM person_face_assignment
            WHERE face_observation_id=? AND active=1
            """,
            (new_face_id,),
        ).fetchone()
    finally:
        conn.close()

    assert [(int(row[0]), int(row[1])) for row in active_clusters_after_merge] == [
        (1, winner_person_id),
        (2, winner_person_id),
    ]
    assert result.attached_count == 1
    assert (int(active_assignment[0]), str(active_assignment[1])) == (winner_person_id, "person_consensus")


def test_merge_and_undo_do_not_create_fake_assignment_runs_or_break_incremental_mode(
    tmp_path: Path,
) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    face_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (210, 180, 160), "quality_score": 0.95},
            {"asset_index": 1, "color": (150, 210, 220), "quality_score": 0.95},
        ],
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        winner_person_id = _insert_person(conn, display_name="Winner")
        loser_person_id = _insert_person(conn, display_name="Loser")
        real_assignment_run_id = _insert_real_assignment_run(conn, scan_session_id=session_id)
        _insert_assignment(conn, person_id=winner_person_id, face_observation_id=face_ids[0], assignment_run_id=real_assignment_run_id)
        _insert_assignment(conn, person_id=loser_person_id, face_observation_id=face_ids[1], assignment_run_id=real_assignment_run_id)
        _insert_cluster(
            conn,
            person_id=winner_person_id,
            assignment_run_id=real_assignment_run_id,
            member_face_ids=[face_ids[0]],
            rep_face_ids=[face_ids[0]],
        )
        _insert_cluster(
            conn,
            person_id=loser_person_id,
            assignment_run_id=real_assignment_run_id,
            member_face_ids=[face_ids[1]],
            rep_face_ids=[face_ids[1]],
        )
        next_session_id = _insert_scan_session(conn, run_kind="scan_incremental")
        conn.commit()
    finally:
        conn.close()

    stage = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    )
    snapshot = build_frozen_v5_param_snapshot()
    assert stage._should_run_incremental(
        scan_session_id=next_session_id,
        run_kind="scan_incremental",
        param_snapshot=snapshot,
    )


def test_merge_keeps_winner_exclusion_and_does_not_reassign_conflicting_loser_face(tmp_path: Path) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    face_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (220, 180, 160)},
            {"asset_index": 1, "color": (218, 178, 158)},
        ],
    )
    winner_face_id, loser_face_id = face_ids

    conn = sqlite3.connect(layout.library_db)
    try:
        winner_person_id = _insert_person(conn, display_name="Winner")
        loser_person_id = _insert_person(conn, display_name="Loser")
        assignment_run_id = _insert_assignment_run(conn, scan_session_id=session_id)
        _insert_assignment(conn, person_id=winner_person_id, face_observation_id=winner_face_id, assignment_run_id=assignment_run_id)
        _insert_assignment(conn, person_id=loser_person_id, face_observation_id=loser_face_id, assignment_run_id=assignment_run_id)
        _insert_exclusion(conn, person_id=winner_person_id, face_observation_id=loser_face_id, active=True)
        conn.commit()
    finally:
        conn.close()

    PeopleService(PeopleRepository(layout.library_db)).merge_people([winner_person_id, loser_person_id])

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
        active_exclusions = conn.execute(
            """
            SELECT person_id, face_observation_id
            FROM person_face_exclusion
            WHERE active=1
            ORDER BY person_id ASC, face_observation_id ASC
            """
        ).fetchall()
        pending_rows = conn.execute(
            "SELECT id, pending_reassign FROM face_observation ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()

    assert [(int(row[0]), int(row[1])) for row in active_assignments] == [
        (winner_face_id, winner_person_id),
    ]
    assert [(int(row[0]), int(row[1])) for row in active_exclusions] == [
        (winner_person_id, loser_face_id),
    ]
    assert [(int(row[0]), int(row[1])) for row in pending_rows] == [
        (winner_face_id, 0),
        (loser_face_id, 1),
    ]


def test_merge_migrated_exclusion_deactivates_conflicting_winner_assignment(tmp_path: Path) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    face_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (220, 180, 160)},
            {"asset_index": 1, "color": (218, 178, 158)},
        ],
    )
    winner_face_id, loser_face_id = face_ids

    conn = sqlite3.connect(layout.library_db)
    try:
        winner_person_id = _insert_person(conn, display_name="Winner")
        loser_person_id = _insert_person(conn, display_name="Loser")
        assignment_run_id = _insert_assignment_run(conn, scan_session_id=session_id)
        _insert_assignment(conn, person_id=winner_person_id, face_observation_id=winner_face_id, assignment_run_id=assignment_run_id)
        _insert_assignment(conn, person_id=loser_person_id, face_observation_id=loser_face_id, assignment_run_id=assignment_run_id)
        _insert_exclusion(conn, person_id=loser_person_id, face_observation_id=winner_face_id, active=True)
        conn.commit()
    finally:
        conn.close()

    PeopleService(PeopleRepository(layout.library_db)).merge_people([winner_person_id, loser_person_id])

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
        active_exclusions = conn.execute(
            """
            SELECT person_id, face_observation_id
            FROM person_face_exclusion
            WHERE active=1
            ORDER BY person_id ASC, face_observation_id ASC
            """
        ).fetchall()
        pending_rows = conn.execute(
            "SELECT id, pending_reassign FROM face_observation ORDER BY id ASC"
        ).fetchall()
    finally:
        conn.close()

    assert [(int(row[0]), int(row[1])) for row in active_assignments] == [
        (loser_face_id, winner_person_id),
    ]
    assert [(int(row[0]), int(row[1])) for row in active_exclusions] == [
        (winner_person_id, winner_face_id),
    ]
    assert [(int(row[0]), int(row[1])) for row in pending_rows] == [
        (winner_face_id, 1),
        (loser_face_id, 0),
    ]


def test_conflict_merge_prunes_cluster_evidence_so_incremental_does_not_attach_back_to_winner(
    tmp_path: Path,
) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    face_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (220, 180, 160), "quality_score": 0.95},
            {"asset_index": 1, "color": (218, 178, 158), "quality_score": 0.95},
            {"asset_index": 1, "color": (219, 179, 159), "quality_score": 0.94},
        ],
    )
    winner_face_id, loser_face_id, new_face_id = face_ids
    winner_main = embedding_from_seed(5101)
    winner_flip = embedding_from_seed(5102)
    loser_main = embedding_from_seed(5201)
    loser_flip = embedding_from_seed(5202)
    upsert_face_embeddings(layout.embedding_db, face_observation_id=winner_face_id, main=winner_main, flip=winner_flip)
    upsert_face_embeddings(layout.embedding_db, face_observation_id=loser_face_id, main=loser_main, flip=loser_flip)
    upsert_face_embeddings(layout.embedding_db, face_observation_id=new_face_id, main=loser_main, flip=loser_flip)

    conn = sqlite3.connect(layout.library_db)
    try:
        winner_person_id = _insert_person(conn, display_name="Winner")
        loser_person_id = _insert_person(conn, display_name="Loser")
        assignment_run_id = _insert_assignment_run(conn, scan_session_id=session_id)
        _insert_assignment(conn, person_id=winner_person_id, face_observation_id=winner_face_id, assignment_run_id=assignment_run_id)
        _insert_assignment(conn, person_id=loser_person_id, face_observation_id=loser_face_id, assignment_run_id=assignment_run_id)
        _insert_exclusion(conn, person_id=winner_person_id, face_observation_id=loser_face_id, active=True)
        _insert_cluster(
            conn,
            person_id=winner_person_id,
            assignment_run_id=assignment_run_id,
            member_face_ids=[winner_face_id],
            rep_face_ids=[winner_face_id],
        )
        _insert_cluster(
            conn,
            person_id=loser_person_id,
            assignment_run_id=assignment_run_id,
            member_face_ids=[loser_face_id],
            rep_face_ids=[loser_face_id],
        )
        conn.commit()
    finally:
        conn.close()

    PeopleService(PeopleRepository(layout.library_db)).merge_people([winner_person_id, loser_person_id])

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
        active_cluster_members = conn.execute(
            """
            SELECT c.id, m.face_observation_id
            FROM face_cluster AS c
            LEFT JOIN face_cluster_member AS m ON m.face_cluster_id = c.id
            WHERE c.status='active' AND c.person_id=?
            ORDER BY c.id ASC, m.face_observation_id ASC
            """,
            (winner_person_id,),
        ).fetchall()
    finally:
        conn.close()

    assert result.attached_count == 0
    assert active_assignment is None
    assert [(int(row[0]), None if row[1] is None else int(row[1])) for row in active_cluster_members] == [
        (1, winner_face_id),
        (2, None),
    ]


def test_undo_last_merge_rejects_drifted_reassignment_with_domain_error(tmp_path: Path) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    face_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (220, 180, 160)},
            {"asset_index": 1, "color": (218, 178, 158)},
        ],
    )
    winner_face_id, loser_face_id = face_ids

    conn = sqlite3.connect(layout.library_db)
    try:
        winner_person_id = _insert_person(conn, display_name="Winner")
        loser_person_id = _insert_person(conn, display_name="Loser")
        third_person_id = _insert_person(conn, display_name="Third")
        assignment_run_id = _insert_assignment_run(conn, scan_session_id=session_id)
        _insert_assignment(conn, person_id=winner_person_id, face_observation_id=winner_face_id, assignment_run_id=assignment_run_id)
        _insert_assignment(conn, person_id=loser_person_id, face_observation_id=loser_face_id, assignment_run_id=assignment_run_id)
        _insert_exclusion(conn, person_id=winner_person_id, face_observation_id=loser_face_id, active=True)
        conn.commit()
    finally:
        conn.close()

    service = PeopleService(PeopleRepository(layout.library_db))
    merge_result = service.merge_people([winner_person_id, loser_person_id])

    conn = sqlite3.connect(layout.library_db)
    try:
        _insert_assignment(
            conn,
            person_id=third_person_id,
            face_observation_id=loser_face_id,
            assignment_run_id=assignment_run_id,
        )
        conn.commit()
    finally:
        conn.close()

    try:
        service.undo_last_merge()
        raise AssertionError("预期 undo_last_merge 拒绝 merge 后漂移的重新归属")
    except PeopleUndoMergeConflictError:
        pass

    conn = sqlite3.connect(layout.library_db)
    try:
        merge_status = conn.execute(
            "SELECT status FROM merge_operation WHERE id=?",
            (merge_result.merge_operation_id,),
        ).fetchone()
        active_assignment = conn.execute(
            """
            SELECT person_id
            FROM person_face_assignment
            WHERE face_observation_id=? AND active=1
            """,
            (loser_face_id,),
        ).fetchone()
    finally:
        conn.close()

    assert str(merge_status[0]) == "applied"
    assert int(active_assignment[0]) == third_person_id


def test_undo_last_merge_rejects_cluster_drift_after_post_merge_incremental_attach(tmp_path: Path) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    face_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (220, 180, 160), "quality_score": 0.95},
            {"asset_index": 1, "color": (218, 178, 158), "quality_score": 0.95},
            {"asset_index": 1, "color": (219, 179, 159), "quality_score": 0.94},
        ],
    )
    winner_face_id, loser_face_id, new_face_id = face_ids
    winner_main = embedding_from_seed(8101)
    winner_flip = embedding_from_seed(8102)
    loser_main = embedding_from_seed(8201)
    loser_flip = embedding_from_seed(8202)
    upsert_face_embeddings(layout.embedding_db, face_observation_id=winner_face_id, main=winner_main, flip=winner_flip)
    upsert_face_embeddings(layout.embedding_db, face_observation_id=loser_face_id, main=loser_main, flip=loser_flip)
    upsert_face_embeddings(layout.embedding_db, face_observation_id=new_face_id, main=winner_main, flip=winner_flip)

    conn = sqlite3.connect(layout.library_db)
    try:
        winner_person_id = _insert_person(conn, display_name="Winner")
        loser_person_id = _insert_person(conn, display_name="Loser")
        assignment_run_id = _insert_assignment_run(conn, scan_session_id=session_id)
        _insert_assignment(conn, person_id=winner_person_id, face_observation_id=winner_face_id, assignment_run_id=assignment_run_id)
        _insert_assignment(conn, person_id=loser_person_id, face_observation_id=loser_face_id, assignment_run_id=assignment_run_id)
        _insert_cluster(
            conn,
            person_id=winner_person_id,
            assignment_run_id=assignment_run_id,
            member_face_ids=[winner_face_id],
            rep_face_ids=[winner_face_id],
        )
        _insert_cluster(
            conn,
            person_id=loser_person_id,
            assignment_run_id=assignment_run_id,
            member_face_ids=[loser_face_id],
            rep_face_ids=[loser_face_id],
        )
        conn.commit()
    finally:
        conn.close()

    service = PeopleService(PeopleRepository(layout.library_db))
    merge_result = service.merge_people([winner_person_id, loser_person_id])

    incremental = IncrementalAssignmentService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        cluster_repo=ClusterRepository(layout.library_db),
    )
    incremental_result = incremental.run(
        assignment_run_id=assignment_run_id,
        face_observation_ids=[new_face_id],
    )
    assert incremental_result.attached_count == 1

    try:
        service.undo_last_merge()
        raise AssertionError("预期 undo_last_merge 拒绝 merge 后 cluster 漂移")
    except PeopleUndoMergeConflictError:
        pass

    conn = sqlite3.connect(layout.library_db)
    try:
        merge_status = conn.execute(
            "SELECT status FROM merge_operation WHERE id=?",
            (merge_result.merge_operation_id,),
        ).fetchone()
        active_assignment = conn.execute(
            """
            SELECT person_id
            FROM person_face_assignment
            WHERE face_observation_id=? AND active=1
            """,
            (new_face_id,),
        ).fetchone()
    finally:
        conn.close()

    assert str(merge_status[0]) == "applied"
    assert int(active_assignment[0]) == winner_person_id


def test_undo_last_merge_rejects_cluster_drift_when_winner_had_no_person_delta_before_fix(tmp_path: Path) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    face_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (220, 180, 160), "quality_score": 0.95},
            {"asset_index": 1, "color": (218, 178, 158), "quality_score": 0.95},
            {"asset_index": 1, "color": (219, 179, 159), "quality_score": 0.94},
        ],
    )
    winner_face_id, loser_face_id, new_face_id = face_ids
    winner_main = embedding_from_seed(9101)
    winner_flip = embedding_from_seed(9102)
    loser_main = embedding_from_seed(9201)
    loser_flip = embedding_from_seed(9202)
    upsert_face_embeddings(layout.embedding_db, face_observation_id=winner_face_id, main=winner_main, flip=winner_flip)
    upsert_face_embeddings(layout.embedding_db, face_observation_id=loser_face_id, main=loser_main, flip=loser_flip)
    upsert_face_embeddings(layout.embedding_db, face_observation_id=new_face_id, main=winner_main, flip=winner_flip)

    conn = sqlite3.connect(layout.library_db)
    try:
        winner_person_id = _insert_person(conn, display_name="Winner")
        loser_person_id = _insert_person(conn, display_name="Loser")
        assignment_run_id = _insert_assignment_run(conn, scan_session_id=session_id)
        _insert_assignment(conn, person_id=winner_person_id, face_observation_id=winner_face_id, assignment_run_id=assignment_run_id)
        _insert_assignment(conn, person_id=loser_person_id, face_observation_id=loser_face_id, assignment_run_id=assignment_run_id)
        _insert_cluster(
            conn,
            person_id=winner_person_id,
            assignment_run_id=assignment_run_id,
            member_face_ids=[winner_face_id],
            rep_face_ids=[winner_face_id],
        )
        conn.commit()
    finally:
        conn.close()

    service = PeopleService(PeopleRepository(layout.library_db))
    merge_result = service.merge_people([winner_person_id, loser_person_id])

    incremental = IncrementalAssignmentService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        cluster_repo=ClusterRepository(layout.library_db),
    )
    incremental_result = incremental.run(
        assignment_run_id=assignment_run_id,
        face_observation_ids=[new_face_id],
    )
    assert incremental_result.attached_count == 1

    try:
        service.undo_last_merge()
        raise AssertionError("预期 undo_last_merge 拒绝 winner cluster 漂移分支")
    except PeopleUndoMergeConflictError:
        pass

    conn = sqlite3.connect(layout.library_db)
    try:
        merge_status = conn.execute(
            "SELECT status FROM merge_operation WHERE id=?",
            (merge_result.merge_operation_id,),
        ).fetchone()
        active_assignment = conn.execute(
            """
            SELECT person_id
            FROM person_face_assignment
            WHERE face_observation_id=? AND active=1
            """,
            (new_face_id,),
        ).fetchone()
    finally:
        conn.close()

    assert str(merge_status[0]) == "applied"
    assert int(active_assignment[0]) == winner_person_id


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


def _insert_real_assignment_run(conn: sqlite3.Connection, *, scan_session_id: int) -> int:
    cursor = conn.execute(
        """
        INSERT INTO assignment_run(
          scan_session_id, algorithm_version, param_snapshot_json, run_kind, started_at, finished_at, status, updated_at
        ) VALUES (?, 'frozen_v5', ?, 'scan_full', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'completed', CURRENT_TIMESTAMP)
        """,
        (scan_session_id, _snapshot_json()),
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


def _insert_exclusion(
    conn: sqlite3.Connection,
    *,
    person_id: int,
    face_observation_id: int,
    active: bool,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO person_face_exclusion(
          person_id, face_observation_id, reason, active, created_at, updated_at
        ) VALUES (?, ?, 'manual_exclude', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (person_id, face_observation_id, 1 if active else 0),
    )
    return int(cursor.lastrowid)


def _insert_scan_session(conn: sqlite3.Connection, *, run_kind: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO scan_session(
          run_kind, status, triggered_by, started_at, finished_at, created_at, updated_at
        ) VALUES (?, 'pending', 'manual_cli', CURRENT_TIMESTAMP, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (run_kind,),
    )
    return int(cursor.lastrowid)


def _snapshot_json() -> str:
    import json

    return json.dumps(build_frozen_v5_param_snapshot(), ensure_ascii=False, sort_keys=True)
