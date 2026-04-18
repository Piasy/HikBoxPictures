from __future__ import annotations

from pathlib import Path
from typing import Iterable

import numpy as np

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.workspace import load_workspace_paths

from .models import (
    AssignParameters,
    BaseRunContext,
    ClusterMemberRecord,
    ClusterRecord,
    ObservationCandidateRecord,
    QueryContext,
    SnapshotContext,
)


class IdentityV31QueryService:
    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)

    def load_report_context(
        self,
        *,
        base_run_id: int | None,
        assign_parameters: AssignParameters,
    ) -> QueryContext:
        assign_parameters = assign_parameters.validate()
        workspace_paths = load_workspace_paths(self.workspace)

        conn = connect_db(workspace_paths.db_path)
        try:
            base_run = self._resolve_base_run(conn=conn, base_run_id=base_run_id)
            snapshot = self._load_snapshot(conn=conn, snapshot_id=base_run.observation_snapshot_id)
            clusters = self._load_clusters(
                conn=conn,
                run_id=base_run.id,
                embedding_model_key=snapshot.embedding_model_key,
            )
            clusters_by_id = {item.cluster_id: item for item in clusters}
            non_rejected_member_observation_ids_by_cluster = {
                item.cluster_id: {member.observation_id for member in item.members if member.decision_status != "rejected"}
                for item in clusters
            }
            source_candidates = self._load_source_candidates(
                conn=conn,
                run_id=base_run.id,
                snapshot_id=snapshot.id,
                embedding_model_key=snapshot.embedding_model_key,
            )
            source_candidate_observation_ids = {
                "review_pending_retained": {item.observation_id for item in source_candidates["review_pending_retained"]},
                "attachment": {item.observation_id for item in source_candidates["attachment"]},
            }
            source_candidate_observation_ids["all"] = (
                source_candidate_observation_ids["review_pending_retained"]
                | source_candidate_observation_ids["attachment"]
            )
            candidate_observations = self._select_candidates(
                source_candidates=source_candidates,
                assign_source=assign_parameters.assign_source,
            )
            return QueryContext(
                base_run=base_run,
                snapshot=snapshot,
                clusters=clusters,
                clusters_by_id=clusters_by_id,
                candidate_observations=candidate_observations,
                non_rejected_member_observation_ids_by_cluster=non_rejected_member_observation_ids_by_cluster,
                source_candidate_observation_ids=source_candidate_observation_ids,
                warnings=[],
            )
        finally:
            conn.close()

    def _resolve_base_run(self, *, conn, base_run_id: int | None) -> BaseRunContext:
        if base_run_id is None:
            row = conn.execute(
                """
                SELECT id, run_status, observation_snapshot_id, cluster_profile_id, is_review_target
                FROM identity_cluster_run
                WHERE is_review_target = 1
                  AND run_status = 'succeeded'
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                raise ValueError("默认 review target run 不存在")
            return self._to_base_run_context(row)

        row = conn.execute(
            """
            SELECT id, run_status, observation_snapshot_id, cluster_profile_id, is_review_target
            FROM identity_cluster_run
            WHERE id = ?
            LIMIT 1
            """,
            (int(base_run_id),),
        ).fetchone()
        if row is None:
            raise ValueError("cluster run 不存在")
        if str(row["run_status"]) != "succeeded":
            raise ValueError("run_status 必须为 succeeded")
        return self._to_base_run_context(row)

    def _load_snapshot(self, *, conn, snapshot_id: int) -> SnapshotContext:
        row = conn.execute(
            """
            SELECT s.id,
                   s.observation_profile_id,
                   p.embedding_model_key
            FROM identity_observation_snapshot AS s
            JOIN identity_observation_profile AS p
              ON p.id = s.observation_profile_id
            WHERE s.id = ?
            LIMIT 1
            """,
            (int(snapshot_id),),
        ).fetchone()
        if row is None:
            raise ValueError("snapshot 不存在")
        return SnapshotContext(
            id=int(row["id"]),
            observation_profile_id=int(row["observation_profile_id"]),
            embedding_model_key=str(row["embedding_model_key"]),
        )

    def _load_clusters(self, *, conn, run_id: int, embedding_model_key: str) -> list[ClusterRecord]:
        cluster_rows = conn.execute(
            """
            SELECT c.id AS cluster_id,
                   c.cluster_stage,
                   c.cluster_state,
                   c.representative_observation_id,
                   c.retained_member_count,
                   c.distinct_photo_count,
                   r.resolution_state
            FROM identity_cluster AS c
            JOIN identity_cluster_resolution AS r
              ON r.cluster_id = c.id
            WHERE c.run_id = ?
              AND c.cluster_stage = 'final'
              AND c.cluster_state = 'active'
              AND r.resolution_state IN ('materialized', 'review_pending')
            ORDER BY CASE r.resolution_state
                       WHEN 'materialized' THEN 0
                       WHEN 'review_pending' THEN 1
                       ELSE 9
                     END ASC,
                     c.retained_member_count DESC,
                     c.id ASC
            """,
            (int(run_id),),
        ).fetchall()
        if not cluster_rows:
            return []

        cluster_ids = [int(row["cluster_id"]) for row in cluster_rows]
        members_by_cluster = self._load_members_by_cluster(
            conn=conn,
            cluster_ids=cluster_ids,
            embedding_model_key=embedding_model_key,
        )
        clusters: list[ClusterRecord] = []
        for row in cluster_rows:
            cluster_id = int(row["cluster_id"])
            members = members_by_cluster.get(cluster_id, [])
            representative_count = sum(1 for item in members if item.is_representative)
            retained_count = sum(1 for item in members if item.decision_status == "retained")
            excluded_count = sum(1 for item in members if item.decision_status == "rejected")
            clusters.append(
                ClusterRecord(
                    cluster_id=cluster_id,
                    cluster_stage=str(row["cluster_stage"]),
                    cluster_state=str(row["cluster_state"]),
                    resolution_state=str(row["resolution_state"]),
                    representative_observation_id=(
                        int(row["representative_observation_id"])
                        if row["representative_observation_id"] is not None
                        else None
                    ),
                    retained_member_count=int(row["retained_member_count"]),
                    distinct_photo_count=int(row["distinct_photo_count"]),
                    representative_count=representative_count,
                    retained_count=retained_count,
                    excluded_count=excluded_count,
                    members=members,
                )
            )
        return clusters

    def _load_members_by_cluster(
        self,
        *,
        conn,
        cluster_ids: Iterable[int],
        embedding_model_key: str,
    ) -> dict[int, list[ClusterMemberRecord]]:
        cluster_ids_tuple = tuple(int(item) for item in cluster_ids)
        if not cluster_ids_tuple:
            return {}
        placeholders = ",".join(["?"] * len(cluster_ids_tuple))
        rows = conn.execute(
            f"""
            SELECT m.cluster_id,
                   m.observation_id,
                   o.photo_asset_id AS photo_id,
                   m.source_pool_kind,
                   m.member_role,
                   m.decision_status,
                   m.is_selected_trusted_seed,
                   m.is_representative,
                   m.quality_score_snapshot,
                   p.primary_path,
                   fe.model_key AS embedding_model_key,
                   fe.dimension AS embedding_dim,
                   fe.vector_blob
            FROM identity_cluster_member AS m
            JOIN face_observation AS o
              ON o.id = m.observation_id
            JOIN photo_asset AS p
              ON p.id = o.photo_asset_id
            LEFT JOIN face_embedding AS fe
              ON fe.id = (
                  SELECT fe2.id
                  FROM face_embedding AS fe2
                  WHERE fe2.face_observation_id = m.observation_id
                    AND fe2.feature_type = 'face'
                    AND fe2.model_key = ?
                    AND fe2.normalized = 1
                  ORDER BY fe2.id ASC
                  LIMIT 1
              )
            WHERE m.cluster_id IN ({placeholders})
            ORDER BY m.cluster_id ASC, m.id ASC
            """,
            (str(embedding_model_key), *cluster_ids_tuple),
        ).fetchall()
        grouped: dict[int, list[ClusterMemberRecord]] = {}
        for row in rows:
            cluster_id = int(row["cluster_id"])
            grouped.setdefault(cluster_id, []).append(
                ClusterMemberRecord(
                    cluster_id=cluster_id,
                    observation_id=int(row["observation_id"]),
                    photo_id=int(row["photo_id"]),
                    source_pool_kind=str(row["source_pool_kind"]),
                    member_role=str(row["member_role"]),
                    decision_status=str(row["decision_status"]),
                    is_selected_trusted_seed=bool(int(row["is_selected_trusted_seed"])),
                    is_representative=bool(int(row["is_representative"])),
                    quality_score_snapshot=(
                        float(row["quality_score_snapshot"]) if row["quality_score_snapshot"] is not None else None
                    ),
                    primary_path=str(row["primary_path"]) if row["primary_path"] is not None else None,
                    embedding_vector=self._decode_embedding_vector(
                        vector_blob=row["vector_blob"],
                        dimension=row["embedding_dim"],
                    ),
                    embedding_dim=int(row["embedding_dim"]) if row["embedding_dim"] is not None else None,
                )
            )
        return grouped

    def _load_source_candidates(
        self,
        *,
        conn,
        run_id: int,
        snapshot_id: int,
        embedding_model_key: str,
    ) -> dict[str, list[ObservationCandidateRecord]]:
        review_pending_rows = conn.execute(
            """
            SELECT m.observation_id,
                   o.photo_asset_id AS photo_id,
                   c.id AS source_cluster_id,
                   p.primary_path,
                   fe.model_key AS embedding_model_key,
                   fe.dimension AS embedding_dim,
                   fe.vector_blob
            FROM identity_cluster AS c
            JOIN identity_cluster_resolution AS r
              ON r.cluster_id = c.id
            JOIN identity_cluster_member AS m
              ON m.cluster_id = c.id
            JOIN face_observation AS o
              ON o.id = m.observation_id
            JOIN photo_asset AS p
              ON p.id = o.photo_asset_id
            LEFT JOIN face_embedding AS fe
              ON fe.id = (
                  SELECT fe2.id
                  FROM face_embedding AS fe2
                  WHERE fe2.face_observation_id = m.observation_id
                    AND fe2.feature_type = 'face'
                    AND fe2.model_key = ?
                    AND fe2.normalized = 1
                  ORDER BY fe2.id ASC
                  LIMIT 1
              )
            WHERE c.run_id = ?
              AND c.cluster_stage = 'final'
              AND c.cluster_state = 'active'
              AND r.resolution_state = 'review_pending'
              AND m.decision_status = 'retained'
            ORDER BY m.observation_id ASC, c.id ASC, m.id ASC
            """,
            (str(embedding_model_key), int(run_id)),
        ).fetchall()
        attachment_rows = conn.execute(
            """
            SELECT pe.observation_id,
                   o.photo_asset_id AS photo_id,
                   NULL AS source_cluster_id,
                   p.primary_path,
                   fe.model_key AS embedding_model_key,
                   fe.dimension AS embedding_dim,
                   fe.vector_blob
            FROM identity_observation_pool_entry AS pe
            JOIN face_observation AS o
              ON o.id = pe.observation_id
            JOIN photo_asset AS p
              ON p.id = o.photo_asset_id
            LEFT JOIN face_embedding AS fe
              ON fe.id = (
                  SELECT fe2.id
                  FROM face_embedding AS fe2
                  WHERE fe2.face_observation_id = pe.observation_id
                    AND fe2.feature_type = 'face'
                    AND fe2.model_key = ?
                    AND fe2.normalized = 1
                  ORDER BY fe2.id ASC
                  LIMIT 1
              )
            WHERE pe.snapshot_id = ?
              AND pe.pool_kind = 'attachment'
            ORDER BY pe.observation_id ASC, pe.id ASC
            """,
            (str(embedding_model_key), int(snapshot_id)),
        ).fetchall()
        review_pending = [
            self._to_candidate_record(row=row, source_kind="review_pending_retained") for row in review_pending_rows
        ]
        attachment = [self._to_candidate_record(row=row, source_kind="attachment") for row in attachment_rows]
        return {
            "review_pending_retained": self._dedupe_by_observation(
                candidates=review_pending,
                source_priority={"review_pending_retained": 0, "attachment": 1},
            ),
            "attachment": self._dedupe_by_observation(
                candidates=attachment,
                source_priority={"review_pending_retained": 0, "attachment": 1},
            ),
        }

    def _select_candidates(
        self,
        *,
        source_candidates: dict[str, list[ObservationCandidateRecord]],
        assign_source: str,
    ) -> list[ObservationCandidateRecord]:
        if assign_source == "review_pending":
            return list(source_candidates["review_pending_retained"])
        if assign_source == "attachment":
            return list(source_candidates["attachment"])
        union = [*source_candidates["review_pending_retained"], *source_candidates["attachment"]]
        return self._dedupe_by_observation(
            candidates=union,
            source_priority={"review_pending_retained": 0, "attachment": 1},
        )

    def _dedupe_by_observation(
        self,
        *,
        candidates: list[ObservationCandidateRecord],
        source_priority: dict[str, int],
    ) -> list[ObservationCandidateRecord]:
        by_observation: dict[int, ObservationCandidateRecord] = {}
        for item in candidates:
            existing = by_observation.get(item.observation_id)
            if existing is None:
                by_observation[item.observation_id] = item
                continue
            existing_priority = source_priority.get(existing.source_kind, 99)
            current_priority = source_priority.get(item.source_kind, 99)
            if current_priority < existing_priority:
                by_observation[item.observation_id] = item
        return [by_observation[key] for key in sorted(by_observation)]

    def _to_base_run_context(self, row) -> BaseRunContext:
        return BaseRunContext(
            id=int(row["id"]),
            run_status=str(row["run_status"]),
            observation_snapshot_id=int(row["observation_snapshot_id"]),
            cluster_profile_id=int(row["cluster_profile_id"]),
            is_review_target=bool(int(row["is_review_target"])),
        )

    def _to_candidate_record(self, *, row, source_kind: str) -> ObservationCandidateRecord:
        return ObservationCandidateRecord(
            observation_id=int(row["observation_id"]),
            photo_id=int(row["photo_id"]),
            source_kind=source_kind,
            source_cluster_id=int(row["source_cluster_id"]) if row["source_cluster_id"] is not None else None,
            primary_path=str(row["primary_path"]) if row["primary_path"] is not None else None,
            embedding_vector=self._decode_embedding_vector(
                vector_blob=row["vector_blob"],
                dimension=row["embedding_dim"],
            ),
            embedding_dim=int(row["embedding_dim"]) if row["embedding_dim"] is not None else None,
            embedding_model_key=str(row["embedding_model_key"]) if row["embedding_model_key"] is not None else None,
        )

    def _decode_embedding_vector(self, *, vector_blob, dimension) -> list[float] | None:
        if not isinstance(vector_blob, (bytes, bytearray, memoryview)):
            return None
        if dimension is None:
            return None
        dim = int(dimension)
        if dim <= 0:
            return None
        vector = np.frombuffer(vector_blob, dtype=np.float32, count=dim).copy()
        if vector.size <= 0:
            return None
        return vector.astype(np.float32).tolist()
