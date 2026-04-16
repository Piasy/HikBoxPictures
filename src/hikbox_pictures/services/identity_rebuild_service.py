from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.ann import AnnIndexStore
from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.db.migrator import apply_migrations
from hikbox_pictures.repositories.identity_repo import IdentityRepo
from hikbox_pictures.repositories.person_repo import PersonRepo
from hikbox_pictures.services.identity_bootstrap_service import IdentityBootstrapService
from hikbox_pictures.services.identity_threshold_profile_service import IdentityThresholdProfileService
from hikbox_pictures.services.observation_quality_backfill_service import ObservationQualityBackfillService
from hikbox_pictures.services.prototype_service import PrototypeService
from hikbox_pictures.workspace import ensure_workspace_layout


class IdentityRebuildService:
    _PHASE1_ORDER: tuple[str, ...] = (
        "profile_resolve",
        "clear_identity_export_layers",
        "quality_backfill",
        "bootstrap_materialize",
        "prototype_ann_rebuild_optional",
        "summary",
    )
    _CLEAR_TABLES: tuple[str, ...] = (
        "person_face_exclusion",
        "person_face_assignment",
        "person_trusted_sample",
        "person_prototype",
        "review_item",
        "export_delivery",
        "export_run",
        "export_template_person",
        "export_template",
        "person",
        "auto_cluster_member",
        "auto_cluster",
        "auto_cluster_batch",
    )

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.paths = ensure_workspace_layout(self.workspace)
        self.conn = connect_db(self.paths.db_path)
        apply_migrations(self.conn)

    def close(self) -> None:
        self.conn.close()

    def run_rebuild(
        self,
        *,
        dry_run: bool,
        backup_db: bool,
        skip_ann_rebuild: bool,
        threshold_profile_path: Path | None,
    ) -> dict[str, Any]:
        profile_service = IdentityThresholdProfileService(self.conn)
        clear_scope = self._collect_clear_scope()
        ann_artifacts = self._list_ann_artifacts()

        backup_path = self._backup_db_if_needed(enabled=backup_db)
        summary: dict[str, Any] = {
            "workspace": str(self.workspace),
            "dry_run": bool(dry_run),
            "phase1_order": list(self._PHASE1_ORDER),
            "executed_phase1_order": [],
            "skip_ann_rebuild": bool(skip_ann_rebuild),
            "threshold_profile_path": str(threshold_profile_path) if threshold_profile_path is not None else None,
            "options": {
                "backup_db": bool(backup_db),
                "skip_ann_rebuild": bool(skip_ann_rebuild),
                "threshold_profile_path": (
                    str(threshold_profile_path) if threshold_profile_path is not None else None
                ),
            },
            "clear_scope": clear_scope,
            "ann_artifact_scope": ann_artifacts,
            "backup_db_path": str(backup_path) if backup_path is not None else None,
            "optional_phase": {
                "prototype_ann_rebuild_optional": {
                    "enabled": not bool(skip_ann_rebuild),
                    "status": "pending",
                }
            },
            "summary_written_at": datetime.now().isoformat(timespec="seconds"),
        }

        if dry_run:
            threshold_candidate_validated = False
            if threshold_profile_path is not None:
                payload = json.loads(threshold_profile_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("threshold-profile JSON 必须是对象")
                profile_service.validate_candidate_keys(payload)
                threshold_candidate_validated = True
            summary["dry_run_plan"] = {
                "clear_targets": dict(clear_scope),
                "ann_artifacts": list(ann_artifacts),
                "threshold_profile_candidate_validated": threshold_candidate_validated,
                "backup_db_path": str(backup_path) if backup_path is not None else None,
            }
            self._persist_summary(summary)
            return summary

        managed_transaction = not self.conn.in_transaction
        try:
            if managed_transaction:
                self.conn.execute("BEGIN")

            profile_resolution = profile_service.resolve_profile_for_rebuild(threshold_profile_path)
            summary["executed_phase1_order"].append("profile_resolve")
            profile_id = int(profile_resolution["profile_id"])
            model_key = profile_service.get_profile_model_key(profile_id)

            cleared_counts, fk_break_updates = self._clear_identity_export_layers()
            summary["executed_phase1_order"].append("clear_identity_export_layers")
            removed_ann_artifact_count = self._clear_ann_artifacts()

            backfill_summary = ObservationQualityBackfillService(self.conn).backfill_all_observations(
                profile_id=profile_id,
                update_profile_quantiles=bool(profile_resolution["update_profile_quantiles"]),
            )
            summary["executed_phase1_order"].append("quality_backfill")

            person_repo = PersonRepo(self.conn)
            prototype_service = PrototypeService(
                self.conn,
                person_repo,
                AnnIndexStore(self.paths.artifacts_dir / "ann" / "prototype_index.npz"),
            )
            bootstrap_summary = IdentityBootstrapService(
                self.conn,
                identity_repo=IdentityRepo(self.conn),
                person_repo=person_repo,
                prototype_service=prototype_service,
            ).run_bootstrap(profile_id=profile_id)
            summary["executed_phase1_order"].append("bootstrap_materialize")

            prototype_rebuild_summary: dict[str, int] | None = None
            if not skip_ann_rebuild:
                rebuilt_count = prototype_service.rebuild_all_person_prototypes(model_key=model_key)
                indexed_count = prototype_service.rebuild_ann_index_from_active_prototypes(model_key=model_key)
                prototype_rebuild_summary = {
                    "rebuilt_person_prototype_count": int(rebuilt_count),
                    "ann_index_person_count": int(indexed_count),
                }
                summary["optional_phase"]["prototype_ann_rebuild_optional"]["status"] = "executed"
            else:
                summary["optional_phase"]["prototype_ann_rebuild_optional"]["status"] = "skipped"
            summary["executed_phase1_order"].append("prototype_ann_rebuild_optional")

            bootstrap_materialized = int(bootstrap_summary.get("materialized_cluster_count", 0))
            bootstrap_review_pending = int(bootstrap_summary.get("review_pending_cluster_count", 0))
            bootstrap_discarded = int(bootstrap_summary.get("discarded_cluster_count", 0))
            post_rebuild = self._collect_post_rebuild_snapshot(profile_id=profile_id)
            profile_summary = {
                "active_threshold_profile_id": profile_id,
                "profile_mode": str(profile_resolution.get("profile_mode") or "unknown"),
                "threshold_profile_model_key": model_key,
                "imported_threshold_profile": bool(profile_resolution["imported_threshold_profile"]),
                "update_profile_quantiles": bool(profile_resolution["update_profile_quantiles"]),
            }
            summary.update(
                {
                    "profile": profile_summary,
                    "clear_execution": {
                        "fk_break_updates": fk_break_updates,
                        "clear_targets": cleared_counts,
                        "removed_ann_artifact_count": int(removed_ann_artifact_count),
                    },
                    "threshold_profile_id": profile_id,
                    "active_threshold_profile_id": profile_id,
                    "profile_mode": str(profile_resolution.get("profile_mode") or "unknown"),
                    "threshold_profile_model_key": model_key,
                    "imported_threshold_profile": bool(profile_resolution["imported_threshold_profile"]),
                    "update_profile_quantiles": bool(profile_resolution["update_profile_quantiles"]),
                    "cleared_counts": cleared_counts,
                    "removed_ann_artifact_count": int(removed_ann_artifact_count),
                    "quality_backfill": backfill_summary,
                    "bootstrap": bootstrap_summary,
                    "materialized_cluster_count": bootstrap_materialized,
                    "review_pending_cluster_count": bootstrap_review_pending,
                    "discarded_cluster_count": bootstrap_discarded,
                    "prototype_rebuild": prototype_rebuild_summary,
                    "prototype_ann_rebuild": prototype_rebuild_summary,
                    "post_rebuild": post_rebuild,
                }
            )
            if managed_transaction:
                self.conn.commit()
            summary["executed_phase1_order"].append("summary")
            self._persist_summary(summary)
            return summary
        except Exception:
            if managed_transaction and self.conn.in_transaction:
                self.conn.rollback()
            raise

    def _collect_clear_scope(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for table in self._CLEAR_TABLES:
            row = self.conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
            result[table] = int(row["c"]) if row is not None else 0
        return result

    def _clear_identity_export_layers(self) -> tuple[dict[str, int], dict[str, int]]:
        cleared_counts: dict[str, int] = {}
        fk_break_updates: dict[str, int] = {}
        managed_transaction = not self.conn.in_transaction
        try:
            fk_break_updates["person.origin_cluster_id"] = int(
                self.conn.execute(
                    "UPDATE person SET origin_cluster_id = NULL WHERE origin_cluster_id IS NOT NULL"
                ).rowcount
            )
            fk_break_updates["person.merged_into_person_id"] = int(
                self.conn.execute(
                    "UPDATE person SET merged_into_person_id = NULL WHERE merged_into_person_id IS NOT NULL"
                ).rowcount
            )
            fk_break_updates["auto_cluster.resolved_person_id"] = int(
                self.conn.execute(
                    "UPDATE auto_cluster SET resolved_person_id = NULL WHERE resolved_person_id IS NOT NULL"
                ).rowcount
            )
            fk_break_updates["ops_event.export_run_id"] = int(
                self.conn.execute(
                    "UPDATE ops_event SET export_run_id = NULL WHERE export_run_id IS NOT NULL"
                ).rowcount
            )
            fk_break_updates["ops_event.template_id"] = int(
                self.conn.execute(
                    "UPDATE ops_event SET template_id = NULL WHERE template_id IS NOT NULL"
                ).rowcount
            )
            for table in self._CLEAR_TABLES:
                cursor = self.conn.execute(f"DELETE FROM {table}")
                cleared_counts[table] = int(cursor.rowcount)
            if managed_transaction:
                self.conn.commit()
            return cleared_counts, fk_break_updates
        except Exception:
            if managed_transaction and self.conn.in_transaction:
                self.conn.rollback()
            raise

    def _list_ann_artifacts(self) -> list[str]:
        ann_dir = self.paths.artifacts_dir / "ann"
        if not ann_dir.exists():
            return []
        return sorted(str(path.resolve()) for path in ann_dir.glob("**/*") if path.is_file())

    def _clear_ann_artifacts(self) -> int:
        ann_dir = self.paths.artifacts_dir / "ann"
        if not ann_dir.exists():
            return 0
        removed = 0
        for path in ann_dir.glob("**/*"):
            if not path.is_file():
                continue
            path.unlink(missing_ok=True)
            removed += 1
        return removed

    def _backup_db_if_needed(self, *, enabled: bool) -> Path | None:
        if not enabled:
            return None
        backup_dir = self.workspace / ".tmp" / "rebuild-identities-v3" / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup_path = backup_dir / f"library-{stamp}.db"

        target = sqlite3.connect(str(backup_path))
        try:
            self.conn.backup(target)
            target.commit()
        finally:
            target.close()
        return backup_path

    def _persist_summary(self, summary: dict[str, Any]) -> None:
        summary_path = self.workspace / ".tmp" / "rebuild-identities-v3" / "last-summary.json"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _collect_post_rebuild_snapshot(self, *, profile_id: int) -> dict[str, Any]:
        status_counts = {
            "materialized": 0,
            "review_pending": 0,
            "discarded": 0,
        }
        rows = self.conn.execute(
            """
            SELECT cluster_status, COUNT(*) AS c
            FROM auto_cluster
            GROUP BY cluster_status
            """
        ).fetchall()
        for row in rows:
            status = str(row["cluster_status"])
            if status in status_counts:
                status_counts[status] = int(row["c"])

        active_profile_row = self.conn.execute(
            """
            SELECT id, profile_name, profile_version, embedding_model_key
            FROM identity_threshold_profile
            WHERE id = ?
            """,
            (int(profile_id),),
        ).fetchone()
        active_profile = dict(active_profile_row) if active_profile_row is not None else None

        return {
            "active_threshold_profile": active_profile,
            "person_count": self._count_rows("person"),
            "trusted_sample_count": self._count_rows("person_trusted_sample"),
            "prototype_count": self._count_rows("person_prototype"),
            "materialized_cluster_count": status_counts["materialized"],
            "review_pending_cluster_count": status_counts["review_pending"],
            "discarded_cluster_count": status_counts["discarded"],
        }

    def _count_rows(self, table: str) -> int:
        row = self.conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
        return int(row["c"]) if row is not None else 0
