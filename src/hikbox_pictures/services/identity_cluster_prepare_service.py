from __future__ import annotations

from collections.abc import Callable
from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.ann import AnnIndexStore
from hikbox_pictures.repositories.identity_publish_repo import IdentityPublishRepo
from hikbox_pictures.repositories.person_repo import PersonRepo
from hikbox_pictures.services.prototype_service import PrototypeService


class IdentityClusterPrepareService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        publish_repo: IdentityPublishRepo,
        person_repo: PersonRepo,
        prototype_service: PrototypeService,
        ann_index_store: AnnIndexStore,
    ) -> None:
        self.conn = conn
        self.publish_repo = publish_repo
        self.person_repo = person_repo
        self.prototype_service = prototype_service
        self.ann_index_store = ann_index_store

    def prepare_run(
        self,
        *,
        run_id: int,
        progress_reporter: Callable[[dict[str, object]], None] | None = None,
    ) -> dict[str, Any]:
        run = self.publish_repo.get_run_required(int(run_id))
        if str(run.get("run_status")) != "succeeded":
            raise ValueError(f"仅允许 prepare succeeded run: {int(run_id)}")

        profile = self.publish_repo.get_cluster_profile_for_run(run_id=int(run_id))
        prepared_cluster_ids: list[int] = []
        candidate_cluster_ids: list[int] = []
        prepare_candidates = list(self.publish_repo.list_prepare_candidates(run_id=int(run_id)))

        managed_transaction = not self.conn.in_transaction
        if managed_transaction:
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            for index, candidate in enumerate(prepare_candidates, start=1):
                cluster_id = int(candidate["cluster_id"])
                candidate_cluster_ids.append(cluster_id)
                gate_reason = self.publish_repo.materialize_gate_reason(candidate=candidate, profile=profile)
                if gate_reason is not None:
                    self.publish_repo.mark_cluster_review_pending(
                        cluster_id=cluster_id,
                        reason=str(gate_reason),
                    )
                else:
                    manifest = self.publish_repo.prepare_cluster_bundle(
                        cluster_id=cluster_id,
                        run_id=int(run_id),
                    )
                    if not self.publish_repo.verify_cluster_bundle_manifest(manifest):
                        self.publish_repo.mark_cluster_review_pending(
                            cluster_id=cluster_id,
                            reason="cluster_bundle_incomplete_or_checksum_mismatch",
                        )
                    else:
                        prepared_cluster_ids.append(cluster_id)
                self._report_progress(
                    progress_reporter,
                    subphase="prepare_candidates",
                    total_count=len(prepare_candidates),
                    completed_count=index,
                )

            ann_manifest = self.publish_repo.prepare_run_ann_bundle(
                run_id=int(run_id),
                prepared_cluster_ids=prepared_cluster_ids,
            )
            self._report_progress(
                progress_reporter,
                subphase="prepare_run_ann_bundle",
                total_count=1,
                completed_count=1,
            )
            if not self.publish_repo.verify_run_ann_manifest(ann_manifest):
                self.publish_repo.mark_run_prepare_failed_and_rollback_candidates(
                    run_id=int(run_id),
                    candidate_cluster_ids=candidate_cluster_ids,
                    reason="run_ann_bundle_failed_or_checksum_mismatch",
                )
                if managed_transaction:
                    self.conn.commit()
                return {
                    "prepared_cluster_count": 0,
                    "candidate_cluster_count": int(len(candidate_cluster_ids)),
                }

            self.publish_repo.mark_run_prepared(
                run_id=int(run_id),
                cluster_ids=prepared_cluster_ids,
                ann_manifest=ann_manifest,
            )
            if managed_transaction:
                self.conn.commit()
            return {
                "prepared_cluster_count": int(len(prepared_cluster_ids)),
                "candidate_cluster_count": int(len(candidate_cluster_ids)),
            }
        except Exception:
            if managed_transaction and self.conn.in_transaction:
                self.conn.rollback()
            raise

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
                "phase": "prepare_run",
                "subphase": str(subphase),
                "status": "running",
                "total_count": total,
                "completed_count": completed,
                "percent": percent,
            }
        )
