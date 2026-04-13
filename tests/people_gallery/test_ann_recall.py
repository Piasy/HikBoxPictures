from __future__ import annotations

from pathlib import Path

import numpy as np

from hikbox_pictures.ann import AnnIndexStore
from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.db.migrator import apply_migrations
from hikbox_pictures.deepface_engine import embedding_to_blob
from hikbox_pictures.repositories.person_repo import PersonRepo
from hikbox_pictures.services.ann_assignment_service import AnnAssignmentService


def test_ann_topk_recall_first_candidate_person_id_correct(tmp_path: Path) -> None:
    conn = connect_db(tmp_path / "library.db")
    try:
        apply_migrations(conn)
        person_repo = PersonRepo(conn)
        person_a = person_repo.create_person("人物A", confirmed=True)
        person_b = person_repo.create_person("人物B", confirmed=True)
        person_c = person_repo.create_person("人物C", confirmed=True)

        person_repo.replace_centroid_prototype(
            person_id=person_a,
            vector_blob=embedding_to_blob(np.asarray([1.0, 0.0, 0.0, 0.0], dtype=np.float32)),
        )
        person_repo.replace_centroid_prototype(
            person_id=person_b,
            vector_blob=embedding_to_blob(np.asarray([0.0, 1.0, 0.0, 0.0], dtype=np.float32)),
        )
        person_repo.replace_centroid_prototype(
            person_id=person_c,
            vector_blob=embedding_to_blob(np.asarray([0.0, 0.0, 1.0, 0.0], dtype=np.float32)),
        )
        conn.commit()

        ann_store = AnnIndexStore(tmp_path / "prototype_index.npz")
        ann_store.rebuild_from_prototypes(person_repo.list_active_prototypes())
        assignment_service = AnnAssignmentService(ann_store)
        observation_embedding = np.asarray([0.02, 0.95, 0.01, 0.0], dtype=np.float32)

        candidates = assignment_service.recall_person_candidates(observation_embedding, top_k=3)

        assert len(candidates) == 3
        assert candidates[0]["person_id"] == person_b
        assert candidates[0]["distance"] <= candidates[1]["distance"]
    finally:
        conn.close()


def test_ann_topk_recall_expands_window_for_multi_prototype_same_person(tmp_path: Path) -> None:
    ann_store = AnnIndexStore(tmp_path / "prototype_index.npz")

    prototypes: list[dict[str, object]] = []
    for idx in range(8):
        prototypes.append(
            {
                "person_id": 1,
                "vector_blob": embedding_to_blob(np.asarray([0.01 * float(idx + 1), 0.0, 0.0, 0.0], dtype=np.float32)),
            }
        )
    prototypes.append(
        {
            "person_id": 2,
            "vector_blob": embedding_to_blob(np.asarray([1.0, 0.0, 0.0, 0.0], dtype=np.float32)),
        }
    )

    ann_store.rebuild_from_prototypes(prototypes)
    service = AnnAssignmentService(ann_store)

    candidates = service.recall_person_candidates(
        np.asarray([0.0, 0.0, 0.0, 0.0], dtype=np.float32),
        top_k=2,
    )
    assert len(candidates) == 2
    assert [int(item["person_id"]) for item in candidates] == [1, 2]


def test_replace_centroid_prototype_does_not_deactivate_other_model_key(tmp_path: Path) -> None:
    conn = connect_db(tmp_path / "library.db")
    try:
        apply_migrations(conn)
        person_repo = PersonRepo(conn)
        person_id = person_repo.create_person("人物A", confirmed=True)
        blob_a = embedding_to_blob(np.asarray([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
        blob_b = embedding_to_blob(np.asarray([0.0, 1.0, 0.0, 0.0], dtype=np.float32))
        conn.execute(
            """
            INSERT INTO person_prototype(person_id, prototype_type, model_key, vector_blob, active)
            VALUES (?, 'centroid', 'model-a', ?, 1)
            """,
            (person_id, blob_a),
        )
        conn.execute(
            """
            INSERT INTO person_prototype(person_id, prototype_type, model_key, vector_blob, active)
            VALUES (?, 'centroid', 'model-b', ?, 1)
            """,
            (person_id, blob_b),
        )
        conn.commit()

        person_repo.replace_centroid_prototype(
            person_id=person_id,
            vector_blob=embedding_to_blob(np.asarray([0.5, 0.5, 0.0, 0.0], dtype=np.float32)),
            model_key="model-a",
        )
        conn.commit()

        active_rows = conn.execute(
            """
            SELECT model_key, COUNT(*) AS c
            FROM person_prototype
            WHERE person_id = ?
              AND prototype_type = 'centroid'
              AND active = 1
            GROUP BY model_key
            ORDER BY model_key ASC
            """,
            (person_id,),
        ).fetchall()
        active_map = {str(row["model_key"]): int(row["c"]) for row in active_rows}
        assert active_map["model-a"] == 1
        assert active_map["model-b"] == 1
    finally:
        conn.close()
