"""增量归属与局部重建服务。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from hikbox_pictures.product.db.connection import connect_sqlite
from hikbox_pictures.product.engine.frozen_v5 import late_fusion_similarity, run_frozen_v5_assignment
from hikbox_pictures.product.engine.param_snapshot import build_frozen_v5_param_snapshot
from hikbox_pictures.product.scan.cluster_repository import ClusterRepository


@dataclass(frozen=True)
class IncrementalAssignmentResult:
    attached_count: int
    local_rebuild_count: int
    person_count: int


class IncrementalAssignmentService:
    """利用持久 cluster 对新增或待重分配 observation 做增量归属。"""

    def __init__(
        self,
        *,
        library_db_path: Path,
        embedding_db_path: Path,
        cluster_repo: ClusterRepository,
        attach_threshold: float = 0.82,
        attach_margin: float = 0.03,
        candidate_threshold: float = 0.72,
    ):
        self._library_db_path = Path(library_db_path)
        self._embedding_db_path = Path(embedding_db_path)
        self._cluster_repo = cluster_repo
        self._attach_threshold = float(attach_threshold)
        self._attach_margin = float(attach_margin)
        self._candidate_threshold = float(candidate_threshold)

    def run(
        self,
        *,
        assignment_run_id: int,
        face_observation_ids: list[int],
        conn: sqlite3.Connection | None = None,
        abort_checker: Callable[[], None] | None = None,
    ) -> IncrementalAssignmentResult:
        candidate_ids = sorted({int(face_id) for face_id in face_observation_ids if int(face_id) > 0})
        managed_conn = conn is None
        db = conn or connect_sqlite(self._library_db_path)
        try:
            if managed_conn:
                db.execute("BEGIN IMMEDIATE")
            attached_count = 0
            local_rebuild_count = 0
            face_quality_by_id = self._load_face_quality_by_id(candidate_ids, conn=db)
            if abort_checker is not None:
                abort_checker()
            for face_id in candidate_ids:
                if abort_checker is not None:
                    abort_checker()
                decision = self._decide(face_observation_id=face_id, conn=db)
                if decision is None:
                    continue
                if decision["mode"] == "attach":
                    self._attach_face(
                        conn=db,
                        face_observation_id=face_id,
                        person_id=int(decision["person_id"]),
                        cluster_id=int(decision["cluster_id"]),
                        assignment_run_id=assignment_run_id,
                        face_quality_by_id=face_quality_by_id,
                    )
                    attached_count += 1
                    if abort_checker is not None:
                        abort_checker()
                    continue
                local_rebuild_count += 1
                rebuilt = self._local_rebuild(
                    conn=db,
                    face_observation_id=face_id,
                    candidate_cluster_ids=[int(value) for value in decision["candidate_cluster_ids"]],
                    assignment_run_id=assignment_run_id,
                    face_quality_by_id=face_quality_by_id,
                )
                if rebuilt:
                    attached_count += 1
                if abort_checker is not None:
                    abort_checker()
            person_count = int(db.execute("SELECT COUNT(*) FROM person WHERE status='active'").fetchone()[0])
            if managed_conn:
                db.commit()
            return IncrementalAssignmentResult(
                attached_count=attached_count,
                local_rebuild_count=local_rebuild_count,
                person_count=person_count,
            )
        except Exception:
            if managed_conn:
                db.rollback()
            raise
        finally:
            if managed_conn:
                db.close()

    def _decide(
        self,
        *,
        face_observation_id: int,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, object] | None:
        face_embedding = self._load_face_embedding(face_observation_id)
        if face_embedding is None:
            return None
        recalled_scores: list[dict[str, object]] = []
        for cluster in self._cluster_repo.list_active_clusters(conn=conn):
            rep_ids = self._filter_active_face_ids(
                [item.face_observation_id for item in self._cluster_repo.list_cluster_rep_faces(cluster.id, conn=conn)],
                conn=conn,
            )
            if not rep_ids:
                continue
            rep_score = self._best_similarity(face_embedding, rep_ids)
            recalled_scores.append(
                {
                    "cluster_id": cluster.id,
                    "person_id": cluster.person_id,
                    "rep_score": rep_score,
                }
            )
        if not recalled_scores:
            return None
        recalled_scores.sort(key=lambda item: (-float(item["rep_score"]), int(item["cluster_id"])))
        recalled_candidates = [
            item for item in recalled_scores if float(item["rep_score"]) >= self._candidate_threshold
        ]
        if not recalled_candidates:
            recalled_candidates = [recalled_scores[0]]

        reranked_scores: list[dict[str, object]] = []
        for recalled in recalled_candidates:
            member_ids = self._filter_active_face_ids(
                [
                    item.face_observation_id
                    for item in self._cluster_repo.list_cluster_members(int(recalled["cluster_id"]), conn=conn)
                ],
                conn=conn,
            )
            if not member_ids:
                continue
            member_score = self._best_similarity(face_embedding, member_ids)
            reranked_scores.append(
                {
                    "cluster_id": int(recalled["cluster_id"]),
                    "person_id": int(recalled["person_id"]),
                    "rep_score": float(recalled["rep_score"]),
                    "score": member_score,
                }
            )
        if not reranked_scores:
            return None
        reranked_scores.sort(
            key=lambda item: (-float(item["score"]), -float(item["rep_score"]), int(item["cluster_id"]))
        )
        best = reranked_scores[0]
        second_score = float(reranked_scores[1]["score"]) if len(reranked_scores) > 1 else -1.0
        margin = float(best["score"]) - second_score
        if float(best["score"]) >= self._attach_threshold and margin >= self._attach_margin:
            return {"mode": "attach", **best}
        return {
            "mode": "local_rebuild",
            "candidate_cluster_ids": [int(item["cluster_id"]) for item in reranked_scores[:2]],
        }

    def _attach_face(
        self,
        *,
        conn: sqlite3.Connection,
        face_observation_id: int,
        person_id: int,
        cluster_id: int,
        assignment_run_id: int,
        face_quality_by_id: dict[int, float],
    ) -> None:
        conn.execute(
            "UPDATE person_face_assignment SET active=0, updated_at=CURRENT_TIMESTAMP WHERE face_observation_id=? AND active=1",
            (int(face_observation_id),),
        )
        conn.execute(
            """
            INSERT INTO person_face_assignment(
              person_id, face_observation_id, assignment_run_id, assignment_source,
              active, confidence, margin, created_at, updated_at
            ) VALUES (?, ?, ?, 'person_consensus', 1, NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (int(person_id), int(face_observation_id), int(assignment_run_id)),
        )
        conn.execute(
            "UPDATE face_observation SET pending_reassign=0, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (int(face_observation_id),),
        )
        self._cluster_repo.append_face_to_cluster(
            cluster_id=cluster_id,
            assignment_run_id=assignment_run_id,
            face_observation_id=face_observation_id,
            face_quality_by_id=face_quality_by_id,
            conn=conn,
        )

    def _local_rebuild(
        self,
        *,
        conn: sqlite3.Connection,
        face_observation_id: int,
        candidate_cluster_ids: list[int],
        assignment_run_id: int,
        face_quality_by_id: dict[int, float],
    ) -> bool:
        subset_face_ids = {int(face_observation_id)}
        cluster_person: dict[int, int] = {}
        for cluster_id in candidate_cluster_ids:
            cluster = next((item for item in self._cluster_repo.list_active_clusters(conn=conn) if item.id == cluster_id), None)
            if cluster is None:
                continue
            cluster_person[cluster.id] = cluster.person_id
            subset_face_ids.update(
                self._filter_active_face_ids(
                    [member.face_observation_id for member in self._cluster_repo.list_cluster_members(cluster.id, conn=conn)],
                    conn=conn,
                )
            )
        face_rows = self._load_subset_faces(sorted(subset_face_ids))
        if len(face_rows) <= 1:
            return False
        runtime_result = run_frozen_v5_assignment(
            faces=face_rows,
            params=build_frozen_v5_param_snapshot(),
        )
        candidate_row = next(
            (row for row in runtime_result.get("faces", []) if int(row.get("face_observation_id") or 0) == int(face_observation_id)),
            None,
        )
        if not candidate_row or not candidate_row.get("person_temp_key"):
            return False
        cluster_label = int(candidate_row.get("cluster_label") or -1)
        if cluster_label == -1:
            return False
        local_member_ids = [
            int(row.get("face_observation_id") or 0)
            for row in runtime_result.get("faces", [])
            if int(row.get("cluster_label") or -1) == cluster_label and int(row.get("face_observation_id") or 0) > 0
        ]
        overlap_by_cluster: list[tuple[int, int]] = []
        for cluster_id in candidate_cluster_ids:
            existing_member_ids = set(
                self._filter_active_face_ids(
                    [item.face_observation_id for item in self._cluster_repo.list_cluster_members(cluster_id, conn=conn)],
                    conn=conn,
                )
            )
            overlap_by_cluster.append((cluster_id, len(existing_member_ids.intersection(local_member_ids))))
        overlap_by_cluster.sort(key=lambda item: (-item[1], item[0]))
        if not overlap_by_cluster or overlap_by_cluster[0][1] <= 0:
            return False
        if len(overlap_by_cluster) > 1 and overlap_by_cluster[1][1] == overlap_by_cluster[0][1]:
            return False
        target_cluster_id = int(overlap_by_cluster[0][0])
        target_person_id = int(cluster_person[target_cluster_id])
        self._attach_face(
            conn=conn,
            face_observation_id=face_observation_id,
            person_id=target_person_id,
            cluster_id=target_cluster_id,
            assignment_run_id=assignment_run_id,
            face_quality_by_id=face_quality_by_id,
        )
        return True

    def _best_similarity(self, target_embedding: dict[str, np.ndarray], face_ids: list[int]) -> float:
        best = -1.0
        for face_id in face_ids:
            candidate = self._load_face_embedding(face_id)
            if candidate is None:
                continue
            sim_main = float(np.dot(target_embedding["main"], candidate["main"]))
            sim_flip = None
            if target_embedding.get("flip") is not None and candidate.get("flip") is not None:
                sim_flip = float(np.dot(target_embedding["flip"], candidate["flip"]))
            best = max(best, late_fusion_similarity(sim_main=sim_main, sim_flip=sim_flip))
        return best

    def _load_face_embedding(self, face_observation_id: int) -> dict[str, np.ndarray] | None:
        conn = connect_sqlite(self._embedding_db_path)
        try:
            rows = conn.execute(
                """
                SELECT variant, vector_blob
                FROM face_embedding
                WHERE face_observation_id=?
                ORDER BY variant ASC
                """,
                (int(face_observation_id),),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return None
        result: dict[str, np.ndarray] = {}
        for variant, blob in rows:
            result[str(variant)] = np.frombuffer(blob, dtype=np.float32)
        main = result.get("main")
        if main is None:
            return None
        return {"main": main, "flip": result.get("flip")}

    def _load_subset_faces(self, face_observation_ids: list[int]) -> list[dict[str, object]]:
        if not face_observation_ids:
            return []
        placeholders = ", ".join("?" for _ in face_observation_ids)
        conn = connect_sqlite(self._library_db_path)
        try:
            rows = conn.execute(
                f"""
                SELECT
                  f.id,
                  f.photo_asset_id,
                  p.primary_path,
                  f.quality_score,
                  f.detector_confidence,
                  f.face_area_ratio
                FROM face_observation AS f
                INNER JOIN photo_asset AS p ON p.id = f.photo_asset_id
                WHERE f.id IN ({placeholders})
                  AND f.active = 1
                  AND p.asset_status = 'active'
                ORDER BY f.id ASC
                """,
                tuple(int(face_id) for face_id in face_observation_ids),
            ).fetchall()
        finally:
            conn.close()
        face_rows: list[dict[str, object]] = []
        for row in rows:
            face_id = int(row[0])
            embedding = self._load_face_embedding(face_id)
            if embedding is None:
                continue
            face_rows.append(
                {
                    "face_observation_id": face_id,
                    "photo_asset_id": int(row[1]),
                    "photo_relpath": str(row[2]),
                    "quality_score": float(row[3]),
                    "detector_confidence": float(row[4]),
                    "face_area_ratio": float(row[5]),
                    "embedding_main": embedding["main"].astype(float).tolist(),
                    "embedding_flip": None if embedding.get("flip") is None else embedding["flip"].astype(float).tolist(),
                }
            )
        return face_rows

    def _load_face_quality_by_id(
        self,
        face_observation_ids: list[int],
        *,
        conn: sqlite3.Connection,
    ) -> dict[int, float]:
        if not face_observation_ids:
            return {}
        placeholders = ", ".join("?" for _ in face_observation_ids)
        rows = conn.execute(
            f"SELECT id, quality_score FROM face_observation WHERE id IN ({placeholders})",
            tuple(int(face_id) for face_id in face_observation_ids),
        ).fetchall()
        return {int(row[0]): float(row[1] or 0.0) for row in rows}

    def _filter_active_face_ids(
        self,
        face_observation_ids: list[int],
        *,
        conn: sqlite3.Connection | None,
    ) -> list[int]:
        unique_ids = sorted({int(face_id) for face_id in face_observation_ids if int(face_id) > 0})
        if not unique_ids:
            return []
        placeholders = ", ".join("?" for _ in unique_ids)
        managed_conn = conn is None
        db = conn or connect_sqlite(self._library_db_path)
        try:
            rows = db.execute(
                f"""
                SELECT f.id
                FROM face_observation AS f
                INNER JOIN photo_asset AS p ON p.id = f.photo_asset_id
                WHERE f.id IN ({placeholders})
                  AND f.active = 1
                  AND p.asset_status = 'active'
                ORDER BY f.id ASC
                """,
                tuple(unique_ids),
            ).fetchall()
            return [int(row[0]) for row in rows]
        finally:
            if managed_conn:
                db.close()
