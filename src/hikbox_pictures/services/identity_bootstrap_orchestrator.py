from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from hikbox_pictures.ann import AnnIndexStore
from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.db.migrator import apply_migrations
from hikbox_pictures.repositories.identity_cluster_run_repo import IdentityClusterRunRepo
from hikbox_pictures.repositories.identity_observation_repo import IdentityObservationRepo
from hikbox_pictures.repositories.identity_publish_repo import IdentityPublishRepo
from hikbox_pictures.repositories.person_repo import PersonRepo
from hikbox_pictures.services.identity_cluster_prepare_service import IdentityClusterPrepareService
from hikbox_pictures.services.identity_cluster_profile_service import IdentityClusterProfileService
from hikbox_pictures.services.identity_cluster_run_service import IdentityClusterRunService
from hikbox_pictures.services.identity_observation_profile_service import IdentityObservationProfileService
from hikbox_pictures.services.identity_observation_snapshot_service import IdentityObservationSnapshotService
from hikbox_pictures.services.identity_run_activation_service import IdentityRunActivationService
from hikbox_pictures.services.observation_quality_backfill_service import ObservationQualityBackfillService
from hikbox_pictures.services.prototype_service import PrototypeService
from hikbox_pictures.workspace import load_workspace_paths


class IdentityBootstrapOrchestrator:
    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.paths = load_workspace_paths(self.workspace)
        self.conn = connect_db(self.paths.db_path)
        apply_migrations(self.conn)

    def close(self) -> None:
        self.conn.close()

    def build_snapshot(
        self,
        *,
        observation_profile_id: int | None,
        candidate_knn_limit: int,
        progress_reporter: Callable[[dict[str, object]], None] | None = None,
    ) -> dict[str, Any]:
        resolved_profile_id = int(observation_profile_id) if observation_profile_id is not None else int(
            IdentityObservationProfileService(self.conn).get_active_profile_id()
        )
        snapshot = IdentityObservationSnapshotService(
            self.conn,
            observation_repo=IdentityObservationRepo(self.conn),
            quality_backfill_service=ObservationQualityBackfillService(self.conn),
        ).build_snapshot(
            observation_profile_id=resolved_profile_id,
            candidate_knn_limit=int(candidate_knn_limit),
            progress_reporter=progress_reporter,
        )
        return {
            **dict(snapshot),
            "observation_profile_id": int(resolved_profile_id),
            "candidate_knn_limit": int(candidate_knn_limit),
        }

    def rerun_cluster_run(
        self,
        *,
        snapshot_id: int,
        cluster_profile_id: int | None,
        supersedes_run_id: int | None,
        select_as_review_target: bool,
        progress_reporter: Callable[[dict[str, object]], None] | None = None,
    ) -> dict[str, Any]:
        resolved_profile_id = int(cluster_profile_id) if cluster_profile_id is not None else int(
            IdentityClusterProfileService(self.conn).get_active_profile_id()
        )
        run_result = IdentityClusterRunService(
            self.conn,
            cluster_run_repo=IdentityClusterRunRepo(self.conn),
        ).execute_run(
            observation_snapshot_id=int(snapshot_id),
            cluster_profile_id=resolved_profile_id,
            supersedes_run_id=int(supersedes_run_id) if supersedes_run_id is not None else None,
            select_as_review_target=bool(select_as_review_target),
            progress_reporter=progress_reporter,
        )
        run_id = int(run_result["run_id"])

        prepare_result = self._new_prepare_service().prepare_run(
            run_id=run_id,
            progress_reporter=progress_reporter,
        )
        return {
            "snapshot_id": int(snapshot_id),
            "cluster_profile_id": int(resolved_profile_id),
            "run_id": run_id,
            "run_status": str(run_result.get("run_status") or "unknown"),
            "prepared_cluster_count": int(prepare_result.get("prepared_cluster_count") or 0),
            "candidate_cluster_count": int(prepare_result.get("candidate_cluster_count") or 0),
            "summary": dict(run_result.get("summary") or {}),
        }

    def select_review_target(self, *, run_id: int) -> dict[str, Any]:
        IdentityClusterRunService(
            self.conn,
            cluster_run_repo=IdentityClusterRunRepo(self.conn),
        ).select_review_target(run_id=int(run_id))
        return {"run_id": int(run_id), "selected": True}

    def activate_run(self, *, run_id: int) -> dict[str, Any]:
        self._new_activation_service().activate_run(run_id=int(run_id))
        return {"run_id": int(run_id), "activated": True}

    def _new_prepare_service(self) -> IdentityClusterPrepareService:
        person_repo = PersonRepo(self.conn)
        ann_store = AnnIndexStore(self.paths.artifacts_dir / "ann" / "prototype_index.npz")
        prototype_service = PrototypeService(self.conn, person_repo=person_repo, ann_index_store=ann_store)
        publish_repo = IdentityPublishRepo(
            self.conn,
            artifact_root=self.paths.artifacts_dir / "identity_prepare",
            live_ann_artifact_path=self.paths.artifacts_dir / "ann" / "prototype_index.npz",
        )
        return IdentityClusterPrepareService(
            self.conn,
            publish_repo=publish_repo,
            person_repo=person_repo,
            prototype_service=prototype_service,
            ann_index_store=ann_store,
        )

    def _new_activation_service(self) -> IdentityRunActivationService:
        person_repo = PersonRepo(self.conn)
        ann_store = AnnIndexStore(self.paths.artifacts_dir / "ann" / "prototype_index.npz")
        prototype_service = PrototypeService(self.conn, person_repo=person_repo, ann_index_store=ann_store)
        publish_repo = IdentityPublishRepo(
            self.conn,
            artifact_root=self.paths.artifacts_dir / "identity_prepare",
            live_ann_artifact_path=self.paths.artifacts_dir / "ann" / "prototype_index.npz",
        )
        return IdentityRunActivationService(
            self.conn,
            publish_repo=publish_repo,
            person_repo=person_repo,
            prototype_service=prototype_service,
            ann_index_store=ann_store,
        )
