from __future__ import annotations

from dataclasses import replace

import numpy as np

from .models import (
    AssignParameters,
    AssignmentEvaluation,
    AssignmentRecord,
    AssignmentSummary,
    ClusterMemberRecord,
    ClusterRecord,
    QueryContext,
    SeedBuildResult,
    SeedIdentityRecord,
    TopCandidateRecord,
)


class IdentityV31AssignmentService:
    def build_seed_identities(
        self,
        *,
        clusters: list[ClusterRecord],
        promote_cluster_ids: set[int],
        disable_seed_cluster_ids: set[int],
    ) -> SeedBuildResult:
        clusters_by_id = {cluster.cluster_id: cluster for cluster in clusters}

        for cluster_id in promote_cluster_ids:
            if cluster_id not in clusters_by_id:
                raise ValueError(f"promote cluster 不存在: {cluster_id}")

        for cluster_id in promote_cluster_ids:
            cluster = clusters_by_id[cluster_id]
            if cluster.cluster_stage != "final" or cluster.resolution_state != "review_pending":
                raise ValueError(f"只能 promote review_pending final cluster: {cluster_id}")

        default_seed_cluster_ids = {
            cluster.cluster_id
            for cluster in clusters
            if cluster.cluster_stage == "final"
            and cluster.cluster_state == "active"
            and cluster.resolution_state == "materialized"
        }
        enabled_seed_cluster_ids = (default_seed_cluster_ids | set(promote_cluster_ids)).copy()

        unknown_disable_ids = sorted(set(disable_seed_cluster_ids) - enabled_seed_cluster_ids)
        if unknown_disable_ids:
            raise ValueError(f"disable 目标不是启用 seed cluster: {unknown_disable_ids[0]}")

        enabled_seed_cluster_ids -= set(disable_seed_cluster_ids)
        if not enabled_seed_cluster_ids:
            raise ValueError("没有任何可用 seed identity")

        valid_seeds_by_cluster: dict[int, SeedIdentityRecord] = {}
        invalid_seeds: list[SeedIdentityRecord] = []
        errors: list[dict[str, object]] = []
        prototype_dimension: int | None = None

        for cluster_id in sorted(enabled_seed_cluster_ids):
            cluster = clusters_by_id[cluster_id]
            seed_record, error_message = self._build_seed_record(cluster=cluster)
            if seed_record is None:
                invalid_seed = SeedIdentityRecord(
                    identity_id=f"seed-cluster-{cluster_id}",
                    source_cluster_id=cluster_id,
                    resolution_state=cluster.resolution_state,
                    seed_member_count=0,
                    fallback_used=False,
                    prototype_dimension=None,
                    representative_observation_id=cluster.representative_observation_id,
                    member_observation_ids=[],
                    valid=False,
                    error_code="invalid_seed_prototype",
                    error_message=error_message,
                    prototype_vector=None,
                )
                invalid_seeds.append(invalid_seed)
                errors.append(
                    {
                        "code": "invalid_seed_prototype",
                        "cluster_id": cluster_id,
                        "message": error_message,
                    }
                )
                continue

            if prototype_dimension is None:
                prototype_dimension = seed_record.prototype_dimension
                valid_seeds_by_cluster[cluster_id] = seed_record
                continue

            if seed_record.prototype_dimension != prototype_dimension:
                message = (
                    f"seed prototype 维度不一致: expected={prototype_dimension}, "
                    f"actual={seed_record.prototype_dimension}"
                )
                invalid_seed = replace(
                    seed_record,
                    valid=False,
                    error_code="invalid_seed_prototype",
                    error_message=message,
                    prototype_dimension=None,
                    prototype_vector=None,
                )
                invalid_seeds.append(invalid_seed)
                errors.append(
                    {
                        "code": "invalid_seed_prototype",
                        "cluster_id": cluster_id,
                        "message": message,
                    }
                )
                continue

            valid_seeds_by_cluster[cluster_id] = seed_record

        if enabled_seed_cluster_ids and not valid_seeds_by_cluster:
            raise ValueError("没有任何可用 seed identity")

        return SeedBuildResult(
            valid_seeds_by_cluster=valid_seeds_by_cluster,
            invalid_seeds=invalid_seeds,
            errors=errors,
            prototype_dimension=prototype_dimension,
        )

    def evaluate_assignments(
        self,
        *,
        query_context: QueryContext,
        seed_result: SeedBuildResult,
        assign_parameters: AssignParameters,
    ) -> AssignmentEvaluation:
        assign_parameters = assign_parameters.validate()

        seed_cluster_ids = sorted(seed_result.valid_seeds_by_cluster)
        valid_seed_count = len(seed_cluster_ids)
        enabled_seed_cluster_ids = set(seed_cluster_ids)
        enabled_seed_cluster_ids.update(item.source_cluster_id for item in seed_result.invalid_seeds)

        excluded_seed_observation_ids: set[int] = set()
        for cluster_id in enabled_seed_cluster_ids:
            excluded_seed_observation_ids.update(
                query_context.non_rejected_member_observation_ids_by_cluster.get(cluster_id, set())
            )

        filtered_candidates = [
            item
            for item in query_context.candidate_observations
            if item.observation_id not in excluded_seed_observation_ids
        ]

        if filtered_candidates and all(self._candidate_embedding_missing(item) for item in filtered_candidates):
            raise ValueError("所有候选 observation 都缺少可用 embedding")

        prototype_dimension = seed_result.prototype_dimension

        prototype_matrix: np.ndarray | None = None
        cluster_id_array: np.ndarray | None = None
        identity_ids: list[str] = []
        if valid_seed_count > 0:
            prototype_matrix = np.asarray(
                [seed_result.valid_seeds_by_cluster[cluster_id].prototype_vector for cluster_id in seed_cluster_ids],
                dtype=np.float64,
            )
            cluster_id_array = np.asarray(seed_cluster_ids, dtype=np.int64)
            identity_ids = [seed_result.valid_seeds_by_cluster[cluster_id].identity_id for cluster_id in seed_cluster_ids]

        assignments: list[AssignmentRecord] = []
        by_observation_id: dict[int, AssignmentRecord] = {}

        auto_assign_count = 0
        review_count = 0
        reject_count = 0
        same_photo_conflict_count = 0
        missing_embedding_count = 0
        dimension_mismatch_count = 0

        for candidate in filtered_candidates:
            if self._candidate_embedding_missing(candidate):
                missing_embedding_count += 1
                continue

            if prototype_dimension is not None and candidate.embedding_dim != prototype_dimension:
                dimension_mismatch_count += 1
                continue

            best_identity_id: str | None = None
            best_cluster_id: int | None = None
            best_distance: float | None = None
            second_best_distance: float | None = None
            distance_margin: float | None = None
            same_photo_conflict = False
            top_candidates: list[TopCandidateRecord] = []

            if valid_seed_count == 0:
                decision = "reject"
                reason_code = "no_seed_candidates"
            else:
                assert prototype_matrix is not None
                assert cluster_id_array is not None
                query_vector = np.asarray(candidate.embedding_vector, dtype=np.float64)
                delta = prototype_matrix - query_vector.reshape(1, -1)
                distances = np.linalg.norm(delta, axis=1)
                order = np.lexsort((cluster_id_array, distances))
                top_n = min(assign_parameters.top_k, valid_seed_count)

                for rank, index in enumerate(order[:top_n], start=1):
                    top_candidates.append(
                        TopCandidateRecord(
                            rank=rank,
                            identity_id=identity_ids[int(index)],
                            cluster_id=int(cluster_id_array[int(index)]),
                            distance=float(distances[int(index)]),
                        )
                    )

                best_index = int(order[0])
                best_identity_id = identity_ids[best_index]
                best_cluster_id = int(cluster_id_array[best_index])
                best_distance = float(distances[best_index])
                if valid_seed_count < 2:
                    second_best_distance = float("inf")
                    distance_margin = float("inf")
                else:
                    second_best_index = int(order[1])
                    second_best_distance = float(distances[second_best_index])
                    distance_margin = float(second_best_distance - best_distance)

                same_photo_conflict = self._has_same_photo_conflict(
                    query_context=query_context,
                    best_cluster_id=best_cluster_id,
                    photo_id=candidate.photo_id,
                )

                if best_distance > assign_parameters.review_max_distance:
                    decision = "reject"
                    reason_code = "distance_above_review_threshold"
                elif same_photo_conflict:
                    decision = "review"
                    reason_code = "same_photo_conflict"
                elif distance_margin < assign_parameters.min_margin:
                    decision = "review"
                    reason_code = "margin_below_threshold"
                elif best_distance > assign_parameters.auto_max_distance:
                    decision = "review"
                    reason_code = "distance_above_auto_threshold"
                else:
                    decision = "auto_assign"
                    reason_code = "auto_threshold_pass"

            if same_photo_conflict:
                same_photo_conflict_count += 1
            if decision == "auto_assign":
                auto_assign_count += 1
            elif decision == "review":
                review_count += 1
            else:
                reject_count += 1

            assets = {
                "crop": candidate.primary_path,
                "context": None,
                "preview": None,
            }
            missing_assets = [key for key, value in assets.items() if value is None]
            assignment = AssignmentRecord(
                observation_id=candidate.observation_id,
                photo_id=candidate.photo_id,
                source_kind=candidate.source_kind,
                source_cluster_id=candidate.source_cluster_id,
                best_identity_id=best_identity_id,
                best_cluster_id=best_cluster_id,
                best_distance=best_distance,
                second_best_distance=second_best_distance,
                distance_margin=distance_margin,
                same_photo_conflict=same_photo_conflict,
                decision=decision,
                reason_code=reason_code,
                top_candidates=top_candidates,
                assets=assets,
                missing_assets=missing_assets,
            )
            assignments.append(assignment)
            by_observation_id[candidate.observation_id] = assignment

        summary = AssignmentSummary(
            candidate_count=len(assignments),
            auto_assign_count=auto_assign_count,
            review_count=review_count,
            reject_count=reject_count,
            same_photo_conflict_count=same_photo_conflict_count,
            missing_embedding_count=missing_embedding_count,
            dimension_mismatch_count=dimension_mismatch_count,
        )
        return AssignmentEvaluation(
            assignments=assignments,
            by_observation_id=by_observation_id,
            excluded_seed_observation_ids=excluded_seed_observation_ids,
            summary=summary,
        )

    def _build_seed_record(self, *, cluster: ClusterRecord) -> tuple[SeedIdentityRecord | None, str | None]:
        trusted_vectors: list[np.ndarray] = []
        trusted_observation_ids: list[int] = []
        fallback_vectors: list[np.ndarray] = []
        fallback_observation_ids: list[int] = []

        for member in cluster.members:
            vector = self._extract_member_embedding(member)
            if vector is None:
                continue
            if member.is_selected_trusted_seed:
                trusted_vectors.append(vector)
                trusted_observation_ids.append(member.observation_id)
            if member.decision_status != "rejected":
                fallback_vectors.append(vector)
                fallback_observation_ids.append(member.observation_id)

        if trusted_vectors:
            selected_vectors = trusted_vectors
            member_observation_ids = trusted_observation_ids
            fallback_used = False
        else:
            selected_vectors = fallback_vectors
            member_observation_ids = fallback_observation_ids
            fallback_used = True

        if not selected_vectors:
            return None, "没有可用的 seed prototype 成员 embedding"

        dimensions = {int(vector.size) for vector in selected_vectors}
        if len(dimensions) != 1:
            return None, "seed prototype 成员 embedding 维度不一致"

        matrix = np.asarray(selected_vectors, dtype=np.float64)
        mean_vector = matrix.mean(axis=0)
        norm = float(np.linalg.norm(mean_vector))
        if norm <= 0.0:
            return None, "seed prototype 均值向量范数为 0"

        normalized = (mean_vector / norm).astype(np.float64)
        prototype_dimension = int(normalized.size)

        return (
            SeedIdentityRecord(
                identity_id=f"seed-cluster-{cluster.cluster_id}",
                source_cluster_id=cluster.cluster_id,
                resolution_state=cluster.resolution_state,
                seed_member_count=len(member_observation_ids),
                fallback_used=fallback_used,
                prototype_dimension=prototype_dimension,
                representative_observation_id=cluster.representative_observation_id,
                member_observation_ids=member_observation_ids,
                valid=True,
                error_code=None,
                error_message=None,
                prototype_vector=normalized.tolist(),
            ),
            None,
        )

    def _extract_member_embedding(self, member: ClusterMemberRecord) -> np.ndarray | None:
        if member.embedding_vector is None or member.embedding_dim is None:
            return None
        vector = np.asarray(member.embedding_vector, dtype=np.float64)
        if vector.ndim != 1:
            return None
        if vector.size != int(member.embedding_dim):
            return None
        return vector

    def _candidate_embedding_missing(self, candidate: object) -> bool:
        embedding_vector = getattr(candidate, "embedding_vector", None)
        embedding_dim = getattr(candidate, "embedding_dim", None)
        if embedding_vector is None or embedding_dim is None:
            return True
        vector = np.asarray(embedding_vector, dtype=np.float64)
        if vector.ndim != 1:
            return True
        return vector.size != int(embedding_dim)

    def _has_same_photo_conflict(
        self,
        *,
        query_context: QueryContext,
        best_cluster_id: int,
        photo_id: int,
    ) -> bool:
        cluster = query_context.clusters_by_id.get(best_cluster_id)
        if cluster is None:
            return False
        photo_ids = {member.photo_id for member in cluster.members if member.decision_status != "rejected"}
        return photo_id in photo_ids
