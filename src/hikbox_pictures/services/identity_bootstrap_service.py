from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import numpy as np

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.repositories.identity_repo import IdentityRepo
from hikbox_pictures.repositories.person_repo import PersonRepo
from hikbox_pictures.services.prototype_service import PrototypeService


@dataclass
class _Observation:
    observation_id: int
    photo_asset_id: int
    quality_score: float
    vector: np.ndarray[Any, np.dtype[np.float32]]


class IdentityBootstrapService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        identity_repo: IdentityRepo,
        person_repo: PersonRepo,
        prototype_service: PrototypeService,
    ) -> None:
        self.conn = conn
        self.identity_repo = identity_repo
        self.person_repo = person_repo
        self.prototype_service = prototype_service

    def run_bootstrap(self, *, profile_id: int) -> dict[str, Any]:
        managed_transaction = not self.conn.in_transaction
        try:
            profile = self.identity_repo.get_profile_required(int(profile_id))
            model_key = str(profile["embedding_model_key"])
            observations = self._load_observations(
                model_key=model_key,
                min_quality=float(profile["high_quality_threshold"]),
            )

            batch_id = self.identity_repo.create_bootstrap_batch(
                model_key=model_key,
                threshold_profile_id=int(profile_id),
                algorithm_version="identity.bootstrap.v1",
            )

            if not observations:
                summary = {
                    "materialized_cluster_count": 0,
                    "review_pending_cluster_count": 0,
                    "discarded_cluster_count": 0,
                    "edge_reject_counts": {
                        "not_mutual": 0,
                        "distance_recheck_failed": 0,
                        "photo_conflict": 0,
                    },
                }
                if managed_transaction:
                    self.conn.commit()
                return summary

            accepted_edges, edge_reject_counts = self._build_edges(observations=observations, profile=profile)
            clusters = self._build_clusters(observations=observations, accepted_edges=accepted_edges)

            materialized_cluster_count = 0
            review_pending_cluster_count = 0
            discarded_cluster_count = 0
            for cluster_observations in clusters:
                cluster_id = self._persist_cluster_and_maybe_materialize(
                    batch_id=int(batch_id),
                    profile=profile,
                    cluster_observations=cluster_observations,
                    edge_reject_counts=edge_reject_counts,
                )
                status = self.identity_repo.get_cluster_status(int(cluster_id))
                if status == "materialized":
                    materialized_cluster_count += 1
                elif status == "discarded":
                    discarded_cluster_count += 1
                else:
                    review_pending_cluster_count += 1

            if managed_transaction:
                self.conn.commit()
            return {
                "materialized_cluster_count": int(materialized_cluster_count),
                "review_pending_cluster_count": int(review_pending_cluster_count),
                "discarded_cluster_count": int(discarded_cluster_count),
                "edge_reject_counts": dict(edge_reject_counts),
            }
        except Exception:
            if managed_transaction and self.conn.in_transaction:
                self.conn.rollback()
            raise

    def _load_observations(self, *, model_key: str, min_quality: float) -> list[_Observation]:
        rows = self.identity_repo.list_high_quality_observations(
            model_key=model_key,
            min_quality=float(min_quality),
        )
        result: list[_Observation] = []
        expected_dim: int | None = None
        for row in rows:
            vector_blob = row.get("vector_blob")
            if not isinstance(vector_blob, (bytes, bytearray, memoryview)):
                continue
            vector = np.frombuffer(vector_blob, dtype=np.float32).copy()
            if vector.ndim != 1 or int(vector.size) == 0:
                continue
            if expected_dim is None:
                expected_dim = int(vector.size)
            elif int(vector.size) != expected_dim:
                continue
            result.append(
                _Observation(
                    observation_id=int(row["observation_id"]),
                    photo_asset_id=int(row["photo_asset_id"]),
                    quality_score=float(row["quality_score"]),
                    vector=vector.astype(np.float32, copy=False),
                )
            )
        return result

    def _build_edges(
        self,
        *,
        observations: list[_Observation],
        profile: dict[str, Any],
    ) -> tuple[list[tuple[int, int]], dict[str, int]]:
        n = len(observations)
        if n <= 1:
            return [], {
                "not_mutual": 0,
                "distance_recheck_failed": 0,
                "photo_conflict": 0,
            }

        candidate_threshold = float(profile["bootstrap_edge_candidate_threshold"])
        accept_threshold = float(profile["bootstrap_edge_accept_threshold"])
        margin_threshold = float(profile["bootstrap_margin_threshold"])

        distances = np.full((n, n), np.inf, dtype=np.float64)
        for i in range(n):
            for j in range(i + 1, n):
                distance = float(np.linalg.norm(observations[i].vector - observations[j].vector))
                distances[i, j] = distance
                distances[j, i] = distance

        top_k = 2
        near_neighbors: dict[int, set[int]] = {}
        margin_values: dict[int, float] = {}
        for i in range(n):
            ordered = sorted(((distances[i, j], j) for j in range(n) if j != i), key=lambda item: item[0])
            candidate_neighbors = [int(j) for d, j in ordered if float(d) <= candidate_threshold][:top_k]
            near_neighbors[i] = set(candidate_neighbors)
            if not ordered:
                margin_values[i] = 0.0
            else:
                best = float(ordered[0][0])
                second = float(ordered[1][0]) if len(ordered) > 1 else best
                margin_values[i] = max(0.0, second - best)

        reject_counts = {
            "not_mutual": 0,
            "distance_recheck_failed": 0,
            "photo_conflict": 0,
        }
        accepted_edges: list[tuple[int, int]] = []
        for i in range(n):
            for j in range(i + 1, n):
                d = float(distances[i, j])
                if d > candidate_threshold:
                    continue
                if j not in near_neighbors.get(i, set()) or i not in near_neighbors.get(j, set()):
                    reject_counts["not_mutual"] += 1
                    continue
                if d > accept_threshold or margin_values.get(i, 0.0) < margin_threshold or margin_values.get(j, 0.0) < margin_threshold:
                    reject_counts["distance_recheck_failed"] += 1
                    continue
                if observations[i].photo_asset_id == observations[j].photo_asset_id:
                    reject_counts["photo_conflict"] += 1
                    continue
                accepted_edges.append((i, j))

        return accepted_edges, reject_counts

    def _build_clusters(
        self,
        *,
        observations: list[_Observation],
        accepted_edges: list[tuple[int, int]],
    ) -> list[list[_Observation]]:
        n = len(observations)
        if n == 0:
            return []

        parents = list(range(n))

        def find(x: int) -> int:
            while parents[x] != x:
                parents[x] = parents[parents[x]]
                x = parents[x]
            return x

        def union(a: int, b: int) -> None:
            ra = find(a)
            rb = find(b)
            if ra != rb:
                parents[rb] = ra

        for a, b in accepted_edges:
            union(int(a), int(b))

        grouped: dict[int, list[_Observation]] = {}
        for idx, obs in enumerate(observations):
            root = find(idx)
            grouped.setdefault(root, []).append(obs)

        return sorted(grouped.values(), key=lambda group: min(item.observation_id for item in group))

    def _persist_cluster_and_maybe_materialize(
        self,
        *,
        batch_id: int,
        profile: dict[str, Any],
        cluster_observations: list[_Observation],
        edge_reject_counts: dict[str, int],
    ) -> int:
        member_count = len(cluster_observations)
        distinct_photo_count = len({item.photo_asset_id for item in cluster_observations})
        high_quality_count = sum(1 for item in cluster_observations if item.quality_score >= float(profile["high_quality_threshold"]))

        seed_candidates = [
            item
            for item in sorted(cluster_observations, key=lambda item: item.quality_score, reverse=True)
            if item.quality_score >= float(profile["trusted_seed_quality_threshold"])
        ]
        pre_dedup_seed_candidate_count = len(seed_candidates)
        seed_candidates = seed_candidates[: int(profile["bootstrap_seed_max_count"])]

        dedup_drop_counts = {
            "exact_duplicate": 0,
            "burst_duplicate": 0,
        }
        selected_seeds: list[_Observation] = []
        used_photo_ids: set[int] = set()
        used_vector_keys: set[bytes] = set()
        for item in seed_candidates:
            vector_key = item.vector.tobytes()
            if vector_key in used_vector_keys:
                dedup_drop_counts["exact_duplicate"] += 1
                continue
            if item.photo_asset_id in used_photo_ids:
                dedup_drop_counts["burst_duplicate"] += 1
                continue
            used_vector_keys.add(vector_key)
            used_photo_ids.add(item.photo_asset_id)
            selected_seeds.append(item)

        reject_reason: str | None = None
        if member_count < int(profile["bootstrap_min_cluster_size"]):
            reject_reason = "cluster_too_small"
        elif distinct_photo_count < int(profile["bootstrap_min_distinct_photo_count"]):
            reject_reason = "distinct_photo_count_insufficient"
        elif high_quality_count < int(profile["bootstrap_min_high_quality_count"]):
            reject_reason = "high_quality_count_insufficient"
        elif len(selected_seeds) < int(profile["bootstrap_seed_min_count"]):
            reject_reason = "seed_insufficient_after_dedup"

        cluster_status = "review_pending"
        if reject_reason == "cluster_too_small":
            cluster_status = "discarded"
        elif reject_reason is None:
            cluster_status = "materialized"

        decision_kind = "review_pending"
        if cluster_status == "discarded":
            decision_kind = "discarded"
        elif reject_reason is None:
            decision_kind = "candidate_materialize"

        diagnostic = {
            "cluster_size": member_count,
            "distinct_photo_count": distinct_photo_count,
            "selected_seed_count": len(selected_seeds),
            "pre_dedup_seed_candidate_count": pre_dedup_seed_candidate_count,
            "quality_distribution": {
                "min": min((item.quality_score for item in cluster_observations), default=0.0),
                "max": max((item.quality_score for item in cluster_observations), default=0.0),
                "avg": (
                    sum(item.quality_score for item in cluster_observations) / member_count
                    if member_count > 0
                    else 0.0
                ),
            },
            "external_margin": float(profile["bootstrap_margin_threshold"]),
            "edge_reject_counts": dict(edge_reject_counts),
            "dedup_drop_counts": dict(dedup_drop_counts),
            "reject_reason": reject_reason,
            "decision_kind": decision_kind,
        }

        representative_observation_id = max(
            cluster_observations,
            key=lambda item: (item.quality_score, -item.observation_id),
        ).observation_id
        cluster_id = self.identity_repo.create_cluster(
            batch_id=int(batch_id),
            representative_observation_id=int(representative_observation_id),
            cluster_status=cluster_status,
            resolved_person_id=None,
            diagnostic_json=json.dumps(diagnostic, ensure_ascii=False, sort_keys=True),
        )
        seed_observation_ids = {item.observation_id for item in selected_seeds}
        for member in cluster_observations:
            self.identity_repo.add_cluster_member(
                cluster_id=int(cluster_id),
                face_observation_id=int(member.observation_id),
                membership_score=None,
                quality_score_snapshot=float(member.quality_score),
                is_seed_candidate=int(member.observation_id) in seed_observation_ids,
            )

        if reject_reason is not None:
            return int(cluster_id)

        self.conn.execute("SAVEPOINT bootstrap_cluster_finalize")
        try:
            person_id = self._materialize_cluster(
                cluster_id=int(cluster_id),
                profile_id=int(profile["id"]),
                model_key=str(profile["embedding_model_key"]),
                members=cluster_observations,
                seeds=selected_seeds,
            )

            finalized = dict(diagnostic)
            finalized["reject_reason"] = None
            finalized["decision_kind"] = "materialized"
            self.identity_repo.update_cluster_resolution(
                cluster_id=int(cluster_id),
                cluster_status="materialized",
                resolved_person_id=int(person_id),
                diagnostic_json=json.dumps(finalized, ensure_ascii=False, sort_keys=True),
            )
            self.conn.execute("RELEASE SAVEPOINT bootstrap_cluster_finalize")
            return int(cluster_id)
        except Exception:
            self.conn.execute("ROLLBACK TO SAVEPOINT bootstrap_cluster_finalize")
            self.conn.execute("RELEASE SAVEPOINT bootstrap_cluster_finalize")
            fallback = dict(diagnostic)
            fallback["reject_reason"] = "artifact_rebuild_failed"
            fallback["decision_kind"] = "review_pending"
            self.identity_repo.update_cluster_resolution(
                cluster_id=int(cluster_id),
                cluster_status="review_pending",
                resolved_person_id=None,
                diagnostic_json=json.dumps(fallback, ensure_ascii=False, sort_keys=True),
            )
            return int(cluster_id)

    def _materialize_cluster(
        self,
        *,
        cluster_id: int,
        profile_id: int,
        model_key: str,
        members: list[_Observation],
        seeds: list[_Observation],
    ) -> int:
        self.conn.execute("SAVEPOINT bootstrap_materialize")
        try:
            person_id = self.person_repo.create_anonymous_person(
                origin_cluster_id=int(cluster_id),
                sequence=self.person_repo.next_anonymous_sequence(),
            )
            cover_observation_id = max(members, key=lambda item: item.quality_score).observation_id
            self.person_repo.set_cover_observation(
                person_id=int(person_id),
                cover_observation_id=int(cover_observation_id),
            )

            assignment_diagnostic = json.dumps(
                {
                    "decision_kind": "bootstrap_materialize",
                    "auto_cluster_id": int(cluster_id),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            for member in members:
                self.person_repo.create_bootstrap_assignment(
                    person_id=int(person_id),
                    face_observation_id=int(member.observation_id),
                    threshold_profile_id=int(profile_id),
                    diagnostic_json=assignment_diagnostic,
                )

            for seed in seeds:
                self.person_repo.create_trusted_sample(
                    person_id=int(person_id),
                    face_observation_id=int(seed.observation_id),
                    trust_source="bootstrap_seed",
                    trust_score=1.0,
                    quality_score_snapshot=float(seed.quality_score),
                    threshold_profile_id=int(profile_id),
                    source_auto_cluster_id=int(cluster_id),
                )

            self.prototype_service.rebuild_person_prototype(
                person_id=int(person_id),
                model_key=model_key,
            )
            self.prototype_service.sync_person_ann_entry(
                person_id=int(person_id),
                model_key=model_key,
            )

            self.conn.execute("RELEASE SAVEPOINT bootstrap_materialize")
            return int(person_id)
        except Exception:
            self.conn.execute("ROLLBACK TO SAVEPOINT bootstrap_materialize")
            self.conn.execute("RELEASE SAVEPOINT bootstrap_materialize")
            raise
