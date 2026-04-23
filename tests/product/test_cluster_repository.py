import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import sqlite3
from pathlib import Path

from hikbox_pictures.product.scan.assignment_stage import AssignmentStageService
from hikbox_pictures.product.scan.cluster_repository import ClusterRepository

from tests.product.task6_test_support import (
    create_task6_workspace,
    embedding_from_seed,
    fake_embedding_calculator_from_map,
    seed_face_observations,
)


def test_persist_cluster_snapshot_materializes_members_and_rep_faces(tmp_path: Path, monkeypatch) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    observation_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (220, 180, 160), "quality_score": 0.91},
            {"asset_index": 0, "color": (218, 178, 158), "quality_score": 0.88},
            {"asset_index": 1, "color": (150, 210, 220), "quality_score": 0.94},
            {"asset_index": 1, "color": (148, 208, 218), "quality_score": 0.90},
        ],
    )
    calculator = fake_embedding_calculator_from_map(
        {
            "f1.png": (embedding_from_seed(11), embedding_from_seed(111), 1.1),
            "f2.png": (embedding_from_seed(12), embedding_from_seed(112), 1.1),
            "f3.png": (embedding_from_seed(21), embedding_from_seed(121), 1.1),
            "f4.png": (embedding_from_seed(22), embedding_from_seed(122), 1.1),
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
                    "probability": 0.94,
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
                    "probability": 0.95,
                },
                {
                    "face_observation_id": observation_ids[3],
                    "cluster_label": 20,
                    "person_temp_key": "p1",
                    "assignment_source": "hdbscan",
                    "probability": 0.92,
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

    service = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    )
    result = service.run_frozen_v5_assignment(
        scan_session_id=session_id,
        run_kind="scan_full",
        embedding_calculator=calculator,
    )

    repo = ClusterRepository(layout.library_db)
    clusters = repo.list_active_clusters()
    assert result.assignment_count == 4
    assert len(clusters) == 2
    assert any(item.member_count >= 2 for item in clusters)

    first_members = repo.list_cluster_members(clusters[0].id)
    first_reps = repo.list_cluster_rep_faces(clusters[0].id)
    assert [item.face_observation_id for item in first_members]
    assert [item.face_observation_id for item in first_reps]

    conn = sqlite3.connect(layout.library_db)
    try:
        stored = conn.execute(
            "SELECT COUNT(*) FROM face_cluster WHERE status='active'"
        ).fetchone()
    finally:
        conn.close()
    assert stored is not None and int(stored[0]) == 2


def test_full_rebuild_keeps_stable_cluster_uuid_when_person_and_members_match(tmp_path: Path, monkeypatch) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    observation_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (220, 180, 160), "quality_score": 0.91},
            {"asset_index": 0, "color": (218, 178, 158), "quality_score": 0.88},
        ],
    )
    calculator = fake_embedding_calculator_from_map(
        {
            "f1.png": (embedding_from_seed(31), embedding_from_seed(131), 1.1),
            "f2.png": (embedding_from_seed(32), embedding_from_seed(132), 1.1),
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
                    "probability": 0.94,
                },
                {
                    "face_observation_id": observation_ids[1],
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "assignment_source": "hdbscan",
                    "probability": 0.93,
                },
            ],
            "persons": [{"person_temp_key": "p0", "face_observation_ids": observation_ids}],
            "clusters": [
                {
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "member_face_observation_ids": observation_ids,
                    "representative_face_observation_ids": [observation_ids[0]],
                }
            ],
            "stats": {"person_count": 1, "assignment_count": 2},
        }

    monkeypatch.setattr("hikbox_pictures.product.scan.assignment_stage.run_frozen_v5_assignment", fake_run)

    service = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    )
    service.run_frozen_v5_assignment(
        scan_session_id=session_id,
        run_kind="scan_full",
        embedding_calculator=calculator,
    )

    repo = ClusterRepository(layout.library_db)
    first_cluster = repo.list_active_clusters()[0]

    service.run_frozen_v5_assignment(
        scan_session_id=session_id,
        run_kind="scan_full",
        embedding_calculator=calculator,
    )

    second_cluster = repo.list_active_clusters()[0]
    assert second_cluster.cluster_uuid == first_cluster.cluster_uuid


def test_full_rebuild_keeps_stable_cluster_uuid_when_only_active_evidence_remains(tmp_path: Path, monkeypatch) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    observation_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (220, 180, 160), "quality_score": 0.91},
            {"asset_index": 1, "color": (218, 178, 158), "quality_score": 0.88},
        ],
    )
    calculator = fake_embedding_calculator_from_map(
        {
            "f1.png": (embedding_from_seed(51), embedding_from_seed(151), 1.1),
            "f2.png": (embedding_from_seed(52), embedding_from_seed(152), 1.1),
        }
    )
    run_calls = {"count": 0}

    def fake_run(*, faces, params):
        run_calls["count"] += 1
        if run_calls["count"] == 1:
            member_ids = observation_ids
        else:
            member_ids = [observation_ids[1]]
        return {
            "faces": [
                {
                    "face_observation_id": face_id,
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "assignment_source": "hdbscan",
                    "probability": 0.94,
                }
                for face_id in member_ids
            ],
            "persons": [{"person_temp_key": "p0", "face_observation_ids": member_ids}],
            "clusters": [
                {
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "member_face_observation_ids": member_ids,
                    "representative_face_observation_ids": member_ids[:1],
                }
            ],
            "stats": {"person_count": 1, "assignment_count": len(member_ids)},
        }

    monkeypatch.setattr("hikbox_pictures.product.scan.assignment_stage.run_frozen_v5_assignment", fake_run)

    service = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    )
    service.run_frozen_v5_assignment(
        scan_session_id=session_id,
        run_kind="scan_full",
        embedding_calculator=calculator,
    )

    repo = ClusterRepository(layout.library_db)
    first_cluster = repo.list_active_clusters()[0]

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

    service.run_frozen_v5_assignment(
        scan_session_id=session_id,
        run_kind="scan_full",
        embedding_calculator=calculator,
    )

    second_cluster = repo.list_active_clusters()[0]
    assert second_cluster.cluster_uuid == first_cluster.cluster_uuid


def test_cluster_repo_uses_remaining_active_members_to_refresh_representatives(tmp_path: Path, monkeypatch) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    observation_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (220, 180, 160), "quality_score": 0.99},
            {"asset_index": 0, "color": (210, 170, 150), "quality_score": 0.97},
            {"asset_index": 1, "color": (150, 210, 220), "quality_score": 0.80},
        ],
    )
    calculator = fake_embedding_calculator_from_map(
        {
            "f1.png": (embedding_from_seed(41), embedding_from_seed(141), 1.1),
            "f2.png": (embedding_from_seed(42), embedding_from_seed(142), 1.1),
            "f3.png": (embedding_from_seed(43), embedding_from_seed(143), 1.1),
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
                    "probability": 0.94,
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
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "assignment_source": "hdbscan",
                    "probability": 0.92,
                },
            ],
            "persons": [{"person_temp_key": "p0", "face_observation_ids": observation_ids}],
            "clusters": [
                {
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "member_face_observation_ids": observation_ids,
                    "representative_face_observation_ids": observation_ids,
                }
            ],
            "stats": {"person_count": 1, "assignment_count": 3},
        }

    monkeypatch.setattr("hikbox_pictures.product.scan.assignment_stage.run_frozen_v5_assignment", fake_run)

    service = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    )
    baseline = service.run_frozen_v5_assignment(
        scan_session_id=session_id,
        run_kind="scan_full",
        embedding_calculator=calculator,
    )
    repo = ClusterRepository(layout.library_db)
    cluster = repo.list_active_clusters()[0]

    conn = sqlite3.connect(layout.library_db)
    try:
        conn.execute(
            """
            UPDATE photo_asset
            SET asset_status='missing', updated_at=CURRENT_TIMESTAMP
            WHERE id IN (
              SELECT photo_asset_id FROM face_observation WHERE id IN (?, ?)
            )
            """,
            (observation_ids[0], observation_ids[1]),
        )
        conn.commit()
    finally:
        conn.close()

    conn = repo.connect()
    try:
        repo.append_face_to_cluster(
            cluster_id=cluster.id,
            assignment_run_id=baseline.assignment_run_id,
            face_observation_id=observation_ids[2],
            face_quality_by_id={observation_ids[2]: 0.80},
            conn=conn,
        )
        conn.commit()
    finally:
        conn.close()

    refreshed_reps = repo.list_cluster_rep_faces(cluster.id)
    assert [item.face_observation_id for item in refreshed_reps] == [observation_ids[2]]
