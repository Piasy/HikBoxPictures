from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import math
from typing import Any

import numpy as np

from hikbox_pictures.repositories.identity_cluster_repo import IdentityClusterRepo


@dataclass(frozen=True)
class _Point:
    observation_id: int
    photo_asset_id: int
    quality_score: float
    vector: np.ndarray


class IdentityClusterAlgorithm:
    def __init__(self, conn, *, cluster_repo: IdentityClusterRepo) -> None:
        self.conn = conn
        self.cluster_repo = cluster_repo

    def build_run_plan(
        self,
        *,
        observation_snapshot_id: int,
        cluster_profile_id: int,
        progress_reporter: Callable[[dict[str, object]], None] | None = None,
    ) -> dict[str, Any]:
        self.cluster_repo.get_snapshot_required(observation_snapshot_id)
        profile = self.cluster_repo.get_cluster_profile_required(cluster_profile_id)

        core_rows = self.cluster_repo.list_snapshot_pool_rows(
            snapshot_id=observation_snapshot_id,
            pool_kind="core_discovery",
        )
        attachment_rows = self.cluster_repo.list_snapshot_pool_rows(
            snapshot_id=observation_snapshot_id,
            pool_kind="attachment",
        )
        if not core_rows:
            raise ValueError("snapshot 缺少 core_discovery 样本，无法执行 cluster run")

        core_points = [
            _Point(
                observation_id=int(row["observation_id"]),
                photo_asset_id=int(row["photo_asset_id"]),
                quality_score=float(row["quality_score"]),
                vector=np.asarray(row["vector"], dtype=np.float32),
            )
            for row in core_rows
        ]
        attachment_points = [
            _Point(
                observation_id=int(row["observation_id"]),
                photo_asset_id=int(row["photo_asset_id"]),
                quality_score=float(row["quality_score"]),
                vector=np.asarray(row["vector"], dtype=np.float32),
            )
            for row in attachment_rows
        ]

        vector_map: dict[int, np.ndarray] = {
            int(point.observation_id): np.asarray(point.vector, dtype=np.float32)
            for point in [*core_points, *attachment_points]
        }
        photo_map: dict[int, int] = {
            int(point.observation_id): int(point.photo_asset_id)
            for point in [*core_points, *attachment_points]
        }
        quality_map: dict[int, float] = {
            int(point.observation_id): float(point.quality_score)
            for point in [*core_points, *attachment_points]
        }

        raw_clusters = self._build_raw_clusters(
            core_points=core_points,
            profile=profile,
            vector_map=vector_map,
            progress_reporter=progress_reporter,
        )
        cleaned_clusters, raw_to_cleaned_lineage = self._build_cleaned_clusters(
            raw_clusters=raw_clusters,
            profile=profile,
            vector_map=vector_map,
            progress_reporter=progress_reporter,
        )
        final_clusters = self._build_final_clusters(
            cleaned_clusters=cleaned_clusters,
            attachment_points=attachment_points,
            profile=profile,
            core_points=core_points,
            vector_map=vector_map,
            photo_map=photo_map,
            quality_map=quality_map,
            progress_reporter=progress_reporter,
        )

        lineage: list[dict[str, Any]] = [*raw_to_cleaned_lineage]
        for index in range(len(final_clusters)):
            lineage.append(
                {
                    "parent_stage": "cleaned",
                    "parent_index": int(index),
                    "child_stage": "final",
                    "child_index": int(index),
                    "relation_kind": "promote",
                    "reason_code": None,
                    "detail_json": {},
                }
            )

        run_summary = {
            "cluster_count": int(len(final_clusters)),
            "active_cluster_count": int(sum(1 for item in final_clusters if item["cluster_state"] == "active")),
            "discarded_cluster_count": int(sum(1 for item in final_clusters if item["cluster_state"] == "discarded")),
            "lineage_edge_count": int(len(lineage)),
            "member_count": int(sum(len(item["members"]) for item in final_clusters)),
            "algorithm_version": "identity.cluster_run.v3_1",
        }

        return {
            "raw_clusters": raw_clusters,
            "cleaned_clusters": cleaned_clusters,
            "final_clusters": final_clusters,
            "lineage": lineage,
            "run_summary": run_summary,
        }

    def _build_raw_clusters(
        self,
        *,
        core_points: list[_Point],
        profile: dict[str, Any],
        vector_map: dict[int, np.ndarray],
        progress_reporter: Callable[[dict[str, object]], None] | None = None,
    ) -> list[dict[str, Any]]:
        obs_ids = [int(point.observation_id) for point in core_points]
        index_by_obs = {obs_id: idx for idx, obs_id in enumerate(obs_ids)}
        k = min(max(1, int(profile["discovery_knn_k"])), max(0, len(obs_ids) - 1))
        max_edge_distance = float(profile.get("raw_edge_max_distance", 1.0))

        neighbors_by_obs: dict[int, list[tuple[int, float]]] = {}
        for index, obs_id in enumerate(obs_ids, start=1):
            distances = sorted(
                (
                    (other_id, self._cosine_distance(vector_map[obs_id], vector_map[other_id]))
                    for other_id in obs_ids
                    if other_id != obs_id
                ),
                key=lambda item: (float(item[1]), int(item[0])),
            )
            neighbors_by_obs[obs_id] = [
                (int(other_id), float(distance)) for other_id, distance in distances[:k]
            ]
            self._report_progress(
                progress_reporter,
                subphase="build_raw_neighbors",
                total_count=len(obs_ids),
                completed_count=index,
            )

        undirected_edges: set[tuple[int, int]] = set()
        for obs_id in obs_ids:
            for other_id, distance in neighbors_by_obs.get(obs_id, []):
                if float(distance) > max_edge_distance:
                    continue
                reverse_neighbor_ids = {int(item[0]) for item in neighbors_by_obs.get(other_id, [])}
                if obs_id in reverse_neighbor_ids:
                    edge = (int(min(obs_id, other_id)), int(max(obs_id, other_id)))
                    undirected_edges.add(edge)

        adjacency: dict[int, set[int]] = {obs_id: set() for obs_id in obs_ids}
        for a, b in undirected_edges:
            adjacency[a].add(b)
            adjacency[b].add(a)

        visited: set[int] = set()
        components: list[list[int]] = []
        for obs_id in obs_ids:
            if obs_id in visited:
                continue
            stack = [obs_id]
            component: list[int] = []
            while stack:
                node = stack.pop()
                if node in visited:
                    continue
                visited.add(node)
                component.append(node)
                stack.extend(sorted(adjacency.get(node, set()) - visited, reverse=True))
            components.append(sorted(component))

        components.sort(key=lambda values: (len(values), values[0]), reverse=True)

        result: list[dict[str, Any]] = []
        for component in components:
            edge_pairs: list[list[int]] = []
            for a, b in sorted(undirected_edges):
                if a in component and b in component:
                    edge_pairs.append([int(a), int(b)])
                    edge_pairs.append([int(b), int(a)])
            result.append(
                {
                    "members": [int(obs_id) for obs_id in component],
                    "summary_json": {
                        "mutual_knn_edges": edge_pairs,
                        "component_size": int(len(component)),
                        "raw_edge_max_distance": float(max_edge_distance),
                    },
                }
            )

        return result

    def _build_cleaned_clusters(
        self,
        *,
        raw_clusters: list[dict[str, Any]],
        profile: dict[str, Any],
        vector_map: dict[int, np.ndarray],
        progress_reporter: Callable[[dict[str, object]], None] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        cleaned_clusters: list[dict[str, Any]] = []
        lineage: list[dict[str, Any]] = []
        split_min_component_size = int(profile["split_min_component_size"])
        split_min_medoid_gap = float(profile["split_min_medoid_gap"])

        for raw_index, raw_cluster in enumerate(raw_clusters):
            members = [int(value) for value in raw_cluster["members"]]
            did_split = False
            if len(members) >= max(2 * split_min_component_size, 4):
                pivot_a, pivot_b, pivot_gap = self._farthest_pair(members=members, vector_map=vector_map)
                if pivot_a is not None and pivot_b is not None and float(pivot_gap) >= split_min_medoid_gap:
                    direction = vector_map[pivot_a] - vector_map[pivot_b]
                    if float(np.linalg.norm(direction)) > 0.0:
                        projections = sorted(
                            (
                                (int(obs_id), float(np.dot(vector_map[int(obs_id)], direction)))
                                for obs_id in members
                            ),
                            key=lambda item: (float(item[1]), int(item[0])),
                        )
                        split_at = len(projections) // 2
                        child_a = sorted(int(item[0]) for item in projections[:split_at])
                        child_b = sorted(int(item[0]) for item in projections[split_at:])
                    else:
                        child_a = sorted(members[: len(members) // 2])
                        child_b = sorted(members[len(members) // 2 :])

                    if len(child_a) >= split_min_component_size and len(child_b) >= split_min_component_size:
                        did_split = True
                        cleaned_a_index = len(cleaned_clusters)
                        cleaned_clusters.append(
                            {
                                "members": child_a,
                                "split_rejected_members": [int(obs_id) for obs_id in child_b],
                                "summary_json": {
                                    "split_applied": True,
                                    "parent_raw_index": int(raw_index),
                                    "split_medoid_gap": float(pivot_gap),
                                },
                            }
                        )
                        lineage.append(
                            {
                                "parent_stage": "raw",
                                "parent_index": int(raw_index),
                                "child_stage": "cleaned",
                                "child_index": int(cleaned_a_index),
                                "relation_kind": "split",
                                "reason_code": "medoid_gap_exceeded",
                                "detail_json": {"split_medoid_gap": float(pivot_gap)},
                            }
                        )

                        cleaned_b_index = len(cleaned_clusters)
                        cleaned_clusters.append(
                            {
                                "members": child_b,
                                "split_rejected_members": [int(obs_id) for obs_id in child_a],
                                "summary_json": {
                                    "split_applied": True,
                                    "parent_raw_index": int(raw_index),
                                    "split_medoid_gap": float(pivot_gap),
                                },
                            }
                        )
                        lineage.append(
                            {
                                "parent_stage": "raw",
                                "parent_index": int(raw_index),
                                "child_stage": "cleaned",
                                "child_index": int(cleaned_b_index),
                                "relation_kind": "split",
                                "reason_code": "medoid_gap_exceeded",
                                "detail_json": {"split_medoid_gap": float(pivot_gap)},
                            }
                        )

            if not did_split:
                cleaned_index = len(cleaned_clusters)
                cleaned_clusters.append(
                    {
                        "members": [int(obs_id) for obs_id in members],
                        "split_rejected_members": [],
                        "summary_json": {
                            "split_applied": False,
                            "parent_raw_index": int(raw_index),
                        },
                    }
                )
                lineage.append(
                    {
                        "parent_stage": "raw",
                        "parent_index": int(raw_index),
                        "child_stage": "cleaned",
                        "child_index": int(cleaned_index),
                        "relation_kind": "carry_over",
                        "reason_code": None,
                        "detail_json": {},
                    }
                )
            self._report_progress(
                progress_reporter,
                subphase="build_cleaned_clusters",
                total_count=len(raw_clusters),
                completed_count=raw_index + 1,
            )

        return cleaned_clusters, lineage

    def _build_final_clusters(
        self,
        *,
        cleaned_clusters: list[dict[str, Any]],
        attachment_points: list[_Point],
        profile: dict[str, Any],
        core_points: list[_Point],
        vector_map: dict[int, np.ndarray],
        photo_map: dict[int, int],
        quality_map: dict[int, float],
        progress_reporter: Callable[[dict[str, object]], None] | None = None,
    ) -> list[dict[str, Any]]:
        core_obs_ids = [int(point.observation_id) for point in core_points]

        final_clusters: list[dict[str, Any]] = []
        for index, cleaned_cluster in enumerate(cleaned_clusters, start=1):
            candidate_obs_ids = [int(obs_id) for obs_id in cleaned_cluster["members"]]
            classified = self._classify_core_members(
                candidate_obs_ids=candidate_obs_ids,
                profile=profile,
                core_obs_ids=core_obs_ids,
                vector_map=vector_map,
                photo_map=photo_map,
                quality_map=quality_map,
            )
            final_clusters.append(
                {
                    "members": classified["members"],
                    "core_retained_ids": set(classified["core_retained_ids"]),
                    "anchor_core_radius": float(classified["anchor_core_radius"]),
                    "freeze_metrics": dict(classified["freeze_metrics"]),
                    "cluster_state": str(classified["cluster_state"]),
                    "discard_reason_code": classified["discard_reason_code"],
                    "resolution_state": str(classified["resolution_state"]),
                    "resolution_reason": classified["resolution_reason"],
                    "summary_json": dict(classified["summary_json"]),
                }
            )
            self._report_progress(
                progress_reporter,
                subphase="classify_final_clusters",
                total_count=len(cleaned_clusters),
                completed_count=index,
            )

        self._assign_attachments(
            final_clusters=final_clusters,
            attachment_points=attachment_points,
            profile=profile,
            core_obs_ids=core_obs_ids,
            vector_map=vector_map,
            photo_map=photo_map,
            progress_reporter=progress_reporter,
        )

        self._finalize_cluster_metrics(
            final_clusters=final_clusters,
            profile=profile,
            core_obs_ids=core_obs_ids,
            vector_map=vector_map,
            photo_map=photo_map,
            quality_map=quality_map,
            progress_reporter=progress_reporter,
        )

        return final_clusters

    def _classify_core_members(
        self,
        *,
        candidate_obs_ids: list[int],
        profile: dict[str, Any],
        core_obs_ids: list[int],
        vector_map: dict[int, np.ndarray],
        photo_map: dict[int, int],
        quality_map: dict[int, float],
    ) -> dict[str, Any]:
        anchor_min = float(profile["anchor_core_min_support_ratio"])
        core_min = float(profile["core_min_support_ratio"])
        boundary_min = float(profile["boundary_min_support_ratio"])
        boundary_multiplier = float(profile["boundary_radius_multiplier"])
        knn_k = int(profile["discovery_knn_k"])
        quantile_q = float(profile["anchor_core_radius_quantile"])

        retained_ids = set(int(obs_id) for obs_id in candidate_obs_ids)
        roles_by_obs: dict[int, str] = {}

        for _ in range(3):
            if not retained_ids:
                break
            medoid_id = self._medoid_id(obs_ids=sorted(retained_ids), vector_map=vector_map)
            dist_by_obs = {
                int(obs_id): self._cosine_distance(vector_map[int(obs_id)], vector_map[int(medoid_id)])
                for obs_id in candidate_obs_ids
            }
            retained_distances = [float(dist_by_obs[obs_id]) for obs_id in sorted(retained_ids)]
            anchor_radius = self._quantile(retained_distances, quantile_q)

            support_by_obs: dict[int, float] = {}
            for obs_id in candidate_obs_ids:
                support_by_obs[int(obs_id)] = self._support_ratio(
                    observation_id=int(obs_id),
                    cluster_member_ids=retained_ids,
                    core_obs_ids=core_obs_ids,
                    vector_map=vector_map,
                    photo_map=photo_map,
                    knn_k=knn_k,
                )

            next_retained: set[int] = set()
            next_roles: dict[int, str] = {}
            for obs_id in candidate_obs_ids:
                support = float(support_by_obs[int(obs_id)])
                distance = float(dist_by_obs[int(obs_id)])
                if support >= anchor_min and distance <= anchor_radius:
                    role = "anchor_core"
                    next_retained.add(int(obs_id))
                elif support >= core_min:
                    role = "core"
                    next_retained.add(int(obs_id))
                elif support >= boundary_min and distance <= anchor_radius * boundary_multiplier:
                    role = "boundary"
                    next_retained.add(int(obs_id))
                else:
                    role = "excluded"
                next_roles[int(obs_id)] = role

            if not next_retained and candidate_obs_ids:
                fallback = max(
                    candidate_obs_ids,
                    key=lambda obs_id: (
                        float(support_by_obs[int(obs_id)]),
                        float(quality_map.get(int(obs_id), 0.0)),
                        -int(obs_id),
                    ),
                )
                next_retained = {int(fallback)}
                next_roles[int(fallback)] = "boundary"

            retained_ids = next_retained
            roles_by_obs = next_roles

        retained_sorted = sorted(retained_ids)
        if retained_sorted:
            retained_support = {
                obs_id: self._support_ratio(
                    observation_id=int(obs_id),
                    cluster_member_ids=retained_ids,
                    core_obs_ids=core_obs_ids,
                    vector_map=vector_map,
                    photo_map=photo_map,
                    knn_k=knn_k,
                )
                for obs_id in retained_sorted
            }
        else:
            retained_support = {}

        if retained_sorted and not any(roles_by_obs.get(obs_id) == "anchor_core" for obs_id in retained_sorted):
            best_anchor = max(
                retained_sorted,
                key=lambda obs_id: (
                    float(retained_support.get(obs_id, 0.0)),
                    float(quality_map.get(obs_id, 0.0)),
                    -int(obs_id),
                ),
            )
            roles_by_obs[best_anchor] = "anchor_core"

        if retained_sorted and not any(roles_by_obs.get(obs_id) == "core" for obs_id in retained_sorted):
            candidates = [obs_id for obs_id in retained_sorted if roles_by_obs.get(obs_id) != "anchor_core"]
            if candidates:
                best_core = max(
                    candidates,
                    key=lambda obs_id: (
                        float(retained_support.get(obs_id, 0.0)),
                        float(quality_map.get(obs_id, 0.0)),
                        -int(obs_id),
                    ),
                )
                roles_by_obs[best_core] = "core"

        if len(retained_sorted) >= 3 and not any(roles_by_obs.get(obs_id) == "boundary" for obs_id in retained_sorted):
            candidates = [obs_id for obs_id in retained_sorted if roles_by_obs.get(obs_id) == "core"]
            if not candidates:
                candidates = [obs_id for obs_id in retained_sorted if roles_by_obs.get(obs_id) == "anchor_core"]
            if len(candidates) >= 2:
                boundary_obs = min(
                    candidates,
                    key=lambda obs_id: (
                        float(retained_support.get(obs_id, 0.0)),
                        -float(quality_map.get(obs_id, 0.0)),
                        int(obs_id),
                    ),
                )
                roles_by_obs[boundary_obs] = "boundary"

        medoid_for_core = (
            self._medoid_id(obs_ids=retained_sorted, vector_map=vector_map)
            if retained_sorted
            else self._medoid_id(obs_ids=sorted(candidate_obs_ids), vector_map=vector_map)
        )
        dist_to_medoid = {
            int(obs_id): self._cosine_distance(vector_map[int(obs_id)], vector_map[int(medoid_for_core)])
            for obs_id in candidate_obs_ids
        }

        retained_core_distances = [float(dist_to_medoid[obs_id]) for obs_id in retained_sorted]
        anchor_core_radius = self._quantile(retained_core_distances, float(profile["anchor_core_radius_quantile"]))

        members: list[dict[str, Any]] = []
        for obs_id in sorted(candidate_obs_ids):
            retained = int(obs_id) in retained_ids
            role = roles_by_obs.get(int(obs_id), "excluded")
            support_value = self._support_ratio(
                observation_id=int(obs_id),
                cluster_member_ids=retained_ids,
                core_obs_ids=core_obs_ids,
                vector_map=vector_map,
                photo_map=photo_map,
                knn_k=knn_k,
            )
            members.append(
                {
                    "observation_id": int(obs_id),
                    "source_pool_kind": "core_discovery",
                    "quality_score_snapshot": float(quality_map.get(int(obs_id), 0.0)),
                    "member_role": str(role if retained else "excluded"),
                    "decision_status": "retained" if retained else "rejected",
                    "distance_to_medoid": float(dist_to_medoid[int(obs_id)]),
                    "density_radius": None,
                    "support_ratio": float(support_value),
                    "attachment_support_ratio": None,
                    "nearest_competing_cluster_distance": None,
                    "separation_gap": None,
                    "decision_reason_code": None if retained else "outside_boundary_radius",
                    "is_trusted_seed_candidate": False,
                    "is_selected_trusted_seed": False,
                    "seed_rank": None,
                    "is_representative": False,
                    "diagnostic_json": {"phase": "core_classify"},
                }
            )

        anchor_core_count = sum(
            1 for obs_id in retained_sorted if str(roles_by_obs.get(obs_id, "")) == "anchor_core"
        )
        core_count = sum(1 for obs_id in retained_sorted if str(roles_by_obs.get(obs_id, "")) == "core")
        boundary_count = sum(1 for obs_id in retained_sorted if str(roles_by_obs.get(obs_id, "")) == "boundary")
        distinct_photo_count = len({int(photo_map[int(obs_id)]) for obs_id in retained_sorted})
        support_values = [float(item["support_ratio"]) for item in members if item["decision_status"] == "retained"]
        compactness_values = retained_core_distances
        intra_conflict_pre = self._intra_photo_conflict_ratio(
            observation_ids=retained_sorted,
            photo_map=photo_map,
        )
        freeze_metrics = {
            "retained_member_count": int(len(retained_sorted)),
            "anchor_core_count": int(anchor_core_count),
            "core_count": int(core_count),
            "boundary_count": int(boundary_count),
            "distinct_photo_count": int(distinct_photo_count),
            "support_ratio_p10": float(self._quantile(support_values, 0.10)) if support_values else 0.0,
            "support_ratio_p50": float(self._quantile(support_values, 0.50)) if support_values else 0.0,
            "compactness_p50": float(self._quantile(compactness_values, 0.50)) if compactness_values else 0.0,
            "compactness_p90": float(self._quantile(compactness_values, 0.90)) if compactness_values else 0.0,
            "boundary_ratio": (
                float(boundary_count) / float(max(1, len(retained_sorted)))
                if retained_sorted
                else 0.0
            ),
            "intra_photo_conflict_ratio_pre": float(intra_conflict_pre),
        }

        existence_reason = self._existence_reason(metrics=freeze_metrics, profile=profile)
        cluster_state = "discarded" if existence_reason is not None else "active"
        resolution_state = "discarded" if existence_reason is not None else "review_pending"

        return {
            "members": members,
            "core_retained_ids": retained_sorted,
            "anchor_core_radius": float(anchor_core_radius),
            "freeze_metrics": freeze_metrics,
            "cluster_state": cluster_state,
            "discard_reason_code": existence_reason,
            "resolution_state": resolution_state,
            "resolution_reason": existence_reason,
            "summary_json": {
                "anchor_core_radius": float(anchor_core_radius),
                "core_medoid_observation_id": int(medoid_for_core),
            },
        }

    def _assign_attachments(
        self,
        *,
        final_clusters: list[dict[str, Any]],
        attachment_points: list[_Point],
        profile: dict[str, Any],
        core_obs_ids: list[int],
        vector_map: dict[int, np.ndarray],
        photo_map: dict[int, int],
        progress_reporter: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        if not attachment_points or not final_clusters:
            return

        active_indices = [
            idx
            for idx, cluster in enumerate(final_clusters)
            if cluster["cluster_state"] == "active" and cluster["core_retained_ids"]
        ]
        if not active_indices:
            active_indices = [0]

        sorted_attachment_points = sorted(attachment_points, key=lambda value: int(value.observation_id))
        for attachment_index, point in enumerate(sorted_attachment_points, start=1):
            distance_candidates: list[tuple[int, float]] = []
            for candidate_index in active_indices:
                retained_ids = [int(obs_id) for obs_id in final_clusters[candidate_index].get("core_retained_ids", set())]
                if not retained_ids:
                    continue
                representative = self._medoid_id(obs_ids=retained_ids, vector_map=vector_map)
                dist = self._cosine_distance(
                    vector_map[int(point.observation_id)],
                    vector_map[int(representative)],
                )
                distance_candidates.append((int(candidate_index), float(dist)))

            if not distance_candidates:
                target_index = int(active_indices[0])
                nearest_distance = 1.0
                separation_gap = 0.0
            else:
                distance_candidates.sort(key=lambda item: (float(item[1]), int(item[0])))
                target_index = int(distance_candidates[0][0])
                nearest_distance = float(distance_candidates[0][1])
                if len(distance_candidates) > 1:
                    second_distance = float(distance_candidates[1][1])
                    separation_gap = max(0.0, float(second_distance - nearest_distance))
                else:
                    # 单候选 cluster 时缺少“次近”距离，按 profile 最小门槛归一化，
                    # 避免因为竞争项缺失而把 separation_gap 错判为 0。
                    separation_gap = max(
                        float(profile["attachment_min_separation_gap"]),
                        float(nearest_distance),
                    )

            target_cluster = final_clusters[target_index]
            retained_for_support = {
                int(member["observation_id"])
                for member in target_cluster["members"]
                if member["decision_status"] == "retained"
            }
            if not retained_for_support:
                retained_for_support = {int(obs_id) for obs_id in target_cluster.get("core_retained_ids", set())}

            attachment_support = self._support_ratio(
                observation_id=int(point.observation_id),
                cluster_member_ids=retained_for_support,
                core_obs_ids=core_obs_ids,
                vector_map=vector_map,
                photo_map=photo_map,
                knn_k=int(profile["attachment_candidate_knn_k"]),
                cap_by_cluster_size=True,
            )

            can_attach = (
                nearest_distance <= float(profile["attachment_max_distance"])
                and attachment_support >= float(profile["attachment_min_support_ratio"])
                and separation_gap >= float(profile["attachment_min_separation_gap"])
                and target_cluster["cluster_state"] == "active"
            )

            target_cluster["members"].append(
                {
                    "observation_id": int(point.observation_id),
                    "source_pool_kind": "attachment",
                    "quality_score_snapshot": float(point.quality_score),
                    "member_role": "attachment" if can_attach else "excluded",
                    "decision_status": "retained" if can_attach else "rejected",
                    "distance_to_medoid": float(nearest_distance),
                    "density_radius": None,
                    "support_ratio": None,
                    "attachment_support_ratio": float(attachment_support),
                    "nearest_competing_cluster_distance": float(nearest_distance),
                    "separation_gap": float(separation_gap),
                    "decision_reason_code": None if can_attach else "outside_boundary_radius",
                    "is_trusted_seed_candidate": False,
                    "is_selected_trusted_seed": False,
                    "seed_rank": None,
                    "is_representative": False,
                    "diagnostic_json": {
                        "phase": "attachment_assign",
                        "can_attach": bool(can_attach),
                    },
                }
            )
            self._report_progress(
                progress_reporter,
                subphase="assign_attachments",
                total_count=len(sorted_attachment_points),
                completed_count=attachment_index,
            )

    def _finalize_cluster_metrics(
        self,
        *,
        final_clusters: list[dict[str, Any]],
        profile: dict[str, Any],
        core_obs_ids: list[int],
        vector_map: dict[int, np.ndarray],
        photo_map: dict[int, int],
        quality_map: dict[int, float],
        progress_reporter: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        retained_rep_ids: dict[int, int] = {}

        for index, cluster in enumerate(final_clusters):
            members = cluster["members"]
            retained_members = [item for item in members if item["decision_status"] == "retained"]
            retained_ids = sorted({int(item["observation_id"]) for item in retained_members})

            if retained_ids:
                representative = self._medoid_id(obs_ids=retained_ids, vector_map=vector_map)
                retained_rep_ids[int(index)] = int(representative)
            else:
                representative = None

            for item in members:
                item["is_representative"] = bool(
                    representative is not None and int(item["observation_id"]) == int(representative)
                )

            for item in retained_members:
                item["density_radius"] = float(
                    self._density_radius(
                        observation_id=int(item["observation_id"]),
                        retained_ids=retained_ids,
                        vector_map=vector_map,
                        min_samples=int(profile["density_min_samples"]),
                    )
                )

            for item in retained_members:
                role = str(item["member_role"])
                if role in {"anchor_core", "core", "boundary"}:
                    item["support_ratio"] = float(
                        self._support_ratio(
                            observation_id=int(item["observation_id"]),
                            cluster_member_ids=set(retained_ids),
                            core_obs_ids=core_obs_ids,
                            vector_map=vector_map,
                            photo_map=photo_map,
                            knn_k=int(profile["discovery_knn_k"]),
                        )
                    )

            core_retained = [
                item
                for item in retained_members
                if str(item["member_role"]) in {"anchor_core", "core", "boundary"}
            ]
            core_retained_ids = sorted({int(item["observation_id"]) for item in core_retained})
            core_distances = [float(item["distance_to_medoid"] or 0.0) for item in core_retained]
            support_values = [float(item["support_ratio"] or 0.0) for item in core_retained]
            anchor_core_count = sum(1 for item in core_retained if item["member_role"] == "anchor_core")
            core_count = sum(1 for item in core_retained if item["member_role"] == "core")
            boundary_count = sum(1 for item in core_retained if item["member_role"] == "boundary")
            attachment_count = sum(
                1
                for item in retained_members
                if str(item["member_role"]) == "attachment"
            )
            excluded_count = sum(1 for item in members if item["decision_status"] != "retained")

            freeze_metrics = cluster["freeze_metrics"]
            retained_member_count = int(freeze_metrics["retained_member_count"])
            distinct_photo_count = int(freeze_metrics["distinct_photo_count"])
            compactness_p50 = float(freeze_metrics["compactness_p50"])
            compactness_p90 = float(freeze_metrics["compactness_p90"])
            support_ratio_p10 = float(freeze_metrics["support_ratio_p10"])
            support_ratio_p50 = float(freeze_metrics["support_ratio_p50"])
            boundary_ratio = float(freeze_metrics["boundary_ratio"])

            intra_conflict_ratio = self._intra_photo_conflict_ratio(
                observation_ids=retained_ids,
                photo_map=photo_map,
            )

            cluster["persist_metrics"] = {
                "member_count": int(len(members)),
                "retained_member_count": int(retained_member_count),
                "anchor_core_count": int(anchor_core_count),
                "core_count": int(core_count),
                "boundary_count": int(boundary_count),
                "attachment_count": int(attachment_count),
                "excluded_count": int(excluded_count),
                "distinct_photo_count": int(distinct_photo_count),
                "compactness_p50": float(compactness_p50),
                "compactness_p90": float(compactness_p90),
                "support_ratio_p10": float(support_ratio_p10),
                "support_ratio_p50": float(support_ratio_p50),
                "intra_photo_conflict_ratio": float(intra_conflict_ratio),
                "boundary_ratio": float(boundary_ratio),
                "discard_reason_code": cluster["discard_reason_code"],
                "representative_observation_id": int(representative) if representative is not None else None,
            }

            anchor_radius = self._quantile(core_distances, float(profile["anchor_core_radius_quantile"]))
            cluster["summary_json"] = {
                **cluster["summary_json"],
                "anchor_core_radius": float(anchor_radius),
                "retained_core_count": int(len(core_retained_ids)),
                "attachment_retained_count": int(attachment_count),
            }

            self._mark_trusted_seeds(cluster=cluster, profile=profile, quality_map=quality_map)
            self._report_progress(
                progress_reporter,
                subphase="finalize_cluster_metrics",
                total_count=len(final_clusters),
                completed_count=index + 1,
            )

        for index, cluster in enumerate(final_clusters):
            representative = retained_rep_ids.get(int(index))
            if representative is None:
                cluster["persist_metrics"]["nearest_cluster_distance"] = None
                cluster["persist_metrics"]["separation_gap"] = None
            else:
                distances = []
                for other_index, other_rep in retained_rep_ids.items():
                    if int(other_index) == int(index):
                        continue
                    distances.append(
                        self._cosine_distance(
                            vector_map[int(representative)],
                            vector_map[int(other_rep)],
                        )
                    )
                if not distances:
                    cluster["persist_metrics"]["nearest_cluster_distance"] = None
                    cluster["persist_metrics"]["separation_gap"] = None
                else:
                    nearest = float(min(distances))
                    cluster["persist_metrics"]["nearest_cluster_distance"] = float(nearest)
                    cluster["persist_metrics"]["separation_gap"] = float(
                        max(0.0, nearest - float(cluster["persist_metrics"]["compactness_p90"]))
                    )
            self._report_progress(
                progress_reporter,
                subphase="measure_cluster_separation",
                total_count=len(final_clusters),
                completed_count=index + 1,
            )

    def _mark_trusted_seeds(
        self,
        *,
        cluster: dict[str, Any],
        profile: dict[str, Any],
        quality_map: dict[int, float],
    ) -> None:
        allow_boundary = int(profile["trusted_seed_allow_boundary"]) == 1
        candidates: list[tuple[int, float]] = []

        for member in cluster["members"]:
            if member["decision_status"] != "retained":
                continue
            role = str(member["member_role"])
            if role == "attachment":
                continue
            if role == "boundary" and not allow_boundary:
                continue
            obs_id = int(member["observation_id"])
            quality = float(quality_map.get(obs_id, float(member.get("quality_score_snapshot") or 0.0)))
            if quality < float(profile["trusted_seed_min_quality"]):
                continue
            candidates.append((obs_id, quality))

        candidates.sort(key=lambda item: (-float(item[1]), int(item[0])))
        selected: list[int] = []
        if len(candidates) >= int(profile["trusted_seed_min_count"]):
            selected = [
                int(obs_id)
                for obs_id, _ in candidates[: int(profile["trusted_seed_max_count"])]
            ]

        selected_set = set(selected)
        rank_map = {obs_id: index + 1 for index, obs_id in enumerate(selected)}
        reject_distribution = {
            "below_quality_threshold": int(
                sum(
                    1
                    for member in cluster["members"]
                    if member["decision_status"] == "retained"
                    and str(member["member_role"]) != "attachment"
                    and float(member.get("quality_score_snapshot") or 0.0) < float(profile["trusted_seed_min_quality"])
                )
            ),
            "boundary_not_allowed": int(
                sum(
                    1
                    for member in cluster["members"]
                    if member["decision_status"] == "retained"
                    and str(member["member_role"]) == "boundary"
                    and not allow_boundary
                    and float(member.get("quality_score_snapshot") or 0.0) >= float(profile["trusted_seed_min_quality"])
                )
            ),
        }

        for member in cluster["members"]:
            obs_id = int(member["observation_id"])
            is_candidate = any(obs_id == item[0] for item in candidates)
            member["is_trusted_seed_candidate"] = bool(is_candidate)
            member["is_selected_trusted_seed"] = bool(obs_id in selected_set)
            member["seed_rank"] = int(rank_map[obs_id]) if obs_id in rank_map else None

        cluster["resolution_detail"] = {
            "trusted_seed_count": int(len(selected_set)),
            "trusted_seed_candidate_count": int(len(candidates)),
            "trusted_seed_reject_distribution": reject_distribution,
        }

    def _report_progress(
        self,
        progress_reporter: Callable[[dict[str, object]], None] | None,
        *,
        subphase: str,
        total_count: int,
        completed_count: int,
    ) -> None:
        if progress_reporter is None:
            return
        total = max(0, int(total_count))
        completed = min(max(0, int(completed_count)), total)
        percent = 100.0 if total <= 0 else round((completed / total) * 100.0, 1)
        progress_reporter(
            {
                "phase": "cluster_run",
                "subphase": str(subphase),
                "status": "running",
                "total_count": total,
                "completed_count": completed,
                "percent": percent,
            }
        )

    def _support_ratio(
        self,
        *,
        observation_id: int,
        cluster_member_ids: set[int],
        core_obs_ids: list[int],
        vector_map: dict[int, np.ndarray],
        photo_map: dict[int, int],
        knn_k: int,
        cap_by_cluster_size: bool = False,
    ) -> float:
        if int(observation_id) not in vector_map:
            return 0.0

        neighbors = sorted(
            (
                (int(candidate_id), self._cosine_distance(vector_map[int(observation_id)], vector_map[int(candidate_id)]))
                for candidate_id in core_obs_ids
                if int(candidate_id) != int(observation_id)
            ),
            key=lambda item: (float(item[1]), int(item[0])),
        )
        conflicts = {
            int(candidate_id)
            for candidate_id in core_obs_ids
            if int(candidate_id) != int(observation_id)
            and int(photo_map.get(int(candidate_id), -1)) == int(photo_map.get(int(observation_id), -2))
        }
        effective = [
            int(candidate_id)
            for candidate_id, _ in neighbors
            if int(candidate_id) not in conflicts
        ]
        effective_count = min(int(knn_k), len(effective))
        denominator = int(effective_count)
        if cap_by_cluster_size:
            denominator = min(int(effective_count), max(1, len(cluster_member_ids)))
        cluster_neighbor_count = sum(
            1
            for candidate_id in effective[: int(knn_k)]
            if int(candidate_id) in cluster_member_ids
        )
        return float(cluster_neighbor_count) / float(max(1, denominator))

    def _density_radius(
        self,
        *,
        observation_id: int,
        retained_ids: list[int],
        vector_map: dict[int, np.ndarray],
        min_samples: int,
    ) -> float:
        if int(observation_id) not in retained_ids or len(retained_ids) <= 1:
            return 0.0
        distances = sorted(
            self._cosine_distance(vector_map[int(observation_id)], vector_map[int(other_id)])
            for other_id in retained_ids
            if int(other_id) != int(observation_id)
        )
        if not distances:
            return 0.0
        index = min(max(0, int(min_samples) - 1), len(distances) - 1)
        return float(distances[index])

    def _existence_reason(self, *, metrics: dict[str, float | int], profile: dict[str, Any]) -> str | None:
        if int(metrics["retained_member_count"]) < int(profile["existence_min_retained_count"]):
            return "retained_too_small"
        if int(metrics["anchor_core_count"]) < int(profile["existence_min_anchor_core_count"]):
            return "anchor_core_insufficient"
        if int(metrics["distinct_photo_count"]) < int(profile["existence_min_distinct_photo_count"]):
            return "distinct_photo_insufficient"
        if float(metrics["support_ratio_p50"]) < float(profile["existence_min_support_ratio_p50"]):
            return "support_ratio_too_low"
        if float(metrics["intra_photo_conflict_ratio_pre"]) > float(profile["existence_max_intra_photo_conflict_ratio"]):
            return "intra_photo_conflict_too_high"
        return None

    def _intra_photo_conflict_ratio(self, *, observation_ids: list[int], photo_map: dict[int, int]) -> float:
        if len(observation_ids) < 2:
            return 0.0
        conflict_pairs = 0
        total_pairs = len(observation_ids) * (len(observation_ids) - 1) // 2
        for i in range(len(observation_ids)):
            for j in range(i + 1, len(observation_ids)):
                if int(photo_map.get(int(observation_ids[i]), -1)) == int(photo_map.get(int(observation_ids[j]), -2)):
                    conflict_pairs += 1
        return float(conflict_pairs) / float(total_pairs)

    def _farthest_pair(
        self,
        *,
        members: list[int],
        vector_map: dict[int, np.ndarray],
    ) -> tuple[int | None, int | None, float]:
        if len(members) < 2:
            return None, None, 0.0
        best_a: int | None = None
        best_b: int | None = None
        best_dist = -1.0
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a = int(members[i])
                b = int(members[j])
                dist = self._cosine_distance(vector_map[a], vector_map[b])
                if dist > best_dist:
                    best_a = a
                    best_b = b
                    best_dist = float(dist)
        return best_a, best_b, max(0.0, float(best_dist))

    def _medoid_id(self, *, obs_ids: list[int], vector_map: dict[int, np.ndarray]) -> int:
        if not obs_ids:
            raise ValueError("medoid 计算需要至少一个 observation")
        best_obs = int(obs_ids[0])
        best_sum = math.inf
        for obs_id in obs_ids:
            dist_sum = 0.0
            for other_id in obs_ids:
                dist_sum += self._cosine_distance(
                    vector_map[int(obs_id)],
                    vector_map[int(other_id)],
                )
            if dist_sum < best_sum or (
                math.isclose(dist_sum, best_sum, rel_tol=1e-9, abs_tol=1e-9)
                and int(obs_id) < int(best_obs)
            ):
                best_sum = float(dist_sum)
                best_obs = int(obs_id)
        return int(best_obs)

    def _quantile(self, values: list[float], q: float) -> float:
        if not values:
            return 0.0
        sorted_values = sorted(float(item) for item in values)
        q = min(1.0, max(0.0, float(q)))
        if len(sorted_values) == 1:
            return float(sorted_values[0])
        pos = q * float(len(sorted_values) - 1)
        low = int(math.floor(pos))
        high = int(math.ceil(pos))
        if low == high:
            return float(sorted_values[low])
        fraction = pos - float(low)
        return float(sorted_values[low] + (sorted_values[high] - sorted_values[low]) * fraction)

    def _cosine_distance(self, vector_a: np.ndarray, vector_b: np.ndarray) -> float:
        denom = float(np.linalg.norm(vector_a) * np.linalg.norm(vector_b))
        if denom <= 0.0:
            return 1.0
        score = float(np.dot(vector_a, vector_b) / denom)
        score = min(1.0, max(-1.0, score))
        return float(max(0.0, 1.0 - score))
