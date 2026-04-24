"""增量归属与局部重建服务。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
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
    anchor_candidate_face_count: int = 0
    anchor_attached_face_count: int = 0
    anchor_missed_face_count: int = 0
    anchor_missed_by_person: dict[int, int] = field(default_factory=dict)


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
            anchor_attached_face_count = 0
            anchor_missed_by_person: dict[int, int] = {}
            face_quality_by_id = self._load_face_quality_by_id(candidate_ids, conn=db)
            pending_reassign_face_ids = self._load_pending_reassign_face_ids(candidate_ids, conn=db)
            anchor_person_ids = self._load_anchor_person_ids(conn=db)
            pending_face_ids = set(candidate_ids)
            if abort_checker is not None:
                abort_checker()
            for cluster_row in self._build_candidate_batch_clusters(candidate_ids):
                cluster_face_ids = sorted(
                    {
                        int(face_id)
                        for face_id in cluster_row.get("member_face_observation_ids", [])
                        if int(face_id) > 0
                    }
                )
                if len(cluster_face_ids) <= 1:
                    continue
                pending_face_ids.difference_update(cluster_face_ids)
                candidate_cluster_ids, anchor_candidate_person_by_face = self._collect_candidate_cluster_ids_for_faces(
                    face_observation_ids=cluster_face_ids,
                    anchor_person_ids=anchor_person_ids,
                    conn=db,
                )
                if not candidate_cluster_ids:
                    assigned_face_ids = self._create_new_person_cluster(
                        conn=db,
                        member_face_ids=cluster_face_ids,
                        representative_face_ids=[
                            int(face_id)
                            for face_id in cluster_row.get("representative_face_observation_ids", [])
                            if int(face_id) > 0
                        ],
                        assignment_run_id=assignment_run_id,
                        face_quality_by_id=face_quality_by_id,
                    )
                    attached_count += len(assigned_face_ids)
                    anchor_attached_delta, anchor_missed_delta = self._summarize_anchor_outcomes(
                        face_observation_ids=cluster_face_ids,
                        anchor_candidate_person_by_face=anchor_candidate_person_by_face,
                        conn=db,
                    )
                    anchor_attached_face_count += anchor_attached_delta
                    self._merge_anchor_missed_by_person(
                        anchor_missed_by_person=anchor_missed_by_person,
                        delta=anchor_missed_delta,
                    )
                    if abort_checker is not None:
                        abort_checker()
                    continue
                local_rebuild_count += 1
                assigned_face_ids = self._local_rebuild_faces(
                    conn=db,
                    face_observation_ids=cluster_face_ids,
                    candidate_cluster_ids=candidate_cluster_ids,
                    assignment_run_id=assignment_run_id,
                    face_quality_by_id=face_quality_by_id,
                )
                attached_count += len(assigned_face_ids)
                pending_face_ids.update(set(cluster_face_ids) - assigned_face_ids)
                anchor_attached_delta, anchor_missed_delta = self._summarize_anchor_outcomes(
                    face_observation_ids=cluster_face_ids,
                    anchor_candidate_person_by_face=anchor_candidate_person_by_face,
                    conn=db,
                )
                anchor_attached_face_count += anchor_attached_delta
                self._merge_anchor_missed_by_person(
                    anchor_missed_by_person=anchor_missed_by_person,
                    delta=anchor_missed_delta,
                )
                if abort_checker is not None:
                    abort_checker()
            for face_id in sorted(pending_face_ids):
                if abort_checker is not None:
                    abort_checker()
                decision = self._decide(face_observation_id=face_id, conn=db)
                anchor_candidate_person_id = self._select_anchor_candidate_person_id(
                    decision=decision,
                    anchor_person_ids=anchor_person_ids,
                )
                anchor_candidate_person_by_face = (
                    {int(face_id): int(anchor_candidate_person_id)} if int(anchor_candidate_person_id) > 0 else {}
                )
                if decision is None:
                    if int(face_id) in pending_reassign_face_ids:
                        assigned_face_ids = self._create_new_person_cluster(
                            conn=db,
                            member_face_ids=[int(face_id)],
                            representative_face_ids=[int(face_id)],
                            assignment_run_id=assignment_run_id,
                            face_quality_by_id=face_quality_by_id,
                        )
                        attached_count += len(assigned_face_ids)
                    anchor_attached_delta, anchor_missed_delta = self._summarize_anchor_outcomes(
                        face_observation_ids=[face_id],
                        anchor_candidate_person_by_face=anchor_candidate_person_by_face,
                        conn=db,
                    )
                    anchor_attached_face_count += anchor_attached_delta
                    self._merge_anchor_missed_by_person(
                        anchor_missed_by_person=anchor_missed_by_person,
                        delta=anchor_missed_delta,
                    )
                    continue
                if decision["mode"] == "attach":
                    self._attach_face(
                        conn=db,
                        face_observation_id=face_id,
                        person_id=int(decision["person_id"]),
                        cluster_id=int(decision["cluster_id"]),
                        assignment_run_id=assignment_run_id,
                        face_quality_by_id=face_quality_by_id,
                        confidence=None if decision.get("score") is None else float(decision["score"]),
                        margin=None if decision.get("margin") is None else float(decision["margin"]),
                    )
                    attached_count += 1
                    anchor_attached_delta, anchor_missed_delta = self._summarize_anchor_outcomes(
                        face_observation_ids=[face_id],
                        anchor_candidate_person_by_face=anchor_candidate_person_by_face,
                        conn=db,
                    )
                    anchor_attached_face_count += anchor_attached_delta
                    self._merge_anchor_missed_by_person(
                        anchor_missed_by_person=anchor_missed_by_person,
                        delta=anchor_missed_delta,
                    )
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
                elif int(face_id) in pending_reassign_face_ids:
                    assigned_face_ids = self._create_new_person_cluster(
                        conn=db,
                        member_face_ids=[int(face_id)],
                        representative_face_ids=[int(face_id)],
                        assignment_run_id=assignment_run_id,
                        face_quality_by_id=face_quality_by_id,
                    )
                    attached_count += len(assigned_face_ids)
                anchor_attached_delta, anchor_missed_delta = self._summarize_anchor_outcomes(
                    face_observation_ids=[face_id],
                    anchor_candidate_person_by_face=anchor_candidate_person_by_face,
                    conn=db,
                )
                anchor_attached_face_count += anchor_attached_delta
                self._merge_anchor_missed_by_person(
                    anchor_missed_by_person=anchor_missed_by_person,
                    delta=anchor_missed_delta,
                )
                if abort_checker is not None:
                    abort_checker()
            person_count = int(db.execute("SELECT COUNT(*) FROM person WHERE status='active'").fetchone()[0])
            if managed_conn:
                db.commit()
            anchor_missed_face_count = sum(int(value) for value in anchor_missed_by_person.values())
            return IncrementalAssignmentResult(
                attached_count=attached_count,
                local_rebuild_count=local_rebuild_count,
                person_count=person_count,
                anchor_candidate_face_count=anchor_attached_face_count + anchor_missed_face_count,
                anchor_attached_face_count=anchor_attached_face_count,
                anchor_missed_face_count=anchor_missed_face_count,
                anchor_missed_by_person=dict(sorted(anchor_missed_by_person.items())),
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
        excluded_person_ids = self._load_active_excluded_person_ids(
            face_observation_ids=[face_observation_id],
            conn=conn,
        ).get(int(face_observation_id), set())
        recalled_scores: list[dict[str, object]] = []
        for cluster in self._cluster_repo.list_active_clusters(conn=conn):
            if int(cluster.person_id) in excluded_person_ids:
                continue
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
            {
                **item,
                "strong_candidate": True,
            }
            for item in recalled_scores
            if float(item["rep_score"]) >= self._candidate_threshold
        ]
        if not recalled_candidates:
            recalled_candidates = [{**recalled_scores[0], "strong_candidate": False}]

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
                    "strong_candidate": bool(recalled["strong_candidate"]),
                }
            )
        if not reranked_scores:
            return None
        reranked_scores.sort(
            key=lambda item: (-float(item["score"]), -float(item["rep_score"]), int(item["cluster_id"]))
        )
        strong_candidate_scores = [item for item in reranked_scores if bool(item["strong_candidate"])]
        best_candidate_person_id = int(strong_candidate_scores[0]["person_id"]) if strong_candidate_scores else 0
        best = reranked_scores[0]
        second_score = float(reranked_scores[1]["score"]) if len(reranked_scores) > 1 else -1.0
        margin = float(best["score"]) - second_score
        if float(best["score"]) >= self._attach_threshold and margin >= self._attach_margin:
            return {
                "mode": "attach",
                **best,
                "margin": float(margin),
                "best_candidate_person_id": best_candidate_person_id,
            }
        return {
            "mode": "local_rebuild",
            "candidate_cluster_ids": [int(item["cluster_id"]) for item in reranked_scores[:2]],
            "best_candidate_person_id": best_candidate_person_id,
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
        confidence: float | None,
        margin: float | None,
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
            ) VALUES (?, ?, ?, 'person_consensus', 1, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                int(person_id),
                int(face_observation_id),
                int(assignment_run_id),
                None if confidence is None else float(confidence),
                None if margin is None else float(margin),
            ),
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

    def _build_candidate_batch_clusters(self, face_observation_ids: list[int]) -> list[dict[str, object]]:
        face_rows = self._load_subset_faces(sorted(face_observation_ids))
        if len(face_rows) <= 1:
            return []
        runtime_result = run_frozen_v5_assignment(
            faces=face_rows,
            params=build_frozen_v5_param_snapshot(),
        )
        valid_face_ids = {int(face_id) for face_id in face_observation_ids if int(face_id) > 0}
        clusters: list[dict[str, object]] = []
        for row in runtime_result.get("clusters", []):
            member_face_ids = sorted(
                {
                    int(face_id)
                    for face_id in row.get("member_face_observation_ids", [])
                    if int(face_id) in valid_face_ids
                }
            )
            if len(member_face_ids) <= 1:
                continue
            clusters.append(
                {
                    "member_face_observation_ids": member_face_ids,
                    "representative_face_observation_ids": [
                        int(face_id)
                        for face_id in row.get("representative_face_observation_ids", [])
                        if int(face_id) in valid_face_ids
                    ],
                }
            )
        return clusters

    def _collect_candidate_cluster_ids_for_faces(
        self,
        *,
        face_observation_ids: list[int],
        anchor_person_ids: set[int],
        conn: sqlite3.Connection,
    ) -> tuple[list[int], dict[int, int]]:
        candidate_cluster_ids: set[int] = set()
        anchor_candidate_person_by_face: dict[int, int] = {}
        for face_id in sorted({int(face_id) for face_id in face_observation_ids if int(face_id) > 0}):
            decision = self._decide(face_observation_id=face_id, conn=conn)
            if decision is None:
                continue
            anchor_candidate_person_id = self._select_anchor_candidate_person_id(
                decision=decision,
                anchor_person_ids=anchor_person_ids,
            )
            if anchor_candidate_person_id > 0:
                anchor_candidate_person_by_face[int(face_id)] = int(anchor_candidate_person_id)
            if decision["mode"] == "attach":
                candidate_cluster_ids.add(int(decision["cluster_id"]))
                continue
            candidate_cluster_ids.update(
                int(cluster_id)
                for cluster_id in decision.get("candidate_cluster_ids", [])
                if int(cluster_id) > 0
            )
        return sorted(candidate_cluster_ids), anchor_candidate_person_by_face

    def _local_rebuild(
        self,
        *,
        conn: sqlite3.Connection,
        face_observation_id: int,
        candidate_cluster_ids: list[int],
        assignment_run_id: int,
        face_quality_by_id: dict[int, float],
    ) -> bool:
        assigned_face_ids = self._local_rebuild_faces(
            conn=conn,
            face_observation_ids=[int(face_observation_id)],
            candidate_cluster_ids=candidate_cluster_ids,
            assignment_run_id=assignment_run_id,
            face_quality_by_id=face_quality_by_id,
        )
        return int(face_observation_id) in assigned_face_ids

    def _local_rebuild_faces(
        self,
        *,
        conn: sqlite3.Connection,
        face_observation_ids: list[int],
        candidate_cluster_ids: list[int],
        assignment_run_id: int,
        face_quality_by_id: dict[int, float],
    ) -> set[int]:
        batch_face_ids = sorted({int(face_id) for face_id in face_observation_ids if int(face_id) > 0})
        if not batch_face_ids:
            return set()
        subset_face_ids: set[int] = set()
        for face_observation_id in batch_face_ids:
            subset_face_ids.add(int(face_observation_id))
        for cluster_id in candidate_cluster_ids:
            cluster = next((item for item in self._cluster_repo.list_active_clusters(conn=conn) if item.id == cluster_id), None)
            if cluster is None:
                continue
            subset_face_ids.update(
                self._filter_active_face_ids(
                    [member.face_observation_id for member in self._cluster_repo.list_cluster_members(cluster.id, conn=conn)],
                    conn=conn,
                )
            )
        face_rows = self._load_subset_faces(sorted(subset_face_ids))
        if len(face_rows) <= 1:
            return set()
        runtime_result = run_frozen_v5_assignment(
            faces=face_rows,
            params=build_frozen_v5_param_snapshot(),
        )
        person_groups: dict[str, list[int]] = {}
        for row in runtime_result.get("faces", []):
            person_temp_key = str(row.get("person_temp_key") or "")
            face_id = int(row.get("face_observation_id") or 0)
            if not person_temp_key or face_id <= 0:
                continue
            person_groups.setdefault(person_temp_key, []).append(face_id)

        active_person_ids = self._load_active_person_ids_for_faces(
            face_observation_ids=sorted(subset_face_ids - set(batch_face_ids)),
            conn=conn,
        )
        excluded_person_ids_by_face = self._load_active_excluded_person_ids(
            face_observation_ids=batch_face_ids,
            conn=conn,
        )
        assigned_face_ids: set[int] = set()
        batch_face_id_set = set(batch_face_ids)
        for group_face_ids in person_groups.values():
            candidate_group_ids = sorted(
                {
                    int(face_id)
                    for face_id in group_face_ids
                    if int(face_id) in batch_face_id_set
                }
            )
            if not candidate_group_ids:
                continue
            existing_person_ids = {
                int(active_person_ids[face_id])
                for face_id in group_face_ids
                if int(face_id) in active_person_ids
            }
            if len(existing_person_ids) == 1:
                target_person_id = next(iter(existing_person_ids))
                allowed_face_ids = [
                    int(face_id)
                    for face_id in candidate_group_ids
                    if target_person_id not in excluded_person_ids_by_face.get(int(face_id), set())
                ]
                blocked_face_ids = [
                    int(face_id)
                    for face_id in candidate_group_ids
                    if target_person_id in excluded_person_ids_by_face.get(int(face_id), set())
                ]
                if allowed_face_ids:
                    self._assign_faces_to_person(
                        conn=conn,
                        face_observation_ids=allowed_face_ids,
                        person_id=target_person_id,
                        assignment_run_id=assignment_run_id,
                        assignment_source="merge",
                    )
                    self._cluster_repo.create_cluster_for_person(
                        person_id=target_person_id,
                        assignment_run_id=assignment_run_id,
                        member_face_ids=allowed_face_ids,
                        representative_face_ids=[],
                        face_quality_by_id=face_quality_by_id,
                        conn=conn,
                        rebuild_scope="local",
                    )
                    assigned_face_ids.update(allowed_face_ids)
                if blocked_face_ids:
                    assigned_face_ids.update(
                        self._create_new_person_cluster(
                            conn=conn,
                            member_face_ids=blocked_face_ids,
                            representative_face_ids=[],
                            assignment_run_id=assignment_run_id,
                            face_quality_by_id=face_quality_by_id,
                        )
                    )
                if allowed_face_ids or blocked_face_ids:
                    continue
            if existing_person_ids:
                continue
            assigned_face_ids.update(
                self._create_new_person_cluster(
                    conn=conn,
                    member_face_ids=candidate_group_ids,
                    representative_face_ids=[],
                    assignment_run_id=assignment_run_id,
                    face_quality_by_id=face_quality_by_id,
                )
            )
        return assigned_face_ids

    def _assign_faces_to_person(
        self,
        *,
        conn: sqlite3.Connection,
        face_observation_ids: list[int],
        person_id: int,
        assignment_run_id: int,
        assignment_source: str,
    ) -> None:
        for face_id in sorted({int(face_id) for face_id in face_observation_ids if int(face_id) > 0}):
            conn.execute(
                "UPDATE person_face_assignment SET active=0, updated_at=CURRENT_TIMESTAMP WHERE face_observation_id=? AND active=1",
                (face_id,),
            )
            conn.execute(
                """
                INSERT INTO person_face_assignment(
                  person_id, face_observation_id, assignment_run_id, assignment_source,
                  active, confidence, margin, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (int(person_id), int(face_id), int(assignment_run_id), str(assignment_source)),
            )
            conn.execute(
                "UPDATE face_observation SET pending_reassign=0, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (face_id,),
            )

    def _create_new_person_cluster(
        self,
        *,
        conn: sqlite3.Connection,
        member_face_ids: list[int],
        representative_face_ids: list[int],
        assignment_run_id: int,
        face_quality_by_id: dict[int, float],
    ) -> set[int]:
        valid_face_ids = sorted({int(face_id) for face_id in member_face_ids if int(face_id) > 0})
        if not valid_face_ids:
            return set()
        cursor = conn.execute(
            """
            INSERT INTO person(
              person_uuid, display_name, is_named, status, merged_into_person_id, created_at, updated_at
            ) VALUES (?, NULL, 0, 'active', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (str(uuid.uuid4()),),
        )
        person_id = int(cursor.lastrowid)
        self._assign_faces_to_person(
            conn=conn,
            face_observation_ids=valid_face_ids,
            person_id=person_id,
            assignment_run_id=assignment_run_id,
            assignment_source="hdbscan",
        )
        self._cluster_repo.create_cluster_for_person(
            person_id=person_id,
            assignment_run_id=assignment_run_id,
            member_face_ids=valid_face_ids,
            representative_face_ids=representative_face_ids,
            face_quality_by_id=face_quality_by_id,
            conn=conn,
            rebuild_scope="local",
        )
        return set(valid_face_ids)

    def _select_anchor_candidate_person_id(
        self,
        *,
        decision: dict[str, object] | None,
        anchor_person_ids: set[int],
    ) -> int:
        if decision is None or not anchor_person_ids:
            return 0
        best_candidate_person_id = int(decision.get("best_candidate_person_id") or 0)
        if best_candidate_person_id in anchor_person_ids:
            return best_candidate_person_id
        if str(decision.get("mode")) == "attach":
            attached_person_id = int(decision.get("person_id") or 0)
            if attached_person_id in anchor_person_ids:
                return attached_person_id
        return 0

    def _summarize_anchor_outcomes(
        self,
        *,
        face_observation_ids: list[int],
        anchor_candidate_person_by_face: dict[int, int],
        conn: sqlite3.Connection,
    ) -> tuple[int, dict[int, int]]:
        if not anchor_candidate_person_by_face:
            return 0, {}
        assigned_person_by_face = self._load_active_person_ids_for_faces(
            face_observation_ids=face_observation_ids,
            conn=conn,
        )
        anchor_attached_face_count = 0
        anchor_missed_by_person: dict[int, int] = defaultdict(int)
        for face_id, anchor_person_id in anchor_candidate_person_by_face.items():
            if int(assigned_person_by_face.get(int(face_id), 0)) == int(anchor_person_id):
                anchor_attached_face_count += 1
                continue
            anchor_missed_by_person[int(anchor_person_id)] += 1
        return anchor_attached_face_count, dict(anchor_missed_by_person)

    def _merge_anchor_missed_by_person(
        self,
        *,
        anchor_missed_by_person: dict[int, int],
        delta: dict[int, int],
    ) -> None:
        for person_id, missed_count in delta.items():
            anchor_missed_by_person[int(person_id)] = anchor_missed_by_person.get(int(person_id), 0) + int(
                missed_count
            )

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

    def _load_pending_reassign_face_ids(
        self,
        face_observation_ids: list[int],
        *,
        conn: sqlite3.Connection,
    ) -> set[int]:
        unique_ids = sorted({int(face_id) for face_id in face_observation_ids if int(face_id) > 0})
        if not unique_ids:
            return set()
        placeholders = ", ".join("?" for _ in unique_ids)
        rows = conn.execute(
            f"""
            SELECT id
            FROM face_observation
            WHERE pending_reassign=1
              AND id IN ({placeholders})
            """,
            tuple(unique_ids),
        ).fetchall()
        return {int(row[0]) for row in rows}

    def _load_active_person_ids_for_faces(
        self,
        *,
        face_observation_ids: list[int],
        conn: sqlite3.Connection,
    ) -> dict[int, int]:
        unique_ids = sorted({int(face_id) for face_id in face_observation_ids if int(face_id) > 0})
        if not unique_ids:
            return {}
        placeholders = ", ".join("?" for _ in unique_ids)
        rows = conn.execute(
            f"""
            SELECT face_observation_id, person_id
            FROM person_face_assignment
            WHERE active=1
              AND face_observation_id IN ({placeholders})
            """,
            tuple(unique_ids),
        ).fetchall()
        return {int(row[0]): int(row[1]) for row in rows}

    def _load_anchor_person_ids(
        self,
        *,
        conn: sqlite3.Connection,
        limit: int = 10,
    ) -> set[int]:
        rows = conn.execute(
            """
            SELECT a.person_id
            FROM person_face_assignment AS a
            INNER JOIN person AS person ON person.id = a.person_id
            INNER JOIN face_observation AS f ON f.id = a.face_observation_id
            INNER JOIN photo_asset AS p ON p.id = f.photo_asset_id
            WHERE a.active=1
              AND person.status='active'
              AND f.active=1
              AND p.asset_status='active'
            GROUP BY a.person_id
            ORDER BY COUNT(*) DESC, a.person_id ASC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return {int(row[0]) for row in rows}

    def _load_active_excluded_person_ids(
        self,
        *,
        face_observation_ids: list[int],
        conn: sqlite3.Connection | None,
    ) -> dict[int, set[int]]:
        unique_ids = sorted({int(face_id) for face_id in face_observation_ids if int(face_id) > 0})
        if not unique_ids:
            return {}
        placeholders = ", ".join("?" for _ in unique_ids)
        managed_conn = conn is None
        db = conn or connect_sqlite(self._library_db_path)
        try:
            rows = db.execute(
                f"""
                SELECT face_observation_id, person_id
                FROM person_face_exclusion
                WHERE active=1
                  AND face_observation_id IN ({placeholders})
                ORDER BY face_observation_id ASC, person_id ASC
                """,
                tuple(unique_ids),
            ).fetchall()
        finally:
            if managed_conn:
                db.close()
        excluded_person_ids_by_face: dict[int, set[int]] = {}
        for face_observation_id, person_id in rows:
            excluded_person_ids_by_face.setdefault(int(face_observation_id), set()).add(int(person_id))
        return excluded_person_ids_by_face

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
