import sqlite3
import sys
import uuid
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hikbox_pictures.product.audit.service import AuditSamplingService
from hikbox_pictures.product.scan.assignment_stage import AssignmentStageService
from hikbox_pictures.product.scan.cluster_repository import ClusterRepository
from hikbox_pictures.product.scan.incremental_assignment_service import IncrementalAssignmentResult
from hikbox_pictures.product.scan.session_service import ScanSessionRepository
from tests.product.task6_test_support import (
    create_task6_workspace,
    fake_embedding_calculator_from_map,
    seed_face_observations,
    upsert_face_embeddings,
)


def test_assignment_run_samples_reassign_to_other_person_and_dedups_new_anonymous_person(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    face_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"color": (210, 180, 160), "quality_score": 0.61},
            {"color": (205, 175, 155), "quality_score": 0.64},
            {"color": (200, 170, 150), "quality_score": 0.72},
        ],
    )
    excluded_person_id = _seed_existing_person_and_exclusion(
        library_db=layout.library_db,
        scan_session_id=session_id,
        active_face_ids=[face_ids[0], face_ids[1]],
        excluded_face_id=face_ids[0],
    )
    service = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    )

    def fake_run(*, faces, params):
        assert [int(face["face_observation_id"]) for face in faces] == face_ids
        return {
            "faces": [
                {
                    "face_observation_id": face_ids[0],
                    "person_temp_key": "p-other",
                    "assignment_source": "hdbscan",
                    "probability": 0.93,
                },
                {
                    "face_observation_id": face_ids[1],
                    "person_temp_key": "p-anon",
                    "assignment_source": "hdbscan",
                    "probability": 0.91,
                },
                {
                    "face_observation_id": face_ids[2],
                    "person_temp_key": "p-anon",
                    "assignment_source": "hdbscan",
                    "probability": 0.90,
                },
            ],
            "persons": [
                {"person_temp_key": "p-other", "face_observation_ids": [face_ids[0]]},
                {"person_temp_key": "p-anon", "face_observation_ids": [face_ids[1], face_ids[2]]},
            ],
            "clusters": [
                {
                    "cluster_label": 10,
                    "person_temp_key": "p-other",
                    "member_face_observation_ids": [face_ids[0]],
                    "representative_face_observation_ids": [face_ids[0]],
                },
                {
                    "cluster_label": 20,
                    "person_temp_key": "p-anon",
                    "member_face_observation_ids": [face_ids[1]],
                    "representative_face_observation_ids": [face_ids[1]],
                },
                {
                    "cluster_label": 21,
                    "person_temp_key": "p-anon",
                    "member_face_observation_ids": [face_ids[2]],
                    "representative_face_observation_ids": [face_ids[2]],
                },
            ],
            "stats": {"person_count": 2, "assignment_count": 3},
        }

    monkeypatch.setattr("hikbox_pictures.product.scan.assignment_stage.run_frozen_v5_assignment", fake_run)

    run_result = service.run_frozen_v5_assignment(
        scan_session_id=session_id,
        run_kind="scan_full",
        embedding_calculator=_constant_embedding_calculator,
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        reassign_rows = conn.execute(
            """
            SELECT audit_type, face_observation_id, person_id, evidence_json
            FROM scan_audit_item
            WHERE assignment_run_id=? AND audit_type='reassign_after_exclusion'
            ORDER BY id ASC
            """,
            (run_result.assignment_run_id,),
        ).fetchall()
        anonymous_rows = conn.execute(
            """
            SELECT audit_type, face_observation_id, person_id, evidence_json
            FROM scan_audit_item
            WHERE assignment_run_id=? AND audit_type='new_anonymous_person'
            ORDER BY id ASC
            """,
            (run_result.assignment_run_id,),
        ).fetchall()
    finally:
        conn.close()

    assert len(reassign_rows) == 1
    assert (str(reassign_rows[0][0]), int(reassign_rows[0][1])) == ("reassign_after_exclusion", face_ids[0])
    assert int(reassign_rows[0][2]) != excluded_person_id
    assert '"excluded_person_id"' in str(reassign_rows[0][3])

    anon_pairs = {(int(row[2]), int(row[1])) for row in anonymous_rows}
    anon_two_face_rows = [row for row in anonymous_rows if '"active_face_count": 2' in str(row[3])]
    assert len(anon_two_face_rows) == 1
    assert len({person_id for person_id, _face_id in anon_pairs}) == len(anonymous_rows)


def test_assignment_run_persists_real_runtime_margin_and_samples_low_margin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    face_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (220, 180, 160), "quality_score": 0.92},
            {"asset_index": 0, "color": (218, 178, 158), "quality_score": 0.91},
            {"asset_index": 1, "color": (140, 180, 220), "quality_score": 0.93},
            {"asset_index": 1, "color": (138, 178, 218), "quality_score": 0.92},
            {"asset_index": 1, "color": (200, 200, 200), "quality_score": 0.94},
        ],
    )
    vector_a = _unit_vector(1.0, 0.6, 0.0)
    vector_b = _unit_vector(0.755, 0.0, 0.655)
    vector_noise = _unit_vector(0.7, -0.02, 0.05)
    calculator = fake_embedding_calculator_from_map(
        {
            "f1.png": (vector_a, None, 1.1),
            "f2.png": (vector_a, None, 1.1),
            "f3.png": (vector_b, None, 1.1),
            "f4.png": (vector_b, None, 1.1),
            "f5.png": (vector_noise, None, 1.1),
        }
    )
    monkeypatch.setattr(
        "hikbox_pictures.product.engine.frozen_v5._cluster_with_hdbscan",
        lambda embeddings, min_cluster_size, min_samples: ([0, 0, 1, 1, -1], [0.96, 0.95, 0.95, 0.94, 0.0]),
    )

    run_result = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    ).run_frozen_v5_assignment(
        scan_session_id=session_id,
        run_kind="scan_full",
        embedding_calculator=calculator,
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        assignment_row = conn.execute(
            """
            SELECT assignment_source, margin
            FROM person_face_assignment
            WHERE assignment_run_id=? AND face_observation_id=?
            """,
            (run_result.assignment_run_id, face_ids[4]),
        ).fetchone()
        audit_row = conn.execute(
            """
            SELECT audit_type
            FROM scan_audit_item
            WHERE assignment_run_id=? AND face_observation_id=?
            ORDER BY id ASC
            """,
            (run_result.assignment_run_id, face_ids[4]),
        ).fetchall()
    finally:
        conn.close()

    assert assignment_row is not None
    assert str(assignment_row[0]) == "person_consensus"
    assert assignment_row[1] is not None
    assert float(assignment_row[1]) <= 0.05
    assert {str(row[0]) for row in audit_row} >= {"low_margin_auto_assign"}


def test_incremental_assignment_persists_real_attach_margin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    observation_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (220, 180, 160), "quality_score": 0.92},
            {"asset_index": 0, "color": (140, 180, 220), "quality_score": 0.93},
        ],
    )
    vector_a = _unit_vector(1.0, 0.0, 0.0)
    vector_b = _unit_vector(0.75, 0.0, 0.6614378)
    baseline_calculator = fake_embedding_calculator_from_map(
        {
            "f1.png": (vector_a, None, 1.1),
            "f2.png": (vector_b, None, 1.1),
        }
    )

    def fake_full_run(*, faces, params):
        return {
            "faces": [
                {
                    "face_observation_id": observation_ids[0],
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "assignment_source": "hdbscan",
                    "probability": 0.95,
                },
                {
                    "face_observation_id": observation_ids[1],
                    "cluster_label": 20,
                    "person_temp_key": "p1",
                    "assignment_source": "hdbscan",
                    "probability": 0.94,
                },
            ],
            "persons": [
                {"person_temp_key": "p0", "face_observation_ids": [observation_ids[0]]},
                {"person_temp_key": "p1", "face_observation_ids": [observation_ids[1]]},
            ],
            "clusters": [
                {
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "member_face_observation_ids": [observation_ids[0]],
                    "representative_face_observation_ids": [observation_ids[0]],
                },
                {
                    "cluster_label": 20,
                    "person_temp_key": "p1",
                    "member_face_observation_ids": [observation_ids[1]],
                    "representative_face_observation_ids": [observation_ids[1]],
                },
            ],
            "stats": {"person_count": 2, "assignment_count": 2},
        }

    monkeypatch.setattr("hikbox_pictures.product.scan.assignment_stage.run_frozen_v5_assignment", fake_full_run)
    baseline = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    ).run_frozen_v5_assignment(
        scan_session_id=session_id,
        run_kind="scan_full",
        embedding_calculator=baseline_calculator,
    )
    assert baseline.assignment_run_id > 0
    ScanSessionRepository(layout.library_db).update_status(session_id, status="completed")

    incremental_session = ScanSessionRepository(layout.library_db).create_session(
        run_kind="scan_incremental",
        status="running",
        triggered_by="manual_cli",
    )
    _seed_scan_session_source(
        library_db=layout.library_db,
        scan_session_id=incremental_session.id,
    )
    new_face_id = seed_face_observations(
        layout.library_db,
        runtime_root,
        [{"asset_index": 1, "color": (200, 200, 200), "quality_score": 0.94}],
    )[0]
    upsert_face_embeddings(
        layout.embedding_db,
        face_observation_id=new_face_id,
        main=_unit_vector(0.84, 0.0, 0.5425864),
        flip=None,
    )

    monkeypatch.undo()
    run_result = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    ).run_frozen_v5_assignment(
        scan_session_id=incremental_session.id,
        run_kind="scan_incremental",
        embedding_calculator=baseline_calculator,
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        margin_row = conn.execute(
            """
            SELECT assignment_source, margin
            FROM person_face_assignment
            WHERE assignment_run_id=? AND face_observation_id=? AND active=1
            """,
            (run_result.assignment_run_id, new_face_id),
        ).fetchone()
    finally:
        conn.close()

    assert margin_row is not None
    assert str(margin_row[0]) == "person_consensus"
    assert margin_row[1] is not None
    assert float(margin_row[1]) >= 0.03


def test_low_margin_audit_ignores_low_confidence_rows_without_real_margin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    face_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"color": (210, 180, 160), "quality_score": 0.61},
            {"color": (205, 175, 155), "quality_score": 0.64},
        ],
    )
    service = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    )

    def fake_run(*, faces, params):
        assert [int(face["face_observation_id"]) for face in faces] == face_ids
        return {
            "faces": [
                {
                    "face_observation_id": face_ids[0],
                    "person_temp_key": "p0",
                    "assignment_source": "hdbscan",
                    "probability": 0.55,
                },
                {
                    "face_observation_id": face_ids[1],
                    "person_temp_key": "p0",
                    "assignment_source": "hdbscan",
                    "probability": 0.93,
                },
            ],
            "persons": [
                {"person_temp_key": "p0", "face_observation_ids": face_ids},
            ],
            "clusters": [
                {
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "member_face_observation_ids": face_ids,
                    "representative_face_observation_ids": [face_ids[1]],
                },
            ],
            "stats": {"person_count": 1, "assignment_count": 2},
        }

    monkeypatch.setattr("hikbox_pictures.product.scan.assignment_stage.run_frozen_v5_assignment", fake_run)

    run_result = service.run_frozen_v5_assignment(
        scan_session_id=session_id,
        run_kind="scan_full",
        embedding_calculator=_constant_embedding_calculator,
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        assignment_row = conn.execute(
            """
            SELECT confidence, margin
            FROM person_face_assignment
            WHERE assignment_run_id=? AND face_observation_id=?
            """,
            (run_result.assignment_run_id, face_ids[0]),
        ).fetchone()
        audit_rows = conn.execute(
            """
            SELECT audit_type
            FROM scan_audit_item
            WHERE assignment_run_id=? AND face_observation_id=?
            ORDER BY id ASC
            """,
            (run_result.assignment_run_id, face_ids[0]),
        ).fetchall()
    finally:
        conn.close()

    assert assignment_row is not None
    assert float(assignment_row[0]) == pytest.approx(0.55)
    assert assignment_row[1] is None
    assert {str(row[0]) for row in audit_rows}.isdisjoint({"low_margin_auto_assign"})


def test_existing_anonymous_person_extended_in_incremental_run_is_not_sampled_as_new(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    baseline_face_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [{"asset_index": 0, "color": (220, 180, 160), "quality_score": 0.92}],
    )
    baseline_calculator = fake_embedding_calculator_from_map(
        {
            "f1.png": (_unit_vector(1.0, 0.0, 0.0), None, 1.1),
        }
    )

    def fake_full_run(*, faces, params):
        return {
            "faces": [
                {
                    "face_observation_id": baseline_face_ids[0],
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "assignment_source": "hdbscan",
                    "probability": 0.95,
                },
            ],
            "persons": [{"person_temp_key": "p0", "face_observation_ids": [baseline_face_ids[0]]}],
            "clusters": [
                {
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "member_face_observation_ids": [baseline_face_ids[0]],
                    "representative_face_observation_ids": [baseline_face_ids[0]],
                },
            ],
            "stats": {"person_count": 1, "assignment_count": 1},
        }

    monkeypatch.setattr("hikbox_pictures.product.scan.assignment_stage.run_frozen_v5_assignment", fake_full_run)
    baseline_result = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    ).run_frozen_v5_assignment(
        scan_session_id=session_id,
        run_kind="scan_full",
        embedding_calculator=baseline_calculator,
    )
    ScanSessionRepository(layout.library_db).update_status(session_id, status="completed")

    conn = sqlite3.connect(layout.library_db)
    try:
        existing_person_id = int(
            conn.execute(
                """
                SELECT person_id
                FROM person_face_assignment
                WHERE assignment_run_id=? AND face_observation_id=? AND active=1
                """,
                (baseline_result.assignment_run_id, baseline_face_ids[0]),
            ).fetchone()[0]
        )
    finally:
        conn.close()

    incremental_session = ScanSessionRepository(layout.library_db).create_session(
        run_kind="scan_incremental",
        status="running",
        triggered_by="manual_cli",
    )
    _seed_scan_session_source(
        library_db=layout.library_db,
        scan_session_id=incremental_session.id,
    )
    new_face_id = seed_face_observations(
        layout.library_db,
        runtime_root,
        [{"asset_index": 1, "color": (200, 200, 200), "quality_score": 0.94}],
    )[0]
    upsert_face_embeddings(
        layout.embedding_db,
        face_observation_id=new_face_id,
        main=_unit_vector(0.98, 0.0, 0.02),
        flip=None,
    )

    def fake_incremental_run(self, *, assignment_run_id, face_observation_ids, conn=None, abort_checker=None):
        assert conn is not None
        assert face_observation_ids == [new_face_id]
        conn.execute(
            """
            INSERT INTO person_face_assignment(
              person_id, face_observation_id, assignment_run_id, assignment_source,
              active, confidence, margin, created_at, updated_at
            ) VALUES (?, ?, ?, 'merge', 1, NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (existing_person_id, new_face_id, assignment_run_id),
        )
        conn.execute(
            "UPDATE face_observation SET pending_reassign=0, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (new_face_id,),
        )
        self._cluster_repo.create_cluster_for_person(
            person_id=existing_person_id,
            assignment_run_id=assignment_run_id,
            member_face_ids=[new_face_id],
            representative_face_ids=[new_face_id],
            face_quality_by_id={new_face_id: 0.94},
            conn=conn,
            rebuild_scope="local",
        )
        return IncrementalAssignmentResult(
            attached_count=1,
            local_rebuild_count=1,
            person_count=1,
        )

    monkeypatch.undo()
    monkeypatch.setattr(
        "hikbox_pictures.product.scan.incremental_assignment_service.IncrementalAssignmentService.run",
        fake_incremental_run,
    )

    incremental_result = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    ).run_frozen_v5_assignment(
        scan_session_id=incremental_session.id,
        run_kind="scan_incremental",
        embedding_calculator=baseline_calculator,
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        audit_rows = conn.execute(
            """
            SELECT audit_type, person_id, face_observation_id
            FROM scan_audit_item
            WHERE assignment_run_id=? AND audit_type='new_anonymous_person'
            ORDER BY id ASC
            """,
            (incremental_result.assignment_run_id,),
        ).fetchall()
    finally:
        conn.close()

    assert audit_rows == []


def test_sample_assignment_run_keeps_persisted_result_stable_after_person_state_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    face_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [{"asset_index": 0, "color": (220, 180, 160), "quality_score": 0.92}],
    )

    def fake_run(*, faces, params):
        assert [int(face["face_observation_id"]) for face in faces] == face_ids
        return {
            "faces": [
                {
                    "face_observation_id": face_ids[0],
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "assignment_source": "hdbscan",
                    "probability": 0.95,
                },
            ],
            "persons": [{"person_temp_key": "p0", "face_observation_ids": face_ids}],
            "clusters": [
                {
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "member_face_observation_ids": face_ids,
                    "representative_face_observation_ids": face_ids,
                },
            ],
            "stats": {"person_count": 1, "assignment_count": 1},
        }

    monkeypatch.setattr("hikbox_pictures.product.scan.assignment_stage.run_frozen_v5_assignment", fake_run)
    run_result = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    ).run_frozen_v5_assignment(
        scan_session_id=session_id,
        run_kind="scan_full",
        embedding_calculator=_constant_embedding_calculator,
    )

    service = AuditSamplingService(layout.library_db)
    first_items = service.sample_assignment_run(run_result.assignment_run_id)
    assert [item.audit_type for item in first_items] == ["new_anonymous_person"]

    conn = sqlite3.connect(layout.library_db)
    try:
        conn.execute(
            """
            UPDATE person
            SET is_named=1,
                display_name='已命名',
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (first_items[0].person_id,),
        )
        conn.commit()
    finally:
        conn.close()

    second_items = service.sample_assignment_run(run_result.assignment_run_id)
    assert [(item.id, item.audit_type, item.face_observation_id) for item in second_items] == [
        (first_items[0].id, "new_anonymous_person", face_ids[0]),
    ]


def test_sample_assignment_run_returns_all_persisted_items_without_truncation(tmp_path: Path) -> None:
    layout, session_id, _runtime_root = create_task6_workspace(tmp_path)
    conn = sqlite3.connect(layout.library_db)
    try:
        run_id = int(
            conn.execute(
                """
                INSERT INTO assignment_run(
                  scan_session_id, algorithm_version, param_snapshot_json, run_kind,
                  started_at, finished_at, status
                ) VALUES (?, 'frozen_v5', '{}', 'scan_full', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'completed')
                """,
                (session_id,),
            ).lastrowid
        )
        for idx in range(1, 106):
            conn.execute(
                """
                INSERT INTO scan_audit_item(
                  scan_session_id, assignment_run_id, audit_type, face_observation_id, person_id, evidence_json
                ) VALUES (?, ?, 'low_margin_auto_assign', ?, NULL, ?)
                """,
                (session_id, run_id, idx, f'{{"seq": {idx}}}'),
            )
        conn.commit()
    finally:
        conn.close()

    items = AuditSamplingService(layout.library_db).sample_assignment_run(run_id)
    assert len(items) == 105
    assert [item.face_observation_id for item in items[:3]] == [1, 2, 3]
    assert [item.face_observation_id for item in items[-3:]] == [103, 104, 105]


def test_sample_assignment_run_keeps_empty_result_stable_after_later_state_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    face_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [{"asset_index": 0, "color": (220, 180, 160), "quality_score": 0.92}],
    )
    named_person_id, other_person_id = _seed_existing_named_people_for_stable_empty_audit(
        library_db=layout.library_db,
        scan_session_id=session_id,
        face_observation_id=face_ids[0],
    )

    def fake_run(*, faces, params):
        assert [int(face["face_observation_id"]) for face in faces] == face_ids
        return {
            "faces": [
                {
                    "face_observation_id": face_ids[0],
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "assignment_source": "hdbscan",
                    "probability": 0.95,
                },
            ],
            "persons": [{"person_temp_key": "p0", "face_observation_ids": face_ids}],
            "clusters": [
                {
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "member_face_observation_ids": face_ids,
                    "representative_face_observation_ids": face_ids,
                },
            ],
            "stats": {"person_count": 1, "assignment_count": 1},
        }

    monkeypatch.setattr("hikbox_pictures.product.scan.assignment_stage.run_frozen_v5_assignment", fake_run)
    run_result = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    ).run_frozen_v5_assignment(
        scan_session_id=session_id,
        run_kind="scan_full",
        embedding_calculator=_constant_embedding_calculator,
    )

    service = AuditSamplingService(layout.library_db)
    first_items = service.sample_assignment_run(run_result.assignment_run_id)
    assert first_items == []

    conn = sqlite3.connect(layout.library_db)
    try:
        conn.execute(
            """
            INSERT INTO person_face_exclusion(
              person_id, face_observation_id, reason, active, created_at, updated_at
            ) VALUES (?, ?, 'manual_exclude', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (other_person_id, face_ids[0]),
        )
        conn.execute(
            """
            UPDATE person
            SET display_name='被改名',
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (named_person_id,),
        )
        conn.commit()
    finally:
        conn.close()

    second_items = service.sample_assignment_run(run_result.assignment_run_id)
    assert second_items == []


def _seed_existing_person_and_exclusion(
    *,
    library_db: Path,
    scan_session_id: int,
    active_face_ids: list[int],
    excluded_face_id: int,
) -> int:
    conn = sqlite3.connect(library_db)
    try:
        previous_run_id = int(
            conn.execute(
                """
                INSERT INTO assignment_run(
                  scan_session_id, algorithm_version, param_snapshot_json, run_kind,
                  started_at, finished_at, status
                ) VALUES (?, 'frozen_v5', '{}', 'scan_full', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'completed')
                """,
                (scan_session_id,),
            ).lastrowid
        )
        person_id = int(
            conn.execute(
                """
                INSERT INTO person(
                  person_uuid, display_name, is_named, status, merged_into_person_id, created_at, updated_at
                ) VALUES (?, NULL, 0, 'active', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (str(uuid.uuid4()),),
            ).lastrowid
        )
        for face_id in active_face_ids:
            conn.execute(
                """
                INSERT INTO person_face_assignment(
                  person_id, face_observation_id, assignment_run_id, assignment_source,
                  active, confidence, margin, created_at, updated_at
                ) VALUES (?, ?, ?, 'hdbscan', 1, 0.95, 0.12, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (person_id, face_id, previous_run_id),
            )
        conn.execute(
            """
            INSERT INTO person_face_exclusion(
              person_id, face_observation_id, reason, active, created_at, updated_at
            ) VALUES (?, ?, 'manual_exclude', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (person_id, excluded_face_id),
        )
        conn.commit()
        return person_id
    finally:
        conn.close()


def _seed_existing_named_people_for_stable_empty_audit(
    *,
    library_db: Path,
    scan_session_id: int,
    face_observation_id: int,
) -> tuple[int, int]:
    conn = sqlite3.connect(library_db)
    try:
        previous_run_id = int(
            conn.execute(
                """
                INSERT INTO assignment_run(
                  scan_session_id, algorithm_version, param_snapshot_json, run_kind,
                  started_at, finished_at, status
                ) VALUES (?, 'frozen_v5', '{}', 'scan_full', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'completed')
                """,
                (scan_session_id,),
            ).lastrowid
        )
        named_person_id = int(
            conn.execute(
                """
                INSERT INTO person(
                  person_uuid, display_name, is_named, status, merged_into_person_id, created_at, updated_at
                ) VALUES (?, '已命名人物', 1, 'active', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (str(uuid.uuid4()),),
            ).lastrowid
        )
        other_person_id = int(
            conn.execute(
                """
                INSERT INTO person(
                  person_uuid, display_name, is_named, status, merged_into_person_id, created_at, updated_at
                ) VALUES (?, '其他人物', 1, 'active', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (str(uuid.uuid4()),),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO person_face_assignment(
              person_id, face_observation_id, assignment_run_id, assignment_source,
              active, confidence, margin, created_at, updated_at
            ) VALUES (?, ?, ?, 'hdbscan', 1, 0.95, 0.12, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (named_person_id, face_observation_id, previous_run_id),
        )
        conn.commit()
        return named_person_id, other_person_id
    finally:
        conn.close()


def _seed_scan_session_source(*, library_db: Path, scan_session_id: int) -> None:
    conn = sqlite3.connect(library_db)
    try:
        source_id = int(conn.execute("SELECT id FROM library_source ORDER BY id ASC LIMIT 1").fetchone()[0])
        conn.execute(
            """
            INSERT INTO scan_session_source(
              scan_session_id, library_source_id, stage_status_json, processed_assets, failed_assets, updated_at
            ) VALUES (?, ?, '{"discover":"done","metadata":"done","detect":"done","embed":"pending","cluster":"pending","assignment":"pending"}', 3, 0, CURRENT_TIMESTAMP)
            """,
            (scan_session_id, source_id),
        )
        conn.commit()
    finally:
        conn.close()


def _unit_vector(x: float, y: float, z: float) -> list[float]:
    vector = np.zeros(512, dtype=np.float32)
    vector[0] = float(x)
    vector[1] = float(y)
    vector[2] = float(z)
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-9:
        raise ValueError("向量范数必须大于 0")
    return (vector / norm).astype(float).tolist()


def _constant_embedding_calculator(_aligned_path: Path) -> tuple[list[float], list[float], float]:
    base = [0.01] * 512
    return base, base, 1.0
