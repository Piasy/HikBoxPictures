from __future__ import annotations

from collections.abc import Callable
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

    def resolve_default_model_key(self, *, preferred: str | None = None) -> str | None:
        if preferred is not None:
            row = self.conn.execute(
                """
                SELECT model_key
                FROM face_embedding
                WHERE feature_type = 'face'
                  AND normalized = 1
                  AND model_key = ?
                LIMIT 1
                """,
                (str(preferred),),
            ).fetchone()
            if row is not None:
                return str(row["model_key"])

        row = self.conn.execute(
            """
            SELECT model_key
            FROM face_embedding
            WHERE feature_type = 'face'
              AND normalized = 1
              AND model_key IS NOT NULL
            GROUP BY model_key
            ORDER BY COUNT(*) DESC, model_key ASC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return preferred
        return str(row["model_key"])

    def rebuild_all_person_prototypes(
        self,
        *,
        model_key: str | None = None,
        progress_reporter: Callable[[dict[str, object]], None] | None = None,
    ) -> int:
        resolved_model_key = self.resolve_default_model_key(preferred=model_key)
        if resolved_model_key is None:
            return 0

        grouped_embeddings: dict[int, list[np.ndarray[Any, np.dtype[np.float32]]]] = defaultdict(list)
        rows = self.conn.execute(
            """
            SELECT pts.person_id, fe.vector_blob
            FROM person_trusted_sample AS pts
            JOIN face_embedding AS fe
              ON fe.face_observation_id = pts.face_observation_id
             AND fe.feature_type = 'face'
            JOIN person AS p
              ON p.id = pts.person_id
            WHERE pts.active = 1
              AND p.status = 'active'
              AND p.ignored = 0
              AND fe.model_key = ?
              AND fe.normalized = 1
            ORDER BY pts.person_id ASC, pts.id ASC
            """,
            (resolved_model_key,),
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
        active_person_ids = self.person_repo.list_active_person_ids()
        total_person_count = len(active_person_ids)
        for index, person_id in enumerate(active_person_ids, start=1):
            samples = grouped_embeddings.get(person_id, [])
            if not samples:
                self.person_repo.deactivate_active_centroid_prototypes(
                    person_id=person_id,
                    model_key=resolved_model_key,
                )
            else:
                centroid = np.mean(np.vstack(samples), axis=0).astype(np.float32, copy=False)
                norm = float(np.linalg.norm(centroid))
                if norm > 0:
                    centroid = centroid / norm
                self.person_repo.replace_centroid_prototype(
                    person_id=person_id,
                    vector_blob=embedding_to_blob(centroid),
                    model_key=resolved_model_key,
                    quality_score=float(len(samples)),
                )
                rebuilt_count += 1
            self._report_progress(
                progress_reporter,
                subphase="rebuild_prototypes",
                total_count=total_person_count,
                completed_count=index,
                unit="person",
            )

        return rebuilt_count

    def rebuild_person_prototype(self, *, person_id: int, model_key: str | None = None) -> bool:
        resolved_model_key = self.resolve_default_model_key(preferred=model_key)
        if resolved_model_key is None:
            return False

        person = self.person_repo.get_person(int(person_id))
        if person is None:
            raise LookupError(f"person {person_id} 不存在")
        if str(person["status"]) != "active" or bool(person["ignored"]):
            self.person_repo.deactivate_active_centroid_prototypes(
                person_id=int(person_id),
                model_key=resolved_model_key,
            )
            return False

        rows = self.conn.execute(
            """
            SELECT fe.vector_blob
            FROM person_trusted_sample AS pts
            JOIN face_embedding AS fe
              ON fe.face_observation_id = pts.face_observation_id
             AND fe.feature_type = 'face'
            WHERE pts.person_id = ?
              AND pts.active = 1
              AND fe.model_key = ?
              AND fe.normalized = 1
            ORDER BY pts.id ASC
            """,
            (int(person_id), resolved_model_key),
        ).fetchall()

        samples: list[np.ndarray[Any, np.dtype[np.float32]]] = []
        expected_dim: int | None = None
        for row in rows:
            vector_blob = row["vector_blob"]
            if not isinstance(vector_blob, (bytes, bytearray, memoryview)):
                continue
            vector = np.frombuffer(vector_blob, dtype=np.float32).copy()
            if vector.ndim != 1 or vector.size == 0:
                continue
            if expected_dim is None:
                expected_dim = int(vector.size)
            elif int(vector.size) != expected_dim:
                continue
            samples.append(vector.astype(np.float32, copy=False))

        if not samples:
            self.person_repo.deactivate_active_centroid_prototypes(
                person_id=int(person_id),
                model_key=resolved_model_key,
            )
            return False

        centroid = np.mean(np.vstack(samples), axis=0).astype(np.float32, copy=False)
        norm = float(np.linalg.norm(centroid))
        if norm > 0:
            centroid = centroid / norm
        self.person_repo.replace_centroid_prototype(
            person_id=int(person_id),
            vector_blob=embedding_to_blob(centroid),
            model_key=resolved_model_key,
            quality_score=float(len(samples)),
        )
        return True

    def rebuild_ann_index_from_active_prototypes(
        self,
        *,
        model_key: str | None = None,
        progress_reporter: Callable[[dict[str, object]], None] | None = None,
    ) -> int:
        resolved_model_key = self.resolve_default_model_key(preferred=model_key)
        if resolved_model_key is None:
            result = self.ann_index_store.rebuild_from_prototypes([])
            self._report_progress(
                progress_reporter,
                subphase="rebuild_ann_index",
                total_count=0,
                completed_count=0,
                unit="prototype",
            )
            return result
        prototypes = self.person_repo.list_active_prototypes(
            prototype_type="centroid",
            model_key=resolved_model_key,
        )
        result = self.ann_index_store.rebuild_from_prototypes(prototypes)
        self._report_progress(
            progress_reporter,
            subphase="rebuild_ann_index",
            total_count=len(prototypes),
            completed_count=len(prototypes),
            unit="prototype",
        )
        return result

    def sync_person_ann_entry(self, *, person_id: int, model_key: str | None = None) -> int:
        resolved_model_key = self.resolve_default_model_key(preferred=model_key)
        if resolved_model_key is None:
            return self.ann_index_store.remove_person(int(person_id))

        prototypes = self.person_repo.list_active_prototypes(
            prototype_type="centroid",
            model_key=resolved_model_key,
            person_id=int(person_id),
        )
        if not prototypes:
            return self.ann_index_store.remove_person(int(person_id))

        try:
            return self.ann_index_store.upsert_person_prototype(prototypes[0])
        except ValueError as exc:
            if "维度不匹配" not in str(exc):
                raise
            all_prototypes = self.person_repo.list_active_prototypes(
                prototype_type="centroid",
                model_key=resolved_model_key,
            )
            return self.ann_index_store.rebuild_from_prototypes(all_prototypes)

    def activate_prepared_cluster_prototype(self, *, run_id: int, cluster_id: int, person_id: int) -> None:
        ok = self.rebuild_person_prototype(person_id=int(person_id), model_key="insightface")
        if not ok:
            raise RuntimeError(
                f"发布阶段 prototype 构建失败: run_id={int(run_id)}, cluster_id={int(cluster_id)}, person_id={int(person_id)}"
            )

    def _report_progress(
        self,
        progress_reporter: Callable[[dict[str, object]], None] | None,
        *,
        subphase: str,
        total_count: int,
        completed_count: int,
        unit: str,
    ) -> None:
        if progress_reporter is None:
            return
        total = max(0, int(total_count))
        completed = min(max(0, int(completed_count)), total)
        percent = 100.0 if total <= 0 else round((completed / total) * 100.0, 1)
        progress_reporter(
            {
                "phase": "prototype_ann_rebuild_optional",
                "subphase": str(subphase),
                "status": "running",
                "unit": str(unit),
                "total_count": total,
                "completed_count": completed,
                "percent": percent,
            }
        )
