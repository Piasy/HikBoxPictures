from __future__ import annotations

import json
from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]


class IdentityReviewNotFoundError(LookupError):
    pass


class IdentityReviewIntegrityError(ValueError):
    pass


class IdentityReviewQueryService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get_identity_tuning_payload(self, *, run_id: int | None = None) -> dict[str, Any]:
        review_run = self._get_review_run(run_id=run_id)
        snapshot = self._get_snapshot_required(snapshot_id=int(review_run["observation_snapshot_id"]))
        observation_profile = self._get_observation_profile_required(
            profile_id=int(snapshot["observation_profile_id"])
        )
        cluster_profile = self._get_cluster_profile_required(profile_id=int(review_run["cluster_profile_id"]))
        clusters = self._list_run_clusters(run_id=int(review_run["id"]))
        run_summary = self._build_run_summary(
            run_id=int(review_run["id"]),
            run_summary_raw=self._load_json(review_run.get("summary_json")),
            snapshot_summary=self._load_json(snapshot.get("summary_json")),
            clusters=clusters,
        )

        return {
            "review_run": review_run,
            "observation_snapshot": snapshot,
            "observation_profile": observation_profile,
            "cluster_profile": cluster_profile,
            "run_summary": run_summary,
            "clusters": clusters,
        }

    def _get_review_run(self, *, run_id: int | None) -> dict[str, Any]:
        if run_id is not None:
            row = self.conn.execute(
                """
                SELECT *
                FROM identity_cluster_run
                WHERE id = ?
                LIMIT 1
                """,
                (int(run_id),),
            ).fetchone()
            if row is None:
                raise IdentityReviewNotFoundError(f"run 不存在: {int(run_id)}")
            return self._serialize_run(dict(row))

        row = self.conn.execute(
            """
            SELECT *
            FROM identity_cluster_run
            WHERE is_review_target = 1
            ORDER BY review_selected_at DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            raise IdentityReviewIntegrityError("完整性错误：缺少 review target run")
        return self._serialize_run(dict(row))

    def _get_snapshot_required(self, *, snapshot_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT *
            FROM identity_observation_snapshot
            WHERE id = ?
            LIMIT 1
            """,
            (int(snapshot_id),),
        ).fetchone()
        if row is None:
            raise IdentityReviewIntegrityError(f"完整性错误：run 绑定的 observation snapshot 不存在: {int(snapshot_id)}")
        payload = dict(row)
        payload["summary_json"] = self._load_json(payload.get("summary_json"))
        return payload

    def _get_observation_profile_required(self, *, profile_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT *
            FROM identity_observation_profile
            WHERE id = ?
            LIMIT 1
            """,
            (int(profile_id),),
        ).fetchone()
        if row is None:
            raise IdentityReviewIntegrityError(f"完整性错误：observation profile 不存在: {int(profile_id)}")
        return dict(row)

    def _get_cluster_profile_required(self, *, profile_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT *
            FROM identity_cluster_profile
            WHERE id = ?
            LIMIT 1
            """,
            (int(profile_id),),
        ).fetchone()
        if row is None:
            raise IdentityReviewIntegrityError(f"完整性错误：cluster profile 不存在: {int(profile_id)}")
        return dict(row)

    def _list_run_clusters(self, *, run_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM identity_cluster
            WHERE run_id = ?
              AND cluster_stage = 'final'
            ORDER BY id ASC
            """,
            (int(run_id),),
        ).fetchall()

        return [self._build_cluster_payload(cluster_row=dict(row)) for row in rows]

    def _build_cluster_payload(self, *, cluster_row: dict[str, Any]) -> dict[str, Any]:
        cluster_id = int(cluster_row["id"])
        resolution_row = self.conn.execute(
            """
            SELECT *
            FROM identity_cluster_resolution
            WHERE cluster_id = ?
            LIMIT 1
            """,
            (cluster_id,),
        ).fetchone()

        return {
            "cluster_id": cluster_id,
            "cluster_stage": str(cluster_row["cluster_stage"]),
            "cluster_state": str(cluster_row["cluster_state"]),
            "representative_observation_id": (
                int(cluster_row["representative_observation_id"])
                if cluster_row["representative_observation_id"] is not None
                else None
            ),
            "representative_crop_url": (
                f"/api/observations/{int(cluster_row['representative_observation_id'])}/crop"
                if cluster_row["representative_observation_id"] is not None
                else None
            ),
            "metrics": {
                "member_count": int(cluster_row["member_count"]),
                "retained_member_count": int(cluster_row["retained_member_count"]),
                "anchor_core_count": int(cluster_row["anchor_core_count"]),
                "core_count": int(cluster_row["core_count"]),
                "boundary_count": int(cluster_row["boundary_count"]),
                "attachment_count": int(cluster_row["attachment_count"]),
                "excluded_count": int(cluster_row["excluded_count"]),
                "distinct_photo_count": int(cluster_row["distinct_photo_count"]),
                "compactness_p50": self._float_or_none(cluster_row["compactness_p50"]),
                "compactness_p90": self._float_or_none(cluster_row["compactness_p90"]),
                "support_ratio_p10": self._float_or_none(cluster_row["support_ratio_p10"]),
                "support_ratio_p50": self._float_or_none(cluster_row["support_ratio_p50"]),
                "intra_photo_conflict_ratio": self._float_or_none(cluster_row["intra_photo_conflict_ratio"]),
                "nearest_cluster_distance": self._float_or_none(cluster_row["nearest_cluster_distance"]),
                "separation_gap": self._float_or_none(cluster_row["separation_gap"]),
                "boundary_ratio": self._float_or_none(cluster_row["boundary_ratio"]),
                "discard_reason_code": cluster_row["discard_reason_code"],
            },
            "seed_audit": self._build_seed_audit(resolution_row=resolution_row),
            "resolution": self._build_resolution_payload(resolution_row=resolution_row),
            "lineage": self._list_cluster_lineage(cluster_id=cluster_id),
            "members": self._group_cluster_members(cluster_id=cluster_id),
        }

    def _build_seed_audit(self, *, resolution_row: sqlite3.Row | None) -> dict[str, Any]:
        if resolution_row is None:
            return {
                "trusted_seed_count": 0,
                "trusted_seed_candidate_count": 0,
                "trusted_seed_reject_distribution": {},
            }
        return {
            "trusted_seed_count": int(resolution_row["trusted_seed_count"]),
            "trusted_seed_candidate_count": int(resolution_row["trusted_seed_candidate_count"]),
            "trusted_seed_reject_distribution": self._load_json(
                resolution_row["trusted_seed_reject_distribution_json"]
            ),
        }

    def _build_resolution_payload(self, *, resolution_row: sqlite3.Row | None) -> dict[str, Any]:
        if resolution_row is None:
            return {
                "resolution_state": "missing",
                "resolution_reason": "missing_resolution_row",
                "publish_state": None,
                "publish_failure_reason": None,
                "person_id": None,
                "prototype_status": None,
                "ann_status": None,
            }
        return {
            "resolution_state": str(resolution_row["resolution_state"]),
            "resolution_reason": resolution_row["resolution_reason"],
            "publish_state": resolution_row["publish_state"],
            "publish_failure_reason": resolution_row["publish_failure_reason"],
            "person_id": int(resolution_row["person_id"]) if resolution_row["person_id"] is not None else None,
            "prototype_status": resolution_row["prototype_status"],
            "ann_status": resolution_row["ann_status"],
        }

    def _list_cluster_lineage(self, *, cluster_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT l.id,
                   l.parent_cluster_id,
                   l.child_cluster_id,
                   p.cluster_stage AS parent_cluster_stage,
                   c.cluster_stage AS child_cluster_stage,
                   l.relation_kind,
                   l.reason_code,
                   l.detail_json
            FROM identity_cluster_lineage AS l
            JOIN identity_cluster AS p ON p.id = l.parent_cluster_id
            JOIN identity_cluster AS c ON c.id = l.child_cluster_id
            WHERE l.parent_cluster_id = ?
               OR l.child_cluster_id = ?
            ORDER BY l.id ASC
            """,
            (cluster_id, cluster_id),
        ).fetchall()

        payload: list[dict[str, Any]] = []
        for row in rows:
            parent_id = int(row["parent_cluster_id"])
            child_id = int(row["child_cluster_id"])
            if cluster_id == parent_id:
                direction = "out"
                related_cluster_id = child_id
            else:
                direction = "in"
                related_cluster_id = parent_id
            payload.append(
                {
                    "lineage_id": int(row["id"]),
                    "direction": direction,
                    "related_cluster_id": related_cluster_id,
                    "parent_cluster_id": parent_id,
                    "child_cluster_id": child_id,
                    "parent_cluster_stage": str(row["parent_cluster_stage"]),
                    "child_cluster_stage": str(row["child_cluster_stage"]),
                    "relation_kind": str(row["relation_kind"]),
                    "reason_code": row["reason_code"],
                    "detail": self._load_json(row["detail_json"]),
                }
            )
        return payload

    def _group_cluster_members(self, *, cluster_id: int) -> dict[str, Any]:
        members = self._list_cluster_members(cluster_id=cluster_id)
        representative = [item for item in members if bool(item["is_representative"])]
        retained = [item for item in members if str(item["decision_status"]) != "rejected"]
        excluded = [item for item in members if str(item["decision_status"]) == "rejected"]
        excluded_reason_distribution: dict[str, int] = {}
        for item in excluded:
            reason = str(item.get("decision_reason_code") or "unknown")
            excluded_reason_distribution[reason] = excluded_reason_distribution.get(reason, 0) + 1
        return {
            "representative": representative,
            "retained": retained,
            "excluded": excluded,
            "excluded_reason_distribution": excluded_reason_distribution,
        }

    def _list_cluster_members(self, *, cluster_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT m.*, fo.photo_asset_id
            FROM identity_cluster_member AS m
            JOIN face_observation AS fo ON fo.id = m.observation_id
            WHERE m.cluster_id = ?
            ORDER BY m.id ASC
            """,
            (cluster_id,),
        ).fetchall()

        payload: list[dict[str, Any]] = []
        for row in rows:
            observation_id = int(row["observation_id"])
            photo_asset_id = int(row["photo_asset_id"])
            payload.append(
                {
                    "member_id": int(row["id"]),
                    "observation_id": observation_id,
                    "photo_asset_id": photo_asset_id,
                    "source_pool_kind": str(row["source_pool_kind"]),
                    "member_role": str(row["member_role"]),
                    "decision_status": str(row["decision_status"]),
                    "decision_reason_code": row["decision_reason_code"],
                    "is_trusted_seed_candidate": bool(row["is_trusted_seed_candidate"]),
                    "is_selected_trusted_seed": bool(row["is_selected_trusted_seed"]),
                    "is_representative": bool(row["is_representative"]),
                    "seed_rank": int(row["seed_rank"]) if row["seed_rank"] is not None else None,
                    "quality_score_snapshot": self._float_or_none(row["quality_score_snapshot"]),
                    "support_ratio": self._float_or_none(row["support_ratio"]),
                    "attachment_support_ratio": self._float_or_none(row["attachment_support_ratio"]),
                    "distance_to_medoid": self._float_or_none(row["distance_to_medoid"]),
                    "nearest_competing_cluster_distance": self._float_or_none(
                        row["nearest_competing_cluster_distance"]
                    ),
                    "separation_gap": self._float_or_none(row["separation_gap"]),
                    "diagnostic": self._load_json(row["diagnostic_json"]),
                    "crop_url": f"/api/observations/{observation_id}/crop",
                    "preview_url": f"/api/photos/{photo_asset_id}/preview",
                }
            )
        return payload

    def _build_run_summary(
        self,
        *,
        run_id: int,
        run_summary_raw: dict[str, Any],
        snapshot_summary: dict[str, Any],
        clusters: list[dict[str, Any]],
    ) -> dict[str, Any]:
        cluster_count = int(len(clusters))
        active_cluster_count = int(sum(1 for item in clusters if str(item["cluster_state"]) == "active"))
        discarded_cluster_count = int(sum(1 for item in clusters if str(item["cluster_state"]) == "discarded"))

        observation_total = snapshot_summary.get("observation_total")
        if observation_total is None:
            observation_total = self.conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM identity_observation_pool_entry
                WHERE snapshot_id = (
                    SELECT observation_snapshot_id
                    FROM identity_cluster_run
                    WHERE id = ?
                    LIMIT 1
                )
                """,
                (int(run_id),),
            ).fetchone()
            observation_total = int(observation_total["c"]) if observation_total is not None else 0
        else:
            observation_total = int(observation_total)

        pool_counts_raw = snapshot_summary.get("pool_counts") if isinstance(snapshot_summary, dict) else {}
        pool_counts = {
            "core_discovery": int((pool_counts_raw or {}).get("core_discovery", 0)),
            "attachment": int((pool_counts_raw or {}).get("attachment", 0)),
            "excluded": int((pool_counts_raw or {}).get("excluded", 0)),
        }

        resolution_rows = self.conn.execute(
            """
            SELECT resolution_state, COUNT(*) AS c
            FROM identity_cluster_resolution AS r
            JOIN identity_cluster AS c ON c.id = r.cluster_id
            WHERE c.run_id = ?
              AND c.cluster_stage = 'final'
            GROUP BY resolution_state
            """,
            (int(run_id),),
        ).fetchall()
        resolution_counts = {str(row["resolution_state"]): int(row["c"]) for row in resolution_rows}

        dedup_rows = self.conn.execute(
            """
            SELECT excluded_reason, COUNT(*) AS c
            FROM identity_observation_pool_entry
            WHERE snapshot_id = (
                SELECT observation_snapshot_id
                FROM identity_cluster_run
                WHERE id = ?
                LIMIT 1
            )
              AND pool_kind = 'excluded'
              AND dedup_group_key IS NOT NULL
              AND excluded_reason IS NOT NULL
            GROUP BY excluded_reason
            """,
            (int(run_id),),
        ).fetchall()
        dedup_drop_distribution = {str(row["excluded_reason"]): int(row["c"]) for row in dedup_rows}

        return {
            **run_summary_raw,
            "observation_total": int(observation_total),
            "pool_counts": pool_counts,
            "cluster_count": cluster_count,
            "final_cluster_counts": {
                "total": cluster_count,
                "active": active_cluster_count,
                "discarded": discarded_cluster_count,
            },
            "resolution_counts": resolution_counts,
            "dedup_drop_distribution": dedup_drop_distribution,
        }

    def _serialize_run(self, row: dict[str, Any]) -> dict[str, Any]:
        row["is_review_target"] = bool(row["is_review_target"])
        row["is_materialization_owner"] = bool(row["is_materialization_owner"])
        row["summary_json"] = self._load_json(row.get("summary_json"))
        row["failure_json"] = self._load_json(row.get("failure_json"))
        row["prepared_ann_manifest_json"] = self._load_json(row.get("prepared_ann_manifest_json"))
        return row

    def _load_json(self, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if not isinstance(value, str) or value.strip() == "":
            return {}
        try:
            payload = json.loads(value)
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _float_or_none(self, value: Any) -> float | None:
        if value is None:
            return None
        return float(value)
