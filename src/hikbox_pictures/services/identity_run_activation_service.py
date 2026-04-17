from __future__ import annotations

from pathlib import Path

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.ann import AnnIndexStore
from hikbox_pictures.repositories.identity_publish_repo import IdentityPublishRepo
from hikbox_pictures.repositories.person_repo import PersonRepo
from hikbox_pictures.services.prototype_service import PrototypeService


class IdentityRunActivationService:
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

    def activate_run(self, *, run_id: int) -> None:
        prepared = self.publish_repo.get_prepared_run_required_with_verified_manifest(int(run_id))
        previous_owner = self.publish_repo.get_materialization_owner()
        previous_owner_run_id = int(previous_owner["id"]) if previous_owner is not None else None
        previous_owner_live_snapshot: dict[str, object] | None = None

        prepared_ann_path = Path(str(prepared["prepared_ann_path"]))
        prepared_ann_checksum = str(prepared["prepared_ann_checksum"])
        self.ann_index_store.verify_prepared_artifact(
            artifact_path=prepared_ann_path,
            expected_checksum=prepared_ann_checksum,
        )
        published_cluster_person_pairs: list[tuple[int, int]] = []

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if previous_owner_run_id is not None and previous_owner_run_id != int(run_id):
                previous_owner_live_snapshot = self.person_repo.retire_bootstrap_people(
                    source_run_id=int(previous_owner_run_id),
                )
                self.publish_repo.clear_materialization_owner()

            bundles = self.publish_repo.list_prepared_publish_bundles(run_id=int(run_id))
            for bundle in bundles:
                cluster_id = int(bundle["cluster_id"])
                publish_plan = bundle["person_publish_plan"]
                person_id = self.person_repo.create_anonymous_person(
                    origin_cluster_id=None,
                    sequence=self.person_repo.next_anonymous_sequence(),
                )
                self.person_repo.apply_person_publish_plan(
                    person_id=int(person_id),
                    publish_plan=publish_plan,
                    source_run_id=int(run_id),
                    source_cluster_id=cluster_id,
                )
                self.prototype_service.activate_prepared_cluster_prototype(
                    run_id=int(run_id),
                    cluster_id=cluster_id,
                    person_id=int(person_id),
                )
                self.publish_repo.mark_cluster_published(
                    cluster_id=cluster_id,
                    person_id=int(person_id),
                )
                published_cluster_person_pairs.append((cluster_id, int(person_id)))

            self.publish_repo.set_materialization_owner(run_id=int(run_id))
            self.publish_repo.mark_run_activated(run_id=int(run_id))
            self.conn.commit()
        except Exception as exc:
            if self.conn.in_transaction:
                self.conn.rollback()
            self._mark_publish_failed_persisted(
                run_id=int(run_id),
                reason=f"publish_transaction_failed:{exc}",
            )
            raise

        try:
            live_ann_bundle = self.publish_repo.build_live_ann_artifact_from_prepared(
                run_id=int(run_id),
                prepared_ann_path=prepared_ann_path,
                prepared_ann_checksum=prepared_ann_checksum,
                cluster_person_pairs=published_cluster_person_pairs,
            )
            self.ann_index_store.activate_verified_artifact(
                artifact_path=Path(str(live_ann_bundle["artifact_path"])),
                expected_checksum=str(live_ann_bundle["artifact_checksum"]),
                source_run_id=int(run_id),
            )
        except Exception as exc:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.person_repo.retire_bootstrap_people(source_run_id=int(run_id))
                self.publish_repo.clear_materialization_owner()
                self.publish_repo.mark_clusters_publish_failed_for_activation(
                    run_id=int(run_id),
                    reason=f"live_ann_switch_failed:{exc}",
                )
                if previous_owner_run_id is not None and previous_owner_run_id != int(run_id):
                    self.person_repo.restore_bootstrap_people(
                        source_run_id=int(previous_owner_run_id),
                        live_snapshot=previous_owner_live_snapshot,
                    )
                    self.publish_repo.set_materialization_owner(run_id=int(previous_owner_run_id))
                self.conn.commit()
            except Exception:
                if self.conn.in_transaction:
                    self.conn.rollback()
                self._mark_publish_failed_persisted(
                    run_id=int(run_id),
                    reason=f"live_ann_switch_failed_compensation_required:{exc}",
                )
            raise

    def _mark_publish_failed_persisted(self, *, run_id: int, reason: str) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.publish_repo.mark_clusters_publish_failed_for_activation(
                run_id=int(run_id),
                reason=str(reason),
            )
            self.conn.commit()
        except Exception:
            if self.conn.in_transaction:
                self.conn.rollback()
