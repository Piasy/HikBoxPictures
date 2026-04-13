from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.ann import AnnIndexStore
from hikbox_pictures.deepface_engine import embedding_to_blob
from hikbox_pictures.repositories.person_repo import PersonRepo


class PrototypeService:
    def __init__(self, conn: sqlite3.Connection, person_repo: PersonRepo, ann_index_store: AnnIndexStore) -> None:
        self.conn = conn
        self.person_repo = person_repo
        self.ann_index_store = ann_index_store

    def rebuild_all_person_prototypes(self, *, model_key: str = "pipeline-stub-v1") -> int:
        grouped_embeddings: dict[int, list[np.ndarray[Any, np.dtype[np.float32]]]] = defaultdict(list)
        rows = self.conn.execute(
            """
            SELECT pfa.person_id, fe.vector_blob
            FROM person_face_assignment AS pfa
            JOIN face_embedding AS fe
              ON fe.face_observation_id = pfa.face_observation_id
             AND fe.feature_type = 'face'
            JOIN person AS p
              ON p.id = pfa.person_id
            WHERE pfa.active = 1
              AND p.status = 'active'
              AND p.ignored = 0
              AND fe.model_key = ?
              AND fe.normalized = 1
            ORDER BY pfa.person_id ASC, pfa.id ASC
            """,
            (str(model_key),),
        ).fetchall()

        expected_dim_by_person: dict[int, int] = {}
        for row in rows:
            person_id = int(row["person_id"])
            vector_blob = row["vector_blob"]
            if not isinstance(vector_blob, (bytes, bytearray, memoryview)):
                continue
            vector = np.frombuffer(vector_blob, dtype=np.float32).copy()
            if vector.ndim != 1 or vector.size == 0:
                continue
            expected_dim = expected_dim_by_person.get(person_id)
            if expected_dim is None:
                expected_dim_by_person[person_id] = int(vector.size)
            elif int(vector.size) != expected_dim:
                continue
            grouped_embeddings[person_id].append(vector.astype(np.float32, copy=False))

        rebuilt_count = 0
        for person_id in self.person_repo.list_active_person_ids():
            samples = grouped_embeddings.get(person_id, [])
            if not samples:
                self.person_repo.deactivate_active_centroid_prototypes(
                    person_id=person_id,
                    model_key=model_key,
                )
                continue
            centroid = np.mean(np.vstack(samples), axis=0).astype(np.float32, copy=False)
            norm = float(np.linalg.norm(centroid))
            if norm > 0:
                centroid = centroid / norm
            self.person_repo.replace_centroid_prototype(
                person_id=person_id,
                vector_blob=embedding_to_blob(centroid),
                model_key=model_key,
                quality_score=float(len(samples)),
            )
            rebuilt_count += 1

        return rebuilt_count

    def rebuild_ann_index_from_active_prototypes(self, *, model_key: str = "pipeline-stub-v1") -> int:
        prototypes = self.person_repo.list_active_prototypes(
            prototype_type="centroid",
            model_key=model_key,
        )
        return self.ann_index_store.rebuild_from_prototypes(prototypes)
