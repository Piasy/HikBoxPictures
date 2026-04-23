import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import sqlite3
from pathlib import Path

import numpy as np

from hikbox_pictures.product.scan.assignment_stage import AssignmentStageService
from hikbox_pictures.product.scan.cluster_repository import ClusterRepository
from hikbox_pictures.product.scan.incremental_assignment_service import IncrementalAssignmentService

from tests.product.task6_test_support import (
    blend_embeddings,
    create_task6_workspace,
    embedding_from_seed,
    fake_embedding_calculator_from_map,
    seed_face_observations,
    upsert_face_embeddings,
)


def test_incremental_face_reuses_existing_cluster_and_person(tmp_path: Path, monkeypatch) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    observation_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (220, 180, 160), "quality_score": 0.92},
            {"asset_index": 0, "color": (218, 178, 158), "quality_score": 0.88},
            {"asset_index": 1, "color": (150, 210, 220), "quality_score": 0.94},
        ],
    )
    base_main = embedding_from_seed(101)
    calculator = fake_embedding_calculator_from_map(
        {
            "f1.png": (base_main, embedding_from_seed(201), 1.1),
            "f2.png": (base_main, embedding_from_seed(202), 1.1),
            "f3.png": (embedding_from_seed(301), embedding_from_seed(302), 1.1),
        }
    )

    def fake_run(*, faces, params):
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
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "assignment_source": "hdbscan",
                    "probability": 0.93,
                },
                {
                    "face_observation_id": observation_ids[2],
                    "cluster_label": 20,
                    "person_temp_key": "p1",
                    "assignment_source": "hdbscan",
                    "probability": 0.94,
                },
            ],
            "persons": [
                {"person_temp_key": "p0", "face_observation_ids": observation_ids[:2]},
                {"person_temp_key": "p1", "face_observation_ids": [observation_ids[2]]},
            ],
            "clusters": [
                {
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "member_face_observation_ids": observation_ids[:2],
                    "representative_face_observation_ids": [observation_ids[0]],
                },
                {
                    "cluster_label": 20,
                    "person_temp_key": "p1",
                    "member_face_observation_ids": [observation_ids[2]],
                    "representative_face_observation_ids": [observation_ids[2]],
                },
            ],
            "stats": {"person_count": 2, "assignment_count": 3},
        }

    monkeypatch.setattr("hikbox_pictures.product.scan.assignment_stage.run_frozen_v5_assignment", fake_run)
    stage = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    )
    baseline = stage.run_frozen_v5_assignment(
        scan_session_id=session_id,
        run_kind="scan_full",
        embedding_calculator=calculator,
    )

    new_face_id = seed_face_observations(
        layout.library_db,
        runtime_root,
        [{"asset_index": 1, "color": (222, 182, 162), "quality_score": 0.91}],
    )[0]
    upsert_face_embeddings(
        layout.embedding_db,
        face_observation_id=new_face_id,
        main=base_main,
        flip=embedding_from_seed(203),
    )

    service = IncrementalAssignmentService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        cluster_repo=ClusterRepository(layout.library_db),
    )
    result = service.run(
        assignment_run_id=baseline.assignment_run_id,
        face_observation_ids=[new_face_id],
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        person_id = int(
            conn.execute(
                "SELECT person_id FROM person_face_assignment WHERE face_observation_id=? AND active=1",
                (new_face_id,),
            ).fetchone()[0]
        )
        cluster_person_id = int(
            conn.execute(
                "SELECT person_id FROM face_cluster WHERE status='active' ORDER BY id ASC LIMIT 1"
            ).fetchone()[0]
        )
    finally:
        conn.close()

    cluster = ClusterRepository(layout.library_db).list_active_clusters()[0]
    assert result.attached_count == 1
    assert result.local_rebuild_count == 0
    assert person_id == cluster_person_id
    assert cluster.member_count == 3


def test_missing_asset_faces_do_not_participate_in_incremental_attach(tmp_path: Path, monkeypatch) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    observation_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [{"asset_index": 0, "color": (220, 180, 160), "quality_score": 0.92}],
    )
    shared_main = embedding_from_seed(2101)
    calculator = fake_embedding_calculator_from_map(
        {
            "f1.png": (shared_main, embedding_from_seed(2201), 1.1),
        }
    )

    def fake_run(*, faces, params):
        return {
            "faces": [
                {
                    "face_observation_id": observation_ids[0],
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "assignment_source": "hdbscan",
                    "probability": 0.95,
                },
            ],
            "persons": [{"person_temp_key": "p0", "face_observation_ids": [observation_ids[0]]}],
            "clusters": [
                {
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "member_face_observation_ids": [observation_ids[0]],
                    "representative_face_observation_ids": [observation_ids[0]],
                },
            ],
            "stats": {"person_count": 1, "assignment_count": 1},
        }

    monkeypatch.setattr("hikbox_pictures.product.scan.assignment_stage.run_frozen_v5_assignment", fake_run)
    stage = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    )
    baseline = stage.run_frozen_v5_assignment(
        scan_session_id=session_id,
        run_kind="scan_full",
        embedding_calculator=calculator,
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        conn.execute(
            """
            UPDATE photo_asset
            SET asset_status='missing', updated_at=CURRENT_TIMESTAMP
            WHERE id=(SELECT photo_asset_id FROM face_observation WHERE id=?)
            """,
            (observation_ids[0],),
        )
        conn.commit()
    finally:
        conn.close()

    new_face_id = seed_face_observations(
        layout.library_db,
        runtime_root,
        [{"asset_index": 1, "color": (221, 181, 161), "quality_score": 0.91}],
    )[0]
    upsert_face_embeddings(
        layout.embedding_db,
        face_observation_id=new_face_id,
        main=shared_main,
        flip=embedding_from_seed(2202),
    )

    service = IncrementalAssignmentService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        cluster_repo=ClusterRepository(layout.library_db),
    )
    result = service.run(
        assignment_run_id=baseline.assignment_run_id,
        face_observation_ids=[new_face_id],
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        active_assignment_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM person_face_assignment WHERE face_observation_id=? AND active=1",
                (new_face_id,),
            ).fetchone()[0]
        )
    finally:
        conn.close()

    assert result.attached_count == 0
    assert result.local_rebuild_count == 0
    assert active_assignment_count == 0


def test_incremental_attach_preserves_high_quality_rep_and_marks_cluster_local(tmp_path: Path, monkeypatch) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    observation_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (220, 180, 160), "quality_score": 0.95},
            {"asset_index": 0, "color": (218, 178, 158), "quality_score": 0.70},
            {"asset_index": 1, "color": (150, 210, 220), "quality_score": 0.94},
        ],
    )
    shared_main = embedding_from_seed(801)
    calculator = fake_embedding_calculator_from_map(
        {
            "f1.png": (shared_main, embedding_from_seed(901), 1.1),
            "f2.png": (shared_main, embedding_from_seed(902), 1.1),
            "f3.png": (embedding_from_seed(903), embedding_from_seed(904), 1.1),
        }
    )

    def fake_run(*, faces, params):
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
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "assignment_source": "hdbscan",
                    "probability": 0.92,
                },
                {
                    "face_observation_id": observation_ids[2],
                    "cluster_label": 20,
                    "person_temp_key": "p1",
                    "assignment_source": "hdbscan",
                    "probability": 0.96,
                },
            ],
            "persons": [
                {"person_temp_key": "p0", "face_observation_ids": observation_ids[:2]},
                {"person_temp_key": "p1", "face_observation_ids": [observation_ids[2]]},
            ],
            "clusters": [
                {
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "member_face_observation_ids": observation_ids[:2],
                    "representative_face_observation_ids": [observation_ids[0], observation_ids[1]],
                },
                {
                    "cluster_label": 20,
                    "person_temp_key": "p1",
                    "member_face_observation_ids": [observation_ids[2]],
                    "representative_face_observation_ids": [observation_ids[2]],
                },
            ],
            "stats": {"person_count": 2, "assignment_count": 3},
        }

    monkeypatch.setattr("hikbox_pictures.product.scan.assignment_stage.run_frozen_v5_assignment", fake_run)
    stage = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    )
    baseline = stage.run_frozen_v5_assignment(
        scan_session_id=session_id,
        run_kind="scan_full",
        embedding_calculator=calculator,
    )

    new_face_id = seed_face_observations(
        layout.library_db,
        runtime_root,
        [{"asset_index": 1, "color": (222, 182, 162), "quality_score": 0.10}],
    )[0]
    upsert_face_embeddings(
        layout.embedding_db,
        face_observation_id=new_face_id,
        main=shared_main,
        flip=embedding_from_seed(905),
    )

    service = IncrementalAssignmentService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        cluster_repo=ClusterRepository(layout.library_db),
    )
    result = service.run(
        assignment_run_id=baseline.assignment_run_id,
        face_observation_ids=[new_face_id],
    )

    repo = ClusterRepository(layout.library_db)
    clusters_by_person = {item.person_id: item for item in repo.list_active_clusters()}
    target_cluster = min(clusters_by_person.values(), key=lambda item: item.id)
    rep_face_ids = [item.face_observation_id for item in repo.list_cluster_rep_faces(target_cluster.id)]

    assert result.attached_count == 1
    assert rep_face_ids[0] == observation_ids[0]
    assert new_face_id in rep_face_ids
    assert target_cluster.rebuild_scope == "local"
    assert any(item.rebuild_scope == "full" for item in clusters_by_person.values() if item.id != target_cluster.id)


def test_ambiguous_face_uses_local_rebuild_instead_of_forcing_attach(tmp_path: Path, monkeypatch) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    observation_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (220, 180, 160), "quality_score": 0.92},
            {"asset_index": 1, "color": (150, 210, 220), "quality_score": 0.93},
        ],
    )
    left = embedding_from_seed(401)
    right = embedding_from_seed(402)
    calculator = fake_embedding_calculator_from_map(
        {
            "f1.png": (left, embedding_from_seed(501), 1.1),
            "f2.png": (right, embedding_from_seed(502), 1.1),
        }
    )

    def fake_run(*, faces, params):
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
                    "probability": 0.95,
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

    monkeypatch.setattr("hikbox_pictures.product.scan.assignment_stage.run_frozen_v5_assignment", fake_run)
    stage = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    )
    baseline = stage.run_frozen_v5_assignment(
        scan_session_id=session_id,
        run_kind="scan_full",
        embedding_calculator=calculator,
    )

    new_face_id = seed_face_observations(
        layout.library_db,
        runtime_root,
        [{"asset_index": 1, "color": (200, 195, 190), "quality_score": 0.89}],
    )[0]
    upsert_face_embeddings(
        layout.embedding_db,
        face_observation_id=new_face_id,
        main=blend_embeddings(left, right, weight=0.5),
        flip=blend_embeddings(left, right, weight=0.48),
    )

    service = IncrementalAssignmentService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        cluster_repo=ClusterRepository(layout.library_db),
    )
    result = service.run(
        assignment_run_id=baseline.assignment_run_id,
        face_observation_ids=[new_face_id],
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        attached = conn.execute(
            "SELECT COUNT(*) FROM person_face_assignment WHERE face_observation_id=? AND active=1",
            (new_face_id,),
        ).fetchone()
    finally:
        conn.close()

    assert result.attached_count == 0
    assert result.local_rebuild_count == 1
    assert attached is not None and int(attached[0]) == 0


def test_representative_recall_filters_candidates_before_member_rerank(tmp_path: Path, monkeypatch) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    observation_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (220, 180, 160), "quality_score": 0.95},
            {"asset_index": 0, "color": (219, 179, 159), "quality_score": 0.70},
            {"asset_index": 1, "color": (150, 210, 220), "quality_score": 0.94},
            {"asset_index": 1, "color": (149, 209, 219), "quality_score": 0.72},
        ],
    )
    calculator = fake_embedding_calculator_from_map(
        {
            "f1.png": (embedding_from_seed(1001), embedding_from_seed(1101), 1.1),
            "f2.png": (embedding_from_seed(1002), embedding_from_seed(1102), 1.1),
            "f3.png": (embedding_from_seed(1003), embedding_from_seed(1103), 1.1),
            "f4.png": (embedding_from_seed(1004), embedding_from_seed(1104), 1.1),
        }
    )

    def fake_run(*, faces, params):
        return {
            "faces": [
                {
                    "face_observation_id": observation_ids[0],
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "assignment_source": "hdbscan",
                    "probability": 0.96,
                },
                {
                    "face_observation_id": observation_ids[1],
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "assignment_source": "hdbscan",
                    "probability": 0.91,
                },
                {
                    "face_observation_id": observation_ids[2],
                    "cluster_label": 20,
                    "person_temp_key": "p1",
                    "assignment_source": "hdbscan",
                    "probability": 0.95,
                },
                {
                    "face_observation_id": observation_ids[3],
                    "cluster_label": 20,
                    "person_temp_key": "p1",
                    "assignment_source": "hdbscan",
                    "probability": 0.90,
                },
            ],
            "persons": [
                {"person_temp_key": "p0", "face_observation_ids": observation_ids[:2]},
                {"person_temp_key": "p1", "face_observation_ids": observation_ids[2:]},
            ],
            "clusters": [
                {
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "member_face_observation_ids": observation_ids[:2],
                    "representative_face_observation_ids": [observation_ids[0]],
                },
                {
                    "cluster_label": 20,
                    "person_temp_key": "p1",
                    "member_face_observation_ids": observation_ids[2:],
                    "representative_face_observation_ids": [observation_ids[2]],
                },
            ],
            "stats": {"person_count": 2, "assignment_count": 4},
        }

    monkeypatch.setattr("hikbox_pictures.product.scan.assignment_stage.run_frozen_v5_assignment", fake_run)
    stage = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    )
    stage.run_frozen_v5_assignment(
        scan_session_id=session_id,
        run_kind="scan_full",
        embedding_calculator=calculator,
    )

    new_face_id = seed_face_observations(
        layout.library_db,
        runtime_root,
        [{"asset_index": 1, "color": (180, 180, 180), "quality_score": 0.83}],
    )[0]
    service = IncrementalAssignmentService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        cluster_repo=ClusterRepository(layout.library_db),
        attach_threshold=0.85,
        attach_margin=0.03,
        candidate_threshold=0.75,
    )

    monkeypatch.setattr(
        service,
        "_load_face_embedding",
        lambda face_observation_id: {"main": np.ones(4, dtype=np.float32), "flip": None},
    )

    repo = ClusterRepository(layout.library_db)
    clusters = repo.list_active_clusters()
    low_rep_cluster = min(clusters, key=lambda item: item.id)
    high_rep_cluster = max(clusters, key=lambda item: item.id)
    score_by_face_ids = {
        tuple(item.face_observation_id for item in repo.list_cluster_rep_faces(low_rep_cluster.id)): 0.70,
        tuple(item.face_observation_id for item in repo.list_cluster_members(low_rep_cluster.id)): 0.99,
        tuple(item.face_observation_id for item in repo.list_cluster_rep_faces(high_rep_cluster.id)): 0.91,
        tuple(item.face_observation_id for item in repo.list_cluster_members(high_rep_cluster.id)): 0.88,
    }

    monkeypatch.setattr(
        service,
        "_best_similarity",
        lambda target_embedding, face_ids: score_by_face_ids.get(tuple(face_ids), -1.0),
    )

    decision = service._decide(face_observation_id=new_face_id)

    assert decision is not None
    assert decision["mode"] == "attach"
    assert int(decision["cluster_id"]) == high_rep_cluster.id


def test_local_rebuild_keeps_face_unassigned_when_overlap_is_still_ambiguous(tmp_path: Path, monkeypatch) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    observation_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (220, 180, 160), "quality_score": 0.92},
            {"asset_index": 1, "color": (150, 210, 220), "quality_score": 0.93},
        ],
    )
    calculator = fake_embedding_calculator_from_map(
        {
            "f1.png": (embedding_from_seed(1201), embedding_from_seed(1301), 1.1),
            "f2.png": (embedding_from_seed(1202), embedding_from_seed(1302), 1.1),
        }
    )

    def fake_run(*, faces, params):
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
                    "probability": 0.95,
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

    monkeypatch.setattr("hikbox_pictures.product.scan.assignment_stage.run_frozen_v5_assignment", fake_run)
    stage = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    )
    baseline = stage.run_frozen_v5_assignment(
        scan_session_id=session_id,
        run_kind="scan_full",
        embedding_calculator=calculator,
    )

    new_face_id = seed_face_observations(
        layout.library_db,
        runtime_root,
        [{"asset_index": 1, "color": (190, 190, 190), "quality_score": 0.89}],
    )[0]
    upsert_face_embeddings(
        layout.embedding_db,
        face_observation_id=new_face_id,
        main=embedding_from_seed(1203),
        flip=embedding_from_seed(1303),
    )

    repo = ClusterRepository(layout.library_db)
    candidate_cluster_ids = [item.id for item in repo.list_active_clusters()]
    service = IncrementalAssignmentService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        cluster_repo=repo,
    )
    monkeypatch.setattr(
        service,
        "_decide",
        lambda face_observation_id, conn=None: {
            "mode": "local_rebuild",
            "candidate_cluster_ids": candidate_cluster_ids,
        },
    )
    monkeypatch.setattr(
        "hikbox_pictures.product.scan.incremental_assignment_service.run_frozen_v5_assignment",
        lambda *, faces, params: {
            "faces": [
                {
                    "face_observation_id": new_face_id,
                    "cluster_label": 99,
                    "person_temp_key": "px",
                    "assignment_source": "hdbscan",
                },
                {
                    "face_observation_id": observation_ids[0],
                    "cluster_label": 99,
                    "person_temp_key": "px",
                    "assignment_source": "hdbscan",
                },
                {
                    "face_observation_id": observation_ids[1],
                    "cluster_label": 99,
                    "person_temp_key": "px",
                    "assignment_source": "hdbscan",
                },
            ]
        },
    )

    result = service.run(
        assignment_run_id=baseline.assignment_run_id,
        face_observation_ids=[new_face_id],
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        attached_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM person_face_assignment WHERE face_observation_id=? AND active=1",
                (new_face_id,),
            ).fetchone()[0]
        )
    finally:
        conn.close()

    assert result.local_rebuild_count == 1
    assert result.attached_count == 0
    assert attached_count == 0


def test_missing_asset_faces_are_excluded_from_local_rebuild_subset(tmp_path: Path, monkeypatch) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    observation_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [{"asset_index": 0, "color": (220, 180, 160), "quality_score": 0.92}],
    )
    calculator = fake_embedding_calculator_from_map(
        {
            "f1.png": (embedding_from_seed(2301), embedding_from_seed(2401), 1.1),
        }
    )

    def fake_run(*, faces, params):
        return {
            "faces": [
                {
                    "face_observation_id": observation_ids[0],
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "assignment_source": "hdbscan",
                    "probability": 0.95,
                },
            ],
            "persons": [{"person_temp_key": "p0", "face_observation_ids": [observation_ids[0]]}],
            "clusters": [
                {
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "member_face_observation_ids": [observation_ids[0]],
                    "representative_face_observation_ids": [observation_ids[0]],
                },
            ],
            "stats": {"person_count": 1, "assignment_count": 1},
        }

    monkeypatch.setattr("hikbox_pictures.product.scan.assignment_stage.run_frozen_v5_assignment", fake_run)
    stage = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    )
    baseline = stage.run_frozen_v5_assignment(
        scan_session_id=session_id,
        run_kind="scan_full",
        embedding_calculator=calculator,
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        conn.execute(
            """
            UPDATE photo_asset
            SET asset_status='missing', updated_at=CURRENT_TIMESTAMP
            WHERE id=(SELECT photo_asset_id FROM face_observation WHERE id=?)
            """,
            (observation_ids[0],),
        )
        conn.commit()
    finally:
        conn.close()

    new_face_id = seed_face_observations(
        layout.library_db,
        runtime_root,
        [{"asset_index": 1, "color": (200, 195, 190), "quality_score": 0.89}],
    )[0]
    upsert_face_embeddings(
        layout.embedding_db,
        face_observation_id=new_face_id,
        main=embedding_from_seed(2302),
        flip=embedding_from_seed(2402),
    )

    service = IncrementalAssignmentService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        cluster_repo=ClusterRepository(layout.library_db),
    )
    monkeypatch.setattr(
        service,
        "_decide",
        lambda face_observation_id, conn=None: {
            "mode": "local_rebuild",
            "candidate_cluster_ids": [ClusterRepository(layout.library_db).list_active_clusters()[0].id],
        },
    )

    captured_local_faces: list[int] = []

    def fake_local_run(*, faces, params):
        captured_local_faces.extend(sorted(int(face["face_observation_id"]) for face in faces))
        return {"faces": []}

    monkeypatch.setattr(
        "hikbox_pictures.product.scan.incremental_assignment_service.run_frozen_v5_assignment",
        fake_local_run,
    )

    result = service.run(
        assignment_run_id=baseline.assignment_run_id,
        face_observation_ids=[new_face_id],
    )

    assert result.attached_count == 0
    assert result.local_rebuild_count == 1
    assert captured_local_faces == []
