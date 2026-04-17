from __future__ import annotations

import json
from typing import Any

import numpy as np

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]


class IdentityClusterRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get_cluster_profile_required(self, cluster_profile_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT *
            FROM identity_cluster_profile
            WHERE id = ?
            """,
            (int(cluster_profile_id),),
        ).fetchone()
        if row is None:
            raise ValueError(f"cluster profile 不存在: {int(cluster_profile_id)}")
        return dict(row)

    def get_snapshot_required(self, snapshot_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT *
            FROM identity_observation_snapshot
            WHERE id = ?
              AND status = 'succeeded'
            """,
            (int(snapshot_id),),
        ).fetchone()
        if row is None:
            raise ValueError(f"observation snapshot 不存在或未完成: {int(snapshot_id)}")
        return dict(row)

    def get_latest_snapshot_id(self) -> int | None:
        row = self.conn.execute(
            """
            SELECT id
            FROM identity_observation_snapshot
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return int(row["id"])

    def list_snapshot_pool_rows(self, *, snapshot_id: int, pool_kind: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT pe.observation_id,
                   fo.photo_asset_id,
                   COALESCE(pe.quality_score_snapshot, fo.quality_score, 0.0) AS quality_score,
                   fe.vector_blob
            FROM identity_observation_pool_entry AS pe
            JOIN face_observation AS fo ON fo.id = pe.observation_id
            JOIN face_embedding AS fe
              ON fe.face_observation_id = pe.observation_id
             AND fe.feature_type = 'face'
             AND fe.model_key = 'insightface'
             AND fe.normalized = 1
            WHERE pe.snapshot_id = ?
              AND pe.pool_kind = ?
            ORDER BY pe.observation_id ASC
            """,
            (int(snapshot_id), str(pool_kind)),
        ).fetchall()

        result: list[dict[str, Any]] = []
        for row in rows:
            vector_blob = row["vector_blob"]
            if not isinstance(vector_blob, (bytes, bytearray, memoryview)):
                continue
            vector = np.frombuffer(vector_blob, dtype=np.float32).copy()
            if vector.ndim != 1 or int(vector.size) <= 0:
                continue
            result.append(
                {
                    "observation_id": int(row["observation_id"]),
                    "photo_asset_id": int(row["photo_asset_id"]),
                    "quality_score": float(row["quality_score"]),
                    "vector": vector,
                    "pool_kind": str(pool_kind),
                }
            )
        return result

    def insert_cluster(
        self,
        *,
        run_id: int,
        cluster_stage: str,
        cluster_state: str,
        member_count: int,
        retained_member_count: int,
        anchor_core_count: int,
        core_count: int,
        boundary_count: int,
        attachment_count: int,
        excluded_count: int,
        distinct_photo_count: int,
        compactness_p50: float | None,
        compactness_p90: float | None,
        support_ratio_p10: float | None,
        support_ratio_p50: float | None,
        intra_photo_conflict_ratio: float | None,
        nearest_cluster_distance: float | None,
        separation_gap: float | None,
        boundary_ratio: float | None,
        discard_reason_code: str | None,
        representative_observation_id: int | None,
        summary_json: dict[str, Any],
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO identity_cluster(
                run_id,
                cluster_stage,
                cluster_state,
                member_count,
                retained_member_count,
                anchor_core_count,
                core_count,
                boundary_count,
                attachment_count,
                excluded_count,
                distinct_photo_count,
                compactness_p50,
                compactness_p90,
                support_ratio_p10,
                support_ratio_p50,
                intra_photo_conflict_ratio,
                nearest_cluster_distance,
                separation_gap,
                boundary_ratio,
                discard_reason_code,
                representative_observation_id,
                summary_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(run_id),
                str(cluster_stage),
                str(cluster_state),
                int(member_count),
                int(retained_member_count),
                int(anchor_core_count),
                int(core_count),
                int(boundary_count),
                int(attachment_count),
                int(excluded_count),
                int(distinct_photo_count),
                float(compactness_p50) if compactness_p50 is not None else None,
                float(compactness_p90) if compactness_p90 is not None else None,
                float(support_ratio_p10) if support_ratio_p10 is not None else None,
                float(support_ratio_p50) if support_ratio_p50 is not None else None,
                float(intra_photo_conflict_ratio) if intra_photo_conflict_ratio is not None else None,
                float(nearest_cluster_distance) if nearest_cluster_distance is not None else None,
                float(separation_gap) if separation_gap is not None else None,
                float(boundary_ratio) if boundary_ratio is not None else None,
                str(discard_reason_code) if discard_reason_code is not None else None,
                int(representative_observation_id) if representative_observation_id is not None else None,
                json.dumps(summary_json or {}, ensure_ascii=False, sort_keys=True),
            ),
        )
        return int(cursor.lastrowid)

    def insert_cluster_member(
        self,
        *,
        cluster_id: int,
        observation_id: int,
        source_pool_kind: str,
        quality_score_snapshot: float | None,
        member_role: str,
        decision_status: str,
        distance_to_medoid: float | None,
        density_radius: float | None,
        support_ratio: float | None,
        attachment_support_ratio: float | None,
        nearest_competing_cluster_distance: float | None,
        separation_gap: float | None,
        decision_reason_code: str | None,
        is_trusted_seed_candidate: bool,
        is_selected_trusted_seed: bool,
        seed_rank: int | None,
        is_representative: bool,
        diagnostic_json: dict[str, Any],
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO identity_cluster_member(
                cluster_id,
                observation_id,
                source_pool_kind,
                quality_score_snapshot,
                member_role,
                decision_status,
                distance_to_medoid,
                density_radius,
                support_ratio,
                attachment_support_ratio,
                nearest_competing_cluster_distance,
                separation_gap,
                decision_reason_code,
                is_trusted_seed_candidate,
                is_selected_trusted_seed,
                seed_rank,
                is_representative,
                diagnostic_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(cluster_id),
                int(observation_id),
                str(source_pool_kind),
                float(quality_score_snapshot) if quality_score_snapshot is not None else None,
                str(member_role),
                str(decision_status),
                float(distance_to_medoid) if distance_to_medoid is not None else None,
                float(density_radius) if density_radius is not None else None,
                float(support_ratio) if support_ratio is not None else None,
                float(attachment_support_ratio) if attachment_support_ratio is not None else None,
                float(nearest_competing_cluster_distance) if nearest_competing_cluster_distance is not None else None,
                float(separation_gap) if separation_gap is not None else None,
                str(decision_reason_code) if decision_reason_code is not None else None,
                1 if bool(is_trusted_seed_candidate) else 0,
                1 if bool(is_selected_trusted_seed) else 0,
                int(seed_rank) if seed_rank is not None else None,
                1 if bool(is_representative) else 0,
                json.dumps(diagnostic_json or {}, ensure_ascii=False, sort_keys=True),
            ),
        )
        return int(cursor.lastrowid)

    def insert_cluster_lineage(
        self,
        *,
        parent_cluster_id: int,
        child_cluster_id: int,
        relation_kind: str,
        reason_code: str | None,
        detail_json: dict[str, Any],
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO identity_cluster_lineage(
                parent_cluster_id,
                child_cluster_id,
                relation_kind,
                reason_code,
                detail_json
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                int(parent_cluster_id),
                int(child_cluster_id),
                str(relation_kind),
                str(reason_code) if reason_code is not None else None,
                json.dumps(detail_json or {}, ensure_ascii=False, sort_keys=True),
            ),
        )
        return int(cursor.lastrowid)

    def insert_cluster_resolution(
        self,
        *,
        cluster_id: int,
        resolution_state: str,
        resolution_reason: str | None,
        source_run_id: int,
        trusted_seed_count: int,
        trusted_seed_candidate_count: int,
        trusted_seed_reject_distribution_json: dict[str, Any],
        detail_json: dict[str, Any],
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO identity_cluster_resolution(
                cluster_id,
                resolution_state,
                resolution_reason,
                source_run_id,
                trusted_seed_count,
                trusted_seed_candidate_count,
                trusted_seed_reject_distribution_json,
                detail_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(cluster_id),
                str(resolution_state),
                str(resolution_reason) if resolution_reason is not None else None,
                int(source_run_id),
                int(trusted_seed_count),
                int(trusted_seed_candidate_count),
                json.dumps(trusted_seed_reject_distribution_json or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(detail_json or {}, ensure_ascii=False, sort_keys=True),
            ),
        )
        return int(cursor.lastrowid)

    def get_run_persistence_counts(self, *, run_id: int) -> dict[str, int]:
        cluster_row = self.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM identity_cluster
            WHERE run_id = ?
            """,
            (int(run_id),),
        ).fetchone()
        member_row = self.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM identity_cluster_member AS m
            JOIN identity_cluster AS c ON c.id = m.cluster_id
            WHERE c.run_id = ?
            """,
            (int(run_id),),
        ).fetchone()
        resolution_row = self.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM identity_cluster_resolution AS r
            JOIN identity_cluster AS c ON c.id = r.cluster_id
            WHERE c.run_id = ?
              AND c.cluster_stage = 'final'
            """,
            (int(run_id),),
        ).fetchone()
        return {
            "cluster_count": int(cluster_row["c"]),
            "member_count": int(member_row["c"]),
            "resolution_count": int(resolution_row["c"]),
        }
