from __future__ import annotations

from typing import Any

from hikbox_pictures.repositories.identity_observation_repo import IdentityObservationRepo


ALGORITHM_VERSION = "identity.observation_snapshot.v1"


class IdentityObservationSnapshotService:
    def __init__(
        self,
        conn,
        *,
        observation_repo: IdentityObservationRepo,
        quality_backfill_service,
    ) -> None:
        self.conn = conn
        self.observation_repo = observation_repo
        self.quality_backfill_service = quality_backfill_service

    def build_snapshot(
        self,
        *,
        observation_profile_id: int,
        candidate_knn_limit: int,
    ) -> dict[str, Any]:
        profile = self.observation_repo.get_observation_profile_required(observation_profile_id)
        self.quality_backfill_service.backfill_all_observations(
            profile_id=observation_profile_id,
            update_profile_quantiles=False,
            allow_legacy_profile=False,
        )
        dataset_hash = self.observation_repo.compute_observation_dataset_hash(
            model_key=str(profile["embedding_model_key"])
        )
        candidate_policy_hash = self.observation_repo.compute_candidate_policy_hash(
            profile_id=observation_profile_id,
            candidate_knn_limit=candidate_knn_limit,
        )
        reusable = self.observation_repo.find_reusable_snapshot(
            observation_profile_id=observation_profile_id,
            dataset_hash=dataset_hash,
            candidate_policy_hash=candidate_policy_hash,
            required_knn_limit=candidate_knn_limit,
            algorithm_version=ALGORITHM_VERSION,
        )
        if reusable is not None:
            return {
                "snapshot_id": int(reusable["id"]),
                "reused": True,
                "pool_counts": dict(reusable["pool_counts"]),
            }

        snapshot_id = self.observation_repo.create_snapshot(
            observation_profile_id=observation_profile_id,
            dataset_hash=dataset_hash,
            candidate_policy_hash=candidate_policy_hash,
            max_knn_supported=candidate_knn_limit,
            algorithm_version=ALGORITHM_VERSION,
        )
        self.conn.commit()
        try:
            pool_counts = self.observation_repo.populate_snapshot_entries(
                snapshot_id=snapshot_id,
                observation_profile_id=observation_profile_id,
            )
            self.conn.commit()
        except Exception as exc:
            self.conn.rollback()
            self.observation_repo.mark_snapshot_failed(
                snapshot_id=snapshot_id,
                reason=str(exc),
            )
            self.conn.commit()
            raise
        return {
            "snapshot_id": int(snapshot_id),
            "reused": False,
            "pool_counts": dict(pool_counts),
        }
