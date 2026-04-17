from __future__ import annotations

from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.repositories.identity_cluster_repo import IdentityClusterRepo
from hikbox_pictures.repositories.identity_cluster_run_repo import IdentityClusterRunRepo
from hikbox_pictures.services.identity_cluster_algorithm import IdentityClusterAlgorithm


class IdentityClusterRunService:
    _TERMINAL_STATUSES = {"succeeded", "failed", "cancelled"}

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        cluster_run_repo: IdentityClusterRunRepo,
        cluster_repo: IdentityClusterRepo | None = None,
        algorithm: IdentityClusterAlgorithm | None = None,
    ) -> None:
        self.conn = conn
        self.cluster_run_repo = cluster_run_repo
        self.cluster_repo = cluster_repo or IdentityClusterRepo(conn)
        self.algorithm = algorithm or IdentityClusterAlgorithm(conn, cluster_repo=self.cluster_repo)

    def create_run(
        self,
        *,
        observation_snapshot_id: int,
        cluster_profile_id: int,
        algorithm_version: str,
        supersedes_run_id: int | None,
    ) -> dict[str, int]:
        managed_transaction = not self.conn.in_transaction
        if managed_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            run_id = self.cluster_run_repo.insert_run(
                observation_snapshot_id=observation_snapshot_id,
                cluster_profile_id=cluster_profile_id,
                algorithm_version=algorithm_version,
                run_status="created",
                supersedes_run_id=supersedes_run_id,
            )
            if managed_transaction:
                self.conn.commit()
            return {"run_id": int(run_id)}
        except Exception:
            if managed_transaction and self.conn.in_transaction:
                self.conn.rollback()
            raise

    def mark_run_running(self, *, run_id: int) -> None:
        managed_transaction = not self.conn.in_transaction
        if managed_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            run = self.cluster_run_repo.get_run_required(run_id)
            current = str(run["run_status"])
            self._assert_transition_allowed(current=current, target="running")
            if current != "running":
                updated = self.cluster_run_repo.update_run_status(
                    run_id=run_id,
                    run_status="running",
                    summary_json=self._load_json(run.get("summary_json")),
                    failure_json=self._load_json(run.get("failure_json")),
                    expected_statuses=(current,),
                )
                if not updated:
                    raise ValueError(f"run_status 并发冲突，无法转换为 running: {int(run_id)}")
            if managed_transaction:
                self.conn.commit()
        except Exception:
            if managed_transaction and self.conn.in_transaction:
                self.conn.rollback()
            raise

    def mark_run_succeeded(
        self,
        *,
        run_id: int,
        summary_json: dict[str, Any],
        select_as_review_target: bool,
    ) -> None:
        managed_transaction = not self.conn.in_transaction
        if managed_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            run = self.cluster_run_repo.get_run_required(run_id)
            current = str(run["run_status"])
            self._assert_transition_allowed(current=current, target="succeeded")
            if current != "succeeded":
                updated = self.cluster_run_repo.update_run_status(
                    run_id=run_id,
                    run_status="succeeded",
                    summary_json=summary_json,
                    failure_json={},
                    expected_statuses=(current,),
                )
                if not updated:
                    raise ValueError(f"run_status 并发冲突，无法转换为 succeeded: {int(run_id)}")
            run_row = self.cluster_run_repo.get_run_required(run_id)
            has_existing_target = self.cluster_run_repo.exists_review_target()
            should_select = bool(select_as_review_target) or (not has_existing_target)
            if should_select:
                self.cluster_run_repo.clear_review_target()
                review_selected_at = str(run_row["finished_at"]) if run_row["finished_at"] is not None else None
                self.cluster_run_repo.set_review_target(
                    run_id=run_id,
                    review_selected_at=review_selected_at,
                )
            if managed_transaction:
                self.conn.commit()
        except Exception:
            if managed_transaction and self.conn.in_transaction:
                self.conn.rollback()
            raise

    def mark_run_failed(self, *, run_id: int, reason: str) -> None:
        managed_transaction = not self.conn.in_transaction
        if managed_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            run = self.cluster_run_repo.get_run_required(run_id)
            current = str(run["run_status"])
            self._assert_transition_allowed(current=current, target="failed")
            if current != "failed":
                updated = self.cluster_run_repo.update_run_status(
                    run_id=run_id,
                    run_status="failed",
                    summary_json={},
                    failure_json={"error": str(reason)},
                    expected_statuses=(current,),
                )
                if not updated:
                    raise ValueError(f"run_status 并发冲突，无法转换为 failed: {int(run_id)}")
            if managed_transaction:
                self.conn.commit()
        except Exception:
            if managed_transaction and self.conn.in_transaction:
                self.conn.rollback()
            raise

    def mark_run_cancelled(self, *, run_id: int, reason: str) -> None:
        managed_transaction = not self.conn.in_transaction
        if managed_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            run = self.cluster_run_repo.get_run_required(run_id)
            current = str(run["run_status"])
            self._assert_transition_allowed(current=current, target="cancelled")
            if current != "cancelled":
                updated = self.cluster_run_repo.update_run_status(
                    run_id=run_id,
                    run_status="cancelled",
                    summary_json={},
                    failure_json={"error": str(reason)},
                    expected_statuses=(current,),
                )
                if not updated:
                    raise ValueError(f"run_status 并发冲突，无法转换为 cancelled: {int(run_id)}")
            if managed_transaction:
                self.conn.commit()
        except Exception:
            if managed_transaction and self.conn.in_transaction:
                self.conn.rollback()
            raise

    def select_review_target(self, *, run_id: int) -> None:
        managed_transaction = not self.conn.in_transaction
        if managed_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            target = self.cluster_run_repo.get_run_required(run_id)
            if str(target["run_status"]) != "succeeded":
                raise ValueError(f"只能选择 succeeded run 作为 review target: {int(run_id)}")
            self.cluster_run_repo.clear_review_target()
            self.cluster_run_repo.set_review_target(
                run_id=run_id,
                review_selected_at=None,
            )
            if managed_transaction:
                self.conn.commit()
        except Exception:
            if managed_transaction and self.conn.in_transaction:
                self.conn.rollback()
            raise

    def execute_run(
        self,
        *,
        observation_snapshot_id: int,
        cluster_profile_id: int,
        supersedes_run_id: int | None,
        select_as_review_target: bool,
    ) -> dict[str, Any]:
        algorithm_version = "identity.cluster_run.v3_1"
        self.cluster_repo.get_snapshot_required(int(observation_snapshot_id))
        self.cluster_repo.get_cluster_profile_required(int(cluster_profile_id))
        created = self.create_run(
            observation_snapshot_id=observation_snapshot_id,
            cluster_profile_id=cluster_profile_id,
            algorithm_version=algorithm_version,
            supersedes_run_id=supersedes_run_id,
        )
        run_id = int(created["run_id"])

        self.mark_run_running(run_id=int(run_id))
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            plan = self.algorithm.build_run_plan(
                observation_snapshot_id=int(observation_snapshot_id),
                cluster_profile_id=int(cluster_profile_id),
            )
            counts = self._persist_run_plan(run_id=int(run_id), plan=plan)
            if (
                int(counts["cluster_count"]) <= 0
                or int(counts["member_count"]) <= 0
                or int(counts["resolution_count"]) <= 0
            ):
                raise ValueError("cluster/member/resolution 落库不完整，禁止标记 succeeded")

            summary = {
                **dict(plan.get("run_summary") or {}),
                "cluster_count": int(counts["cluster_count"]),
                "member_count": int(counts["member_count"]),
                "resolution_count": int(counts["resolution_count"]),
            }
            self.mark_run_succeeded(
                run_id=int(run_id),
                summary_json=summary,
                select_as_review_target=bool(select_as_review_target),
            )
            self.conn.commit()
            return {
                "run_id": int(run_id),
                "run_status": "succeeded",
                "summary": summary,
            }
        except Exception as exc:
            if self.conn.in_transaction:
                self.conn.rollback()
            self.mark_run_failed(run_id=int(run_id), reason=str(exc))
            raise

    def _persist_run_plan(self, *, run_id: int, plan: dict[str, Any]) -> dict[str, int]:
        raw_ids: list[int] = []
        cleaned_ids: list[int] = []
        final_ids: list[int] = []

        for raw_cluster in plan.get("raw_clusters") or []:
            raw_members = [int(obs_id) for obs_id in raw_cluster.get("members") or []]
            cluster_id = self.cluster_repo.insert_cluster(
                run_id=int(run_id),
                cluster_stage="raw",
                cluster_state="active",
                member_count=int(len(raw_members)),
                retained_member_count=int(len(raw_members)),
                anchor_core_count=0,
                core_count=int(len(raw_members)),
                boundary_count=0,
                attachment_count=0,
                excluded_count=0,
                distinct_photo_count=int(len(raw_members)),
                compactness_p50=None,
                compactness_p90=None,
                support_ratio_p10=None,
                support_ratio_p50=None,
                intra_photo_conflict_ratio=0.0,
                nearest_cluster_distance=None,
                separation_gap=None,
                boundary_ratio=0.0,
                discard_reason_code=None,
                representative_observation_id=int(raw_members[0]) if raw_members else None,
                summary_json=dict(raw_cluster.get("summary_json") or {}),
            )
            raw_ids.append(int(cluster_id))
            for obs_id in raw_members:
                self.cluster_repo.insert_cluster_member(
                    cluster_id=int(cluster_id),
                    observation_id=int(obs_id),
                    source_pool_kind="core_discovery",
                    quality_score_snapshot=None,
                    member_role="core",
                    decision_status="retained",
                    distance_to_medoid=None,
                    density_radius=None,
                    support_ratio=None,
                    attachment_support_ratio=None,
                    nearest_competing_cluster_distance=None,
                    separation_gap=None,
                    decision_reason_code=None,
                    is_trusted_seed_candidate=False,
                    is_selected_trusted_seed=False,
                    seed_rank=None,
                    is_representative=False,
                    diagnostic_json={"phase": "raw"},
                )

        for cleaned_cluster in plan.get("cleaned_clusters") or []:
            cleaned_members = [int(obs_id) for obs_id in cleaned_cluster.get("members") or []]
            split_rejected = [int(obs_id) for obs_id in cleaned_cluster.get("split_rejected_members") or []]
            cluster_id = self.cluster_repo.insert_cluster(
                run_id=int(run_id),
                cluster_stage="cleaned",
                cluster_state="active",
                member_count=int(len(cleaned_members) + len(split_rejected)),
                retained_member_count=int(len(cleaned_members)),
                anchor_core_count=0,
                core_count=int(len(cleaned_members)),
                boundary_count=0,
                attachment_count=0,
                excluded_count=int(len(split_rejected)),
                distinct_photo_count=int(len(cleaned_members)),
                compactness_p50=None,
                compactness_p90=None,
                support_ratio_p10=None,
                support_ratio_p50=None,
                intra_photo_conflict_ratio=0.0,
                nearest_cluster_distance=None,
                separation_gap=None,
                boundary_ratio=0.0,
                discard_reason_code=None,
                representative_observation_id=int(cleaned_members[0]) if cleaned_members else None,
                summary_json=dict(cleaned_cluster.get("summary_json") or {}),
            )
            cleaned_ids.append(int(cluster_id))
            for obs_id in cleaned_members:
                self.cluster_repo.insert_cluster_member(
                    cluster_id=int(cluster_id),
                    observation_id=int(obs_id),
                    source_pool_kind="core_discovery",
                    quality_score_snapshot=None,
                    member_role="core",
                    decision_status="retained",
                    distance_to_medoid=None,
                    density_radius=None,
                    support_ratio=None,
                    attachment_support_ratio=None,
                    nearest_competing_cluster_distance=None,
                    separation_gap=None,
                    decision_reason_code=None,
                    is_trusted_seed_candidate=False,
                    is_selected_trusted_seed=False,
                    seed_rank=None,
                    is_representative=False,
                    diagnostic_json={"phase": "cleaned"},
                )
            for obs_id in split_rejected:
                self.cluster_repo.insert_cluster_member(
                    cluster_id=int(cluster_id),
                    observation_id=int(obs_id),
                    source_pool_kind="core_discovery",
                    quality_score_snapshot=None,
                    member_role="excluded",
                    decision_status="rejected",
                    distance_to_medoid=None,
                    density_radius=None,
                    support_ratio=None,
                    attachment_support_ratio=None,
                    nearest_competing_cluster_distance=None,
                    separation_gap=None,
                    decision_reason_code="split_into_other_child",
                    is_trusted_seed_candidate=False,
                    is_selected_trusted_seed=False,
                    seed_rank=None,
                    is_representative=False,
                    diagnostic_json={"phase": "cleaned_split"},
                )

        for final_cluster in plan.get("final_clusters") or []:
            metrics = dict(final_cluster.get("persist_metrics") or {})
            cluster_id = self.cluster_repo.insert_cluster(
                run_id=int(run_id),
                cluster_stage="final",
                cluster_state=str(final_cluster.get("cluster_state") or "active"),
                member_count=int(metrics.get("member_count") or 0),
                retained_member_count=int(metrics.get("retained_member_count") or 0),
                anchor_core_count=int(metrics.get("anchor_core_count") or 0),
                core_count=int(metrics.get("core_count") or 0),
                boundary_count=int(metrics.get("boundary_count") or 0),
                attachment_count=int(metrics.get("attachment_count") or 0),
                excluded_count=int(metrics.get("excluded_count") or 0),
                distinct_photo_count=int(metrics.get("distinct_photo_count") or 0),
                compactness_p50=float(metrics.get("compactness_p50") or 0.0),
                compactness_p90=float(metrics.get("compactness_p90") or 0.0),
                support_ratio_p10=float(metrics.get("support_ratio_p10") or 0.0),
                support_ratio_p50=float(metrics.get("support_ratio_p50") or 0.0),
                intra_photo_conflict_ratio=float(metrics.get("intra_photo_conflict_ratio") or 0.0),
                nearest_cluster_distance=(
                    float(metrics["nearest_cluster_distance"])
                    if metrics.get("nearest_cluster_distance") is not None
                    else None
                ),
                separation_gap=(
                    float(metrics["separation_gap"])
                    if metrics.get("separation_gap") is not None
                    else None
                ),
                boundary_ratio=float(metrics.get("boundary_ratio") or 0.0),
                discard_reason_code=(
                    str(metrics["discard_reason_code"])
                    if metrics.get("discard_reason_code") is not None
                    else None
                ),
                representative_observation_id=(
                    int(metrics["representative_observation_id"])
                    if metrics.get("representative_observation_id") is not None
                    else None
                ),
                summary_json=dict(final_cluster.get("summary_json") or {}),
            )
            final_ids.append(int(cluster_id))

            for member in final_cluster.get("members") or []:
                self.cluster_repo.insert_cluster_member(
                    cluster_id=int(cluster_id),
                    observation_id=int(member["observation_id"]),
                    source_pool_kind=str(member.get("source_pool_kind") or "excluded"),
                    quality_score_snapshot=(
                        float(member["quality_score_snapshot"])
                        if member.get("quality_score_snapshot") is not None
                        else None
                    ),
                    member_role=str(member.get("member_role") or "excluded"),
                    decision_status=str(member.get("decision_status") or "rejected"),
                    distance_to_medoid=(
                        float(member["distance_to_medoid"])
                        if member.get("distance_to_medoid") is not None
                        else None
                    ),
                    density_radius=(
                        float(member["density_radius"])
                        if member.get("density_radius") is not None
                        else None
                    ),
                    support_ratio=(
                        float(member["support_ratio"])
                        if member.get("support_ratio") is not None
                        else None
                    ),
                    attachment_support_ratio=(
                        float(member["attachment_support_ratio"])
                        if member.get("attachment_support_ratio") is not None
                        else None
                    ),
                    nearest_competing_cluster_distance=(
                        float(member["nearest_competing_cluster_distance"])
                        if member.get("nearest_competing_cluster_distance") is not None
                        else None
                    ),
                    separation_gap=(
                        float(member["separation_gap"])
                        if member.get("separation_gap") is not None
                        else None
                    ),
                    decision_reason_code=(
                        str(member["decision_reason_code"])
                        if member.get("decision_reason_code") is not None
                        else None
                    ),
                    is_trusted_seed_candidate=bool(member.get("is_trusted_seed_candidate")),
                    is_selected_trusted_seed=bool(member.get("is_selected_trusted_seed")),
                    seed_rank=(int(member["seed_rank"]) if member.get("seed_rank") is not None else None),
                    is_representative=bool(member.get("is_representative")),
                    diagnostic_json=dict(member.get("diagnostic_json") or {}),
                )

            resolution_detail = dict(final_cluster.get("resolution_detail") or {})
            self.cluster_repo.insert_cluster_resolution(
                cluster_id=int(cluster_id),
                resolution_state=str(final_cluster.get("resolution_state") or "unresolved"),
                resolution_reason=(
                    str(final_cluster["resolution_reason"])
                    if final_cluster.get("resolution_reason") is not None
                    else None
                ),
                source_run_id=int(run_id),
                trusted_seed_count=int(resolution_detail.get("trusted_seed_count") or 0),
                trusted_seed_candidate_count=int(resolution_detail.get("trusted_seed_candidate_count") or 0),
                trusted_seed_reject_distribution_json=dict(
                    resolution_detail.get("trusted_seed_reject_distribution") or {}
                ),
                detail_json={
                    "cluster_state": str(final_cluster.get("cluster_state") or "active"),
                    "summary": dict(final_cluster.get("summary_json") or {}),
                },
            )

        for lineage in plan.get("lineage") or []:
            parent_stage = str(lineage.get("parent_stage"))
            child_stage = str(lineage.get("child_stage"))
            parent_index = int(lineage.get("parent_index", -1))
            child_index = int(lineage.get("child_index", -1))

            if parent_stage == "raw":
                if parent_index < 0 or parent_index >= len(raw_ids):
                    raise ValueError(f"lineage parent 索引越界(raw): {parent_index}")
                parent_id = int(raw_ids[parent_index])
            elif parent_stage == "cleaned":
                if parent_index < 0 or parent_index >= len(cleaned_ids):
                    raise ValueError(f"lineage parent 索引越界(cleaned): {parent_index}")
                parent_id = int(cleaned_ids[parent_index])
            else:
                raise ValueError(f"lineage parent_stage 非法: {parent_stage}")

            if child_stage == "cleaned":
                if child_index < 0 or child_index >= len(cleaned_ids):
                    raise ValueError(f"lineage child 索引越界(cleaned): {child_index}")
                child_id = int(cleaned_ids[child_index])
            elif child_stage == "final":
                if child_index < 0 or child_index >= len(final_ids):
                    raise ValueError(f"lineage child 索引越界(final): {child_index}")
                child_id = int(final_ids[child_index])
            else:
                raise ValueError(f"lineage child_stage 非法: {child_stage}")

            self.cluster_repo.insert_cluster_lineage(
                parent_cluster_id=int(parent_id),
                child_cluster_id=int(child_id),
                relation_kind=str(lineage.get("relation_kind") or "derived"),
                reason_code=(
                    str(lineage["reason_code"])
                    if lineage.get("reason_code") is not None
                    else None
                ),
                detail_json=dict(lineage.get("detail_json") or {}),
            )

        return self.cluster_repo.get_run_persistence_counts(run_id=int(run_id))

    def _assert_transition_allowed(self, *, current: str, target: str) -> None:
        if current == target:
            return
        if current in self._TERMINAL_STATUSES:
            raise ValueError(f"run_status 终态不允许转换: {current} -> {target}")
        allowed = {
            "created": {"running", "succeeded", "failed", "cancelled"},
            "running": {"succeeded", "failed", "cancelled"},
        }
        if target not in allowed.get(current, set()):
            raise ValueError(f"非法 run_status 转换: {current} -> {target}")

    def _load_json(self, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if not isinstance(value, str) or value.strip() == "":
            return {}
        try:
            import json

            payload = json.loads(value)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}
