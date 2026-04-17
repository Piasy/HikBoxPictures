from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.db.migrator import apply_migrations
from hikbox_pictures.repositories.asset_repo import AssetRepo
from hikbox_pictures.repositories.identity_cluster_run_repo import IdentityClusterRunRepo
from hikbox_pictures.repositories.identity_observation_repo import IdentityObservationRepo
from hikbox_pictures.repositories.source_repo import SourceRepo
from hikbox_pictures.services.identity_cluster_run_service import IdentityClusterRunService
from hikbox_pictures.services.identity_observation_snapshot_service import IdentityObservationSnapshotService
from hikbox_pictures.workspace import WorkspacePaths, init_workspace_layout


class _NoopBackfillService:
    def __init__(self, owner: "IdentityPhase1Workspace") -> None:
        self._owner = owner

    def backfill_all_observations(
        self,
        *,
        profile_id: int,
        update_profile_quantiles: bool = False,
        allow_legacy_profile: bool = True,
    ) -> dict[str, int | float]:
        self._owner.backfill_call_count += 1
        return {
            "updated_observation_count": 0,
            "area_log_p10": 0.0,
            "area_log_p90": 0.0,
            "sharpness_log_p10": 0.0,
            "sharpness_log_p90": 0.0,
        }


@dataclass
class IdentityPhase1Workspace:
    root: Path
    paths: WorkspacePaths
    conn: sqlite3.Connection
    observation_profile_id: int
    cluster_profile_id: int
    backfill_call_count: int = 0
    stub_run_ann_prepare_failure_run_ids: set[int] = field(default_factory=set)
    stub_publish_stage_failure_reasons: dict[int, str] = field(default_factory=dict)

    def close(self) -> None:
        self.conn.close()

    def new_observation_snapshot_service(self) -> IdentityObservationSnapshotService:
        return IdentityObservationSnapshotService(
            self.conn,
            observation_repo=IdentityObservationRepo(self.conn),
            quality_backfill_service=_NoopBackfillService(self),
        )

    def new_cluster_run_service(self) -> IdentityClusterRunService:
        return IdentityClusterRunService(
            self.conn,
            cluster_run_repo=IdentityClusterRunRepo(self.conn),
        )

    def new_cluster_prepare_service(self):  # type: ignore[no-untyped-def]
        from hikbox_pictures.ann import AnnIndexStore
        from hikbox_pictures.repositories.identity_publish_repo import IdentityPublishRepo
        from hikbox_pictures.repositories.person_repo import PersonRepo
        from hikbox_pictures.services.identity_cluster_prepare_service import IdentityClusterPrepareService
        from hikbox_pictures.services.prototype_service import PrototypeService

        person_repo = PersonRepo(self.conn)
        ann_store = AnnIndexStore(self.paths.artifacts_dir / "ann" / "prototype_index.npz")
        prototype_service = PrototypeService(self.conn, person_repo=person_repo, ann_index_store=ann_store)
        publish_repo = IdentityPublishRepo(
            self.conn,
            artifact_root=self.paths.artifacts_dir / "identity_prepare",
            live_ann_artifact_path=self.paths.artifacts_dir / "ann" / "prototype_index.npz",
            run_ann_prepare_failure_run_ids=self.stub_run_ann_prepare_failure_run_ids,
            publish_stage_failure_reasons=self.stub_publish_stage_failure_reasons,
        )
        return IdentityClusterPrepareService(
            self.conn,
            publish_repo=publish_repo,
            person_repo=person_repo,
            prototype_service=prototype_service,
            ann_index_store=ann_store,
        )

    def new_run_activation_service(self):  # type: ignore[no-untyped-def]
        from hikbox_pictures.ann import AnnIndexStore
        from hikbox_pictures.repositories.identity_publish_repo import IdentityPublishRepo
        from hikbox_pictures.repositories.person_repo import PersonRepo
        from hikbox_pictures.services.identity_run_activation_service import IdentityRunActivationService
        from hikbox_pictures.services.prototype_service import PrototypeService

        person_repo = PersonRepo(self.conn)
        ann_store = AnnIndexStore(self.paths.artifacts_dir / "ann" / "prototype_index.npz")
        prototype_service = PrototypeService(self.conn, person_repo=person_repo, ann_index_store=ann_store)
        publish_repo = IdentityPublishRepo(
            self.conn,
            artifact_root=self.paths.artifacts_dir / "identity_prepare",
            live_ann_artifact_path=self.paths.artifacts_dir / "ann" / "prototype_index.npz",
            run_ann_prepare_failure_run_ids=self.stub_run_ann_prepare_failure_run_ids,
            publish_stage_failure_reasons=self.stub_publish_stage_failure_reasons,
        )
        return IdentityRunActivationService(
            self.conn,
            publish_repo=publish_repo,
            person_repo=person_repo,
            prototype_service=prototype_service,
            ann_index_store=ann_store,
        )

    def get_cluster_run(self, run_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT
                id,
                observation_snapshot_id,
                cluster_profile_id,
                run_status,
                is_review_target,
                review_selected_at,
                is_materialization_owner,
                supersedes_run_id,
                started_at,
                finished_at,
                summary_json,
                failure_json
            FROM identity_cluster_run
            WHERE id = ?
            """,
            (int(run_id),),
        ).fetchone()
        if row is None:
            raise AssertionError(f"cluster run 不存在: {int(run_id)}")
        return dict(row)

    def count_review_targets(self) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM identity_cluster_run
            WHERE is_review_target = 1
            """
        ).fetchone()
        return int(row["c"])

    def count_clusters(self, *, run_id: int) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM identity_cluster
            WHERE run_id = ?
            """,
            (int(run_id),),
        ).fetchone()
        return int(row["c"])

    def count_cluster_members(self, *, run_id: int) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM identity_cluster_member AS m
            JOIN identity_cluster AS c ON c.id = m.cluster_id
            WHERE c.run_id = ?
            """,
            (int(run_id),),
        ).fetchone()
        return int(row["c"])

    def count_cluster_resolutions(self, *, run_id: int) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM identity_cluster_resolution AS r
            JOIN identity_cluster AS c ON c.id = r.cluster_id
            WHERE c.run_id = ?
            """,
            (int(run_id),),
        ).fetchone()
        return int(row["c"])

    def list_clusters(self, *, run_id: int, cluster_stage: str | None = None) -> list[dict[str, Any]]:
        sql = """
            SELECT *
            FROM identity_cluster
            WHERE run_id = ?
        """
        params: list[Any] = [int(run_id)]
        if cluster_stage is not None:
            sql += " AND cluster_stage = ?"
            params.append(str(cluster_stage))
        sql += " ORDER BY id ASC"
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def list_cluster_lineage(self, *, run_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT l.*
            FROM identity_cluster_lineage AS l
            JOIN identity_cluster AS p ON p.id = l.parent_cluster_id
            JOIN identity_cluster AS c ON c.id = l.child_cluster_id
            WHERE p.run_id = ?
              AND c.run_id = ?
            ORDER BY l.id ASC
            """,
            (int(run_id), int(run_id)),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_cluster_members(self, *, run_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT m.*, c.cluster_stage, c.cluster_state, c.run_id
            FROM identity_cluster_member AS m
            JOIN identity_cluster AS c ON c.id = m.cluster_id
            WHERE c.run_id = ?
            ORDER BY c.id ASC, m.observation_id ASC
            """,
            (int(run_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_cluster_resolutions(self, *, run_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT r.*
            FROM identity_cluster_resolution AS r
            JOIN identity_cluster AS c ON c.id = r.cluster_id
            WHERE c.run_id = ?
            ORDER BY r.cluster_id ASC
            """,
            (int(run_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_pool_entries(self, *, snapshot_id: int, pool_kind: str, excluded_reason: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT dedup_group_key, representative_observation_id, diagnostic_json
            FROM identity_observation_pool_entry
            WHERE snapshot_id = ?
              AND pool_kind = ?
              AND excluded_reason = ?
            ORDER BY observation_id ASC
            """,
            (snapshot_id, pool_kind, excluded_reason),
        ).fetchall()
        return [
            {
                "dedup_group_key": row["dedup_group_key"],
                "representative_observation_id": row["representative_observation_id"],
                "diagnostic_json": json.loads(row["diagnostic_json"]),
            }
            for row in rows
        ]

    def create_observation_profile_variant(
        self,
        *,
        profile_name: str,
        core_quality_threshold: float,
        burst_window_seconds: int,
    ) -> int:
        row = self.conn.execute(
            """
            INSERT INTO identity_observation_profile(
                profile_name, profile_version, embedding_feature_type, embedding_model_key,
                embedding_distance_metric, embedding_schema_version, quality_formula_version,
                quality_area_weight, quality_sharpness_weight, quality_pose_weight,
                core_quality_threshold, attachment_quality_threshold,
                exact_duplicate_distance_threshold, same_photo_keep_best,
                burst_window_seconds, burst_duplicate_distance_threshold,
                pool_exclusion_rules_version, active
            )
            SELECT ?, profile_version || '.alt', embedding_feature_type, embedding_model_key,
                   embedding_distance_metric, embedding_schema_version, quality_formula_version,
                   quality_area_weight, quality_sharpness_weight, quality_pose_weight,
                   ?, attachment_quality_threshold, exact_duplicate_distance_threshold,
                   same_photo_keep_best, ?, burst_duplicate_distance_threshold,
                   pool_exclusion_rules_version, 0
            FROM identity_observation_profile
            WHERE id = ?
            """,
            (profile_name, core_quality_threshold, burst_window_seconds, self.observation_profile_id),
        )
        self.conn.commit()
        return int(row.lastrowid)

    def seed_additional_observation_for_dataset_change(self) -> None:
        source_id = self._ensure_source()
        image_path = self._make_image("dataset-change.jpg", pattern="checker")
        photo_id = self._insert_photo(source_id, image_path, capture_second=99)
        obs_id = self._insert_observation(
            photo_id,
            bbox=(0.10, 0.35, 0.35, 0.10),
            area_ratio=0.2,
            pose=0.85,
            quality=0.82,
        )
        self._insert_embedding(obs_id, np.asarray([0.2, 0.8, 0.1, 0.0], dtype=np.float32))
        self.conn.commit()

    def seed_observation_mix_case(self) -> None:
        source_id = self._ensure_source()
        self.conn.execute("DELETE FROM face_embedding")
        self.conn.execute("DELETE FROM face_observation")
        self.conn.execute("DELETE FROM photo_asset")
        self.conn.execute("DELETE FROM identity_observation_pool_entry")

        # 真实可区分向量布局：4 core + 2 attachment + 3 excluded（同图/连拍/exact shadow）
        photo_same = self._insert_photo(source_id, self._make_image("p1-main.jpg", pattern="diag"), capture_second=0)
        obs_main = self._insert_observation(
            photo_same,
            bbox=(0.10, 0.40, 0.40, 0.10),
            area_ratio=0.17,
            pose=0.92,
            quality=0.94,
        )
        self._insert_embedding(obs_main, np.asarray([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
        obs_same_photo_dup = self._insert_observation(
            photo_same,
            bbox=(0.12, 0.42, 0.42, 0.12),
            area_ratio=0.18,
            pose=0.86,
            quality=0.90,
        )
        self._insert_embedding(obs_same_photo_dup, np.asarray([0.999, 0.001, 0.0, 0.0], dtype=np.float32))

        specs = [
            ("p2-burst-dup.jpg", 10, np.asarray([0.995, 0.005, 0.0, 0.0], dtype=np.float32), 0.80, 0.88, "diag"),
            ("p3-shadow-dup.jpg", 45, np.asarray([1.0, 0.0, 0.0, 0.0], dtype=np.float32), 0.74, 0.87, "diag"),
            ("p4-core-b.jpg", 60, np.asarray([0.0, 1.0, 0.0, 0.0], dtype=np.float32), 0.93, 0.93, "checker"),
            ("p5-core-c.jpg", 70, np.asarray([0.0, 0.0, 1.0, 0.0], dtype=np.float32), 0.91, 0.92, "stripe"),
            ("p6-core-d.jpg", 80, np.asarray([0.0, 0.0, 0.0, 1.0], dtype=np.float32), 0.89, 0.91, "solid"),
            ("p7-attachment-a.jpg", 90, np.asarray([0.6, 0.6, 0.1, 0.0], dtype=np.float32), 0.52, 0.46, "checker"),
            ("p8-attachment-b.jpg", 100, np.asarray([0.2, 0.1, 0.9, 0.1], dtype=np.float32), 0.51, 0.41, "stripe"),
        ]
        for index, (file_name, capture_second, vector, pose, quality, pattern) in enumerate(specs, start=1):
            image_path = self._make_image(file_name, pattern=pattern)
            photo_id = self._insert_photo(source_id, image_path, capture_second=capture_second)
            obs_id = self._insert_observation(
                photo_id,
                bbox=(0.10, 0.40, 0.40, 0.10),
                area_ratio=0.18 + float(index) * 0.01,
                pose=pose,
                quality=quality,
            )
            self._insert_embedding(obs_id, vector)
        self.conn.commit()

    def seed_split_and_attachment_case(self) -> None:
        source_id = self._ensure_source()
        self._reset_observations()

        specs = [
            ("split-a1.jpg", 0, [1.000, 0.000, 0.000, 0.000], 0.94, 0.95, "diag"),
            ("split-a2.jpg", 30, [0.980, 0.020, 0.000, 0.000], 0.91, 0.92, "diag"),
            ("split-a3.jpg", 60, [0.965, 0.035, 0.000, 0.000], 0.89, 0.90, "diag"),
            ("split-a4.jpg", 90, [0.945, 0.055, 0.000, 0.000], 0.88, 0.89, "diag"),
            ("split-bridge-1.jpg", 120, [0.640, 0.360, 0.000, 0.000], 0.86, 0.87, "checker"),
            ("split-bridge-2.jpg", 150, [0.550, 0.450, 0.000, 0.000], 0.84, 0.85, "checker"),
            ("split-b1.jpg", 180, [0.000, 1.000, 0.000, 0.000], 0.95, 0.95, "stripe"),
            ("split-b2.jpg", 210, [0.020, 0.980, 0.000, 0.000], 0.92, 0.93, "stripe"),
            ("split-b3.jpg", 240, [0.040, 0.960, 0.000, 0.000], 0.90, 0.91, "stripe"),
            ("split-b4.jpg", 270, [0.060, 0.940, 0.000, 0.000], 0.87, 0.88, "stripe"),
            ("discard-c1.jpg", 300, [0.000, 0.000, 1.000, 0.000], 0.72, 0.78, "solid"),
            ("discard-c2.jpg", 330, [0.020, 0.000, 0.980, 0.000], 0.70, 0.76, "solid"),
            ("attach-a.jpg", 360, [0.840, 0.160, 0.000, 0.000], 0.58, 0.50, "checker"),
            ("attach-b.jpg", 390, [0.790, 0.210, 0.000, 0.000], 0.54, 0.46, "checker"),
            ("attach-reject.jpg", 420, [0.330, 0.330, 0.340, 0.000], 0.51, 0.44, "checker"),
            ("same-photo-main.jpg", 450, [0.930, 0.070, 0.000, 0.000], 0.88, 0.93, "diag"),
        ]
        same_photo_image = self._make_image("same-photo-main.jpg", pattern="diag")
        same_photo_id = self._insert_photo(source_id, same_photo_image, capture_second=450)
        same_main_obs = self._insert_observation(
            same_photo_id,
            bbox=(0.10, 0.40, 0.40, 0.10),
            area_ratio=0.20,
            pose=0.88,
            quality=0.93,
        )
        self._insert_embedding(same_main_obs, np.asarray([0.930, 0.070, 0.000, 0.000], dtype=np.float32))
        same_dup_obs = self._insert_observation(
            same_photo_id,
            bbox=(0.12, 0.42, 0.42, 0.12),
            area_ratio=0.19,
            pose=0.77,
            quality=0.20,
        )
        self._insert_embedding(same_dup_obs, np.asarray([0.920, 0.080, 0.000, 0.000], dtype=np.float32))

        for index, (file_name, capture_second, vector, pose, quality, pattern) in enumerate(specs, start=1):
            if file_name == "same-photo-main.jpg":
                continue
            image_path = self._make_image(file_name, pattern=pattern)
            photo_id = self._insert_photo(source_id, image_path, capture_second=capture_second)
            obs_id = self._insert_observation(
                photo_id,
                bbox=(0.10, 0.40, 0.40, 0.10),
                area_ratio=0.17 + float(index) * 0.008,
                pose=pose,
                quality=quality,
            )
            self._insert_embedding(obs_id, np.asarray(vector, dtype=np.float32))
        self.conn.commit()

    def seed_known_topology_case(self) -> None:
        source_id = self._ensure_source()
        self._reset_observations()

        specs = [
            ("known-a1.jpg", 0, [1.00, 0.00, 0.00, 0.00], 0.95, 0.95, "diag"),
            ("known-a2.jpg", 10, [0.97, 0.03, 0.00, 0.00], 0.94, 0.94, "diag"),
            ("known-a3.jpg", 20, [0.94, 0.06, 0.00, 0.00], 0.93, 0.93, "diag"),
            ("known-a4.jpg", 30, [0.91, 0.09, 0.00, 0.00], 0.92, 0.92, "diag"),
            ("known-b1.jpg", 40, [0.00, 1.00, 0.00, 0.00], 0.95, 0.95, "stripe"),
            ("known-b2.jpg", 50, [0.03, 0.97, 0.00, 0.00], 0.94, 0.94, "stripe"),
            ("known-b3.jpg", 60, [0.06, 0.94, 0.00, 0.00], 0.93, 0.93, "stripe"),
            ("known-b4.jpg", 70, [0.09, 0.91, 0.00, 0.00], 0.92, 0.92, "stripe"),
            ("known-attach.jpg", 80, [0.82, 0.18, 0.00, 0.00], 0.56, 0.48, "checker"),
            ("known-far.jpg", 90, [0.00, 0.00, 1.00, 0.00], 0.80, 0.79, "solid"),
        ]
        for index, (file_name, capture_second, vector, pose, quality, pattern) in enumerate(specs, start=1):
            image_path = self._make_image(file_name, pattern=pattern)
            photo_id = self._insert_photo(source_id, image_path, capture_second=capture_second)
            obs_id = self._insert_observation(
                photo_id,
                bbox=(0.10, 0.40, 0.40, 0.10),
                area_ratio=0.19 + float(index) * 0.004,
                pose=pose,
                quality=quality,
            )
            self._insert_embedding(obs_id, np.asarray(vector, dtype=np.float32))
        self.conn.commit()

    def seed_materialize_candidate_case(self) -> None:
        self.seed_split_and_attachment_case()
        self.conn.execute(
            """
            UPDATE identity_cluster_profile
            SET materialize_min_anchor_core_count = 1,
                materialize_min_distinct_photo_count = 2,
                materialize_max_compactness_p90 = 0.80,
                materialize_min_separation_gap = 0.0,
                materialize_max_boundary_ratio = 0.80,
                trusted_seed_min_quality = 0.45,
                trusted_seed_min_count = 2,
                trusted_seed_max_count = 5,
                trusted_seed_allow_boundary = 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (int(self.cluster_profile_id),),
        )
        self.conn.commit()

    def seed_materialize_gate_negative_case(self, *, scenario: str) -> None:
        self.seed_materialize_candidate_case()
        if str(scenario) == "anchor_core_below_materialize_min":
            self.conn.execute(
                """
                UPDATE identity_cluster_profile
                SET materialize_min_anchor_core_count = 5,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (int(self.cluster_profile_id),),
            )
            self.conn.commit()
            return
        raise ValueError(f"未知 materialize gate 场景: {scenario}")

    def get_run_ann_manifest(self, run_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT prepared_ann_manifest_json
            FROM identity_cluster_run
            WHERE id = ?
            """,
            (int(run_id),),
        ).fetchone()
        if row is None:
            raise AssertionError(f"run 不存在: {int(run_id)}")
        payload = json.loads(str(row["prepared_ann_manifest_json"] or "{}"))
        if not isinstance(payload, dict):
            return {}
        return payload

    def stub_run_ann_prepare_failure(self, *, run_id: int) -> None:
        self.stub_run_ann_prepare_failure_run_ids.add(int(run_id))

    def stub_publish_stage_failure(self, *, run_id: int, reason: str) -> None:
        self.stub_publish_stage_failure_reasons[int(run_id)] = str(reason)

    def corrupt_prepared_ann_artifact(self, *, run_id: int) -> None:
        manifest = self.get_run_ann_manifest(int(run_id))
        artifact_path = Path(str(manifest.get("artifact_path") or "")).expanduser()
        if not artifact_path.is_absolute():
            artifact_path = (self.root / artifact_path).resolve()
        if not artifact_path.exists():
            raise AssertionError(f"prepared ann artifact 不存在: {artifact_path}")
        payload = artifact_path.read_bytes()
        artifact_path.write_bytes(payload + b"corrupt")

    def get_live_ann_owner_run_id(self) -> int | None:
        from hikbox_pictures.ann import AnnIndexStore

        ann_store = AnnIndexStore(self.paths.artifacts_dir / "ann" / "prototype_index.npz")
        return ann_store.get_live_owner_run_id()

    def get_live_ann_person_ids(self) -> list[int]:
        artifact_path = self.paths.artifacts_dir / "ann" / "prototype_index.npz"
        if not artifact_path.exists():
            return []
        with np.load(artifact_path, allow_pickle=False) as payload:
            if "person_ids" not in payload.files:
                return []
            person_ids = payload["person_ids"].astype(np.int64, copy=False)
        return [int(value) for value in person_ids.tolist()]

    def get_live_ann_checksum(self) -> str:
        from hikbox_pictures.ann import AnnIndexStore

        ann_store = AnnIndexStore(self.paths.artifacts_dir / "ann" / "prototype_index.npz")
        return ann_store.calculate_artifact_checksum()

    def get_live_prototype_owner_run_id(self) -> int | None:
        rows = self.conn.execute(
            """
            SELECT DISTINCT pco.source_run_id
            FROM person_prototype AS pp
            JOIN person_cluster_origin AS pco ON pco.person_id = pp.person_id
            WHERE pp.active = 1
              AND pco.active = 1
            ORDER BY pco.source_run_id ASC
            """
        ).fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            raise AssertionError("存在多个 live prototype owner run")
        return int(rows[0]["source_run_id"])

    def recompute_member_support_ratio(self, *, cluster_id: int, observation_id: int) -> float:
        profile = self._get_cluster_profile_for_cluster(cluster_id=cluster_id)
        k = int(profile["discovery_knn_k"])
        cluster_rows = self.conn.execute(
            """
            SELECT m.observation_id
            FROM identity_cluster_member AS m
            WHERE m.cluster_id = ?
              AND m.decision_status = 'retained'
            """,
            (int(cluster_id),),
        ).fetchall()
        cluster_member_ids = {int(row["observation_id"]) for row in cluster_rows}
        all_core_rows = self.conn.execute(
            """
            SELECT pe.observation_id, fo.photo_asset_id, fe.vector_blob
            FROM identity_observation_pool_entry AS pe
            JOIN face_observation AS fo ON fo.id = pe.observation_id
            JOIN face_embedding AS fe
              ON fe.face_observation_id = pe.observation_id
             AND fe.feature_type = 'face'
             AND fe.model_key = 'insightface'
             AND fe.normalized = 1
            WHERE pe.snapshot_id = (
                SELECT run_ref.observation_snapshot_id
                FROM identity_cluster AS c
                JOIN identity_cluster_run AS run_ref ON run_ref.id = c.run_id
                WHERE c.id = ?
            )
              AND pe.pool_kind = 'core_discovery'
            ORDER BY pe.observation_id ASC
            """,
            (int(cluster_id),),
        ).fetchall()
        vector_map = {
            int(row["observation_id"]): np.frombuffer(row["vector_blob"], dtype=np.float32).copy()
            for row in all_core_rows
            if isinstance(row["vector_blob"], (bytes, bytearray, memoryview))
        }
        photo_map = {int(row["observation_id"]): int(row["photo_asset_id"]) for row in all_core_rows}
        if int(observation_id) not in vector_map:
            return 0.0
        target = vector_map[int(observation_id)]
        neighbors = sorted(
            (
                (oid, self._cosine_distance(target, vec))
                for oid, vec in vector_map.items()
                if oid != int(observation_id)
            ),
            key=lambda item: (float(item[1]), int(item[0])),
        )
        conflicting_ids = {
            oid
            for oid, photo_id in photo_map.items()
            if oid != int(observation_id) and photo_id == photo_map.get(int(observation_id))
        }
        effective = [oid for oid, _ in neighbors if oid not in conflicting_ids]
        effective_count = min(k, len(effective))
        cluster_neighbor_count = sum(1 for oid in effective[:k] if oid in cluster_member_ids)
        return float(cluster_neighbor_count) / float(max(1, effective_count))

    def assert_member_support_ratio_formula(self, *, run_id: int, sample_size: int) -> None:
        rows = self.conn.execute(
            """
            SELECT m.support_ratio, m.observation_id, m.cluster_id
            FROM identity_cluster_member AS m
            JOIN identity_cluster AS c ON c.id = m.cluster_id
            WHERE c.run_id = ?
              AND c.cluster_stage = 'final'
              AND m.decision_status = 'retained'
              AND m.member_role IN ('anchor_core', 'core', 'boundary')
            ORDER BY m.observation_id ASC
            LIMIT ?
            """,
            (int(run_id), int(sample_size)),
        ).fetchall()
        assert rows
        for row in rows:
            expected = self.recompute_member_support_ratio(
                cluster_id=int(row["cluster_id"]),
                observation_id=int(row["observation_id"]),
            )
            actual = float(row["support_ratio"] or 0.0)
            assert math.isclose(actual, expected, rel_tol=1e-6, abs_tol=1e-6), (
                f"support_ratio 公式不一致: cluster={int(row['cluster_id'])}, "
                f"obs={int(row['observation_id'])}, actual={actual}, expected={expected}"
            )

    def assert_intra_photo_conflict_ratio_formula(self, *, run_id: int) -> None:
        rows = self.conn.execute(
            """
            SELECT id, intra_photo_conflict_ratio
            FROM identity_cluster
            WHERE run_id = ?
              AND cluster_stage = 'final'
            ORDER BY id ASC
            """,
            (int(run_id),),
        ).fetchall()
        assert rows
        for row in rows:
            members = self.conn.execute(
                """
                SELECT fo.photo_asset_id
                FROM identity_cluster_member AS m
                JOIN face_observation AS fo ON fo.id = m.observation_id
                WHERE m.cluster_id = ?
                  AND m.decision_status = 'retained'
                """,
                (int(row["id"]),),
            ).fetchall()
            photo_ids = [int(member["photo_asset_id"]) for member in members]
            total = len(photo_ids)
            if total < 2:
                expected = 0.0
            else:
                conflict_pairs = 0
                total_pairs = total * (total - 1) // 2
                for i in range(total):
                    for j in range(i + 1, total):
                        if photo_ids[i] == photo_ids[j]:
                            conflict_pairs += 1
                expected = float(conflict_pairs) / float(total_pairs)
            actual = float(row["intra_photo_conflict_ratio"] or 0.0)
            assert math.isclose(actual, expected, rel_tol=1e-6, abs_tol=1e-6), (
                f"intra_photo_conflict_ratio 公式不一致: cluster={int(row['id'])}, actual={actual}, expected={expected}"
            )

    def assert_existence_gate_reason_consistent(self, *, run_id: int) -> None:
        rows = self.conn.execute(
            """
            SELECT c.id, c.cluster_state, c.discard_reason_code, r.resolution_state, r.resolution_reason
            FROM identity_cluster AS c
            JOIN identity_cluster_resolution AS r ON r.cluster_id = c.id
            WHERE c.run_id = ?
              AND c.cluster_stage = 'final'
            ORDER BY c.id ASC
            """,
            (int(run_id),),
        ).fetchall()
        assert rows
        for row in rows:
            cluster_state = str(row["cluster_state"])
            resolution_state = str(row["resolution_state"])
            if cluster_state == "discarded":
                assert resolution_state == "discarded"
                assert str(row["discard_reason_code"] or "") == str(row["resolution_reason"] or "")
            else:
                assert resolution_state in {"unresolved", "review_pending"}
                assert row["discard_reason_code"] is None

    def assert_final_gate_metrics_frozen_before_attachment(self, *, run_id: int) -> None:
        rows = self.conn.execute(
            """
            SELECT id, retained_member_count, anchor_core_count, core_count, boundary_count, attachment_count
            FROM identity_cluster
            WHERE run_id = ?
              AND cluster_stage = 'final'
            ORDER BY id ASC
            """,
            (int(run_id),),
        ).fetchall()
        assert rows
        for row in rows:
            retained = int(row["retained_member_count"])
            expected_retained = int(row["anchor_core_count"]) + int(row["core_count"]) + int(row["boundary_count"])
            assert retained == expected_retained
            assert int(row["attachment_count"]) >= 0

    def assert_cluster_discard_reason_equals_resolution_reason(self, *, run_id: int) -> None:
        self.assert_existence_gate_reason_consistent(run_id=run_id)

    def assert_known_topology_contract(self, *, run_id: int) -> None:
        final_clusters = self.list_clusters(run_id=run_id, cluster_stage="final")
        assert final_clusters
        members = self.list_cluster_members(run_id=run_id)
        final_members = [item for item in members if str(item["cluster_stage"]) == "final"]
        assert final_members
        assert any(item["member_role"] == "anchor_core" for item in final_members if item["decision_status"] == "retained")

        for cluster in final_clusters:
            cluster_id = int(cluster["id"])
            retained = [m for m in final_members if int(m["cluster_id"]) == cluster_id and m["decision_status"] == "retained"]
            if not retained:
                continue

            representative = next((m for m in retained if int(m["is_representative"]) == 1), None)
            assert representative is not None
            obs_ids = [int(m["observation_id"]) for m in retained]
            vectors = self._load_vectors(obs_ids)
            sums = {
                obs_id: sum(self._cosine_distance(vectors[obs_id], vectors[other]) for other in obs_ids)
                for obs_id in obs_ids
            }
            expected_medoid = min(sums.items(), key=lambda item: (float(item[1]), int(item[0])))[0]
            assert int(representative["observation_id"]) == int(expected_medoid)

            profile = self._get_cluster_profile_for_cluster(cluster_id=cluster_id)
            min_samples = int(profile["density_min_samples"])
            for member in retained:
                obs_id = int(member["observation_id"])
                if len(obs_ids) <= 1:
                    expected_radius = 0.0
                else:
                    distances = sorted(
                        self._cosine_distance(vectors[obs_id], vectors[other])
                        for other in obs_ids
                        if other != obs_id
                    )
                    idx = min(max(0, min_samples - 1), len(distances) - 1)
                    expected_radius = float(distances[idx])
                actual_radius = float(member["density_radius"] or 0.0)
                assert math.isclose(actual_radius, expected_radius, rel_tol=1e-6, abs_tol=1e-6)

            core_distances = sorted(
                float(m["distance_to_medoid"] or 0.0)
                for m in retained
                if m["member_role"] in {"anchor_core", "core", "boundary"}
            )
            if core_distances:
                quantile = float(profile["anchor_core_radius_quantile"])
                expected_anchor_radius = self._quantile(core_distances, quantile)
                summary = json.loads(str(cluster["summary_json"])) if cluster["summary_json"] else {}
                actual_anchor_radius = float(summary.get("anchor_core_radius", 0.0))
                assert math.isclose(actual_anchor_radius, expected_anchor_radius, rel_tol=1e-6, abs_tol=1e-6)

            for member in retained:
                if member["member_role"] in {"anchor_core", "core", "boundary"}:
                    expected_support = self.recompute_member_support_ratio(
                        cluster_id=cluster_id,
                        observation_id=int(member["observation_id"]),
                    )
                    actual_support = float(member["support_ratio"] or 0.0)
                    assert math.isclose(actual_support, expected_support, rel_tol=1e-6, abs_tol=1e-6)

        raw_clusters = self.list_clusters(run_id=run_id, cluster_stage="raw")
        assert raw_clusters
        for cluster in raw_clusters:
            summary = json.loads(str(cluster["summary_json"])) if cluster["summary_json"] else {}
            mutual_edges = summary.get("mutual_knn_edges") or []
            for edge in mutual_edges:
                if not isinstance(edge, list) or len(edge) != 2:
                    continue
                a = int(edge[0])
                b = int(edge[1])
                reverse = [b, a]
                assert reverse in mutual_edges or a == b

    def _load_vectors(self, observation_ids: list[int]) -> dict[int, np.ndarray]:
        if not observation_ids:
            return {}
        placeholders = ", ".join("?" for _ in observation_ids)
        rows = self.conn.execute(
            f"""
            SELECT face_observation_id AS observation_id, vector_blob
            FROM face_embedding
            WHERE face_observation_id IN ({placeholders})
              AND feature_type = 'face'
              AND model_key = 'insightface'
              AND normalized = 1
            """,
            tuple(int(obs_id) for obs_id in observation_ids),
        ).fetchall()
        return {
            int(row["observation_id"]): np.frombuffer(row["vector_blob"], dtype=np.float32).copy()
            for row in rows
            if isinstance(row["vector_blob"], (bytes, bytearray, memoryview))
        }

    def _get_cluster_profile_for_cluster(self, *, cluster_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT p.*
            FROM identity_cluster AS c
            JOIN identity_cluster_run AS run_ref ON run_ref.id = c.run_id
            JOIN identity_cluster_profile AS p ON p.id = run_ref.cluster_profile_id
            WHERE c.id = ?
            """,
            (int(cluster_id),),
        ).fetchone()
        if row is None:
            raise AssertionError(f"cluster profile 不存在: {int(cluster_id)}")
        return dict(row)

    def _quantile(self, values: list[float], q: float) -> float:
        if not values:
            return 0.0
        q = min(1.0, max(0.0, float(q)))
        if len(values) == 1:
            return float(values[0])
        pos = q * float(len(values) - 1)
        low = int(math.floor(pos))
        high = int(math.ceil(pos))
        if low == high:
            return float(values[low])
        fraction = pos - float(low)
        return float(values[low] + (values[high] - values[low]) * fraction)

    def _ensure_source(self) -> int:
        row = self.conn.execute("SELECT id FROM library_source ORDER BY id ASC LIMIT 1").fetchone()
        if row is not None:
            return int(row["id"])
        return SourceRepo(self.conn).add_source(
            name="fixture-source",
            root_path=str(self.root / "source"),
            root_fingerprint="fixture-fp",
            active=True,
        )

    def _reset_observations(self) -> None:
        self.conn.execute("DELETE FROM face_embedding")
        self.conn.execute("DELETE FROM face_observation")
        self.conn.execute("DELETE FROM photo_asset")
        self.conn.execute("DELETE FROM identity_observation_pool_entry")

    def _insert_photo(self, source_id: int, image_path: Path, capture_second: int) -> int:
        asset_repo = AssetRepo(self.conn)
        photo_id = asset_repo.add_photo_asset(source_id, str(image_path), processing_status="assignment_done")
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        capture_datetime = (base + timedelta(seconds=int(capture_second))).isoformat(timespec="seconds")
        self.conn.execute(
            """
            UPDATE photo_asset
            SET capture_datetime = ?,
                capture_month = '2025-01',
                primary_fingerprint = ?,
                is_heic = 0
            WHERE id = ?
            """,
            (capture_datetime, f"fp-{photo_id}", photo_id),
        )
        return int(photo_id)

    def _insert_observation(
        self,
        photo_id: int,
        *,
        bbox: tuple[float, float, float, float],
        area_ratio: float,
        pose: float,
        quality: float,
    ) -> int:
        row = self.conn.execute(
            """
            INSERT INTO face_observation(
                photo_asset_id,
                bbox_top,
                bbox_right,
                bbox_bottom,
                bbox_left,
                face_area_ratio,
                sharpness_score,
                pose_score,
                quality_score,
                active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (photo_id, bbox[0], bbox[1], bbox[2], bbox[3], area_ratio, 1.0, pose, quality),
        )
        return int(row.lastrowid)

    def _insert_embedding(self, observation_id: int, vector: np.ndarray) -> None:
        vector_array = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(vector_array))
        if norm > 0.0:
            vector_array = vector_array / norm
        self.conn.execute(
            """
            INSERT INTO face_embedding(
                face_observation_id,
                feature_type,
                model_key,
                dimension,
                vector_blob,
                normalized
            )
            VALUES (?, 'face', 'insightface', ?, ?, 1)
            """,
            (int(observation_id), int(vector_array.size), vector_array.tobytes()),
        )

    def _cosine_distance(self, a: np.ndarray, b: np.ndarray) -> float:
        denom = float(np.linalg.norm(a) * np.linalg.norm(b))
        if denom <= 0.0:
            return 1.0
        score = float(np.dot(a, b) / denom)
        return float(max(0.0, 1.0 - score))

    def _make_image(self, name: str, *, pattern: str) -> Path:
        image_path = self.root / "seed-images" / name
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (64, 64), color=(180, 180, 180))
        draw = ImageDraw.Draw(image)
        if pattern == "diag":
            draw.line((0, 0, 63, 63), fill=(20, 20, 20), width=4)
            draw.line((0, 63, 63, 0), fill=(60, 60, 60), width=2)
        elif pattern == "checker":
            for x in range(0, 64, 8):
                for y in range(0, 64, 8):
                    if (x + y) % 16 == 0:
                        draw.rectangle((x, y, x + 7, y + 7), fill=(40, 40, 40))
        elif pattern == "stripe":
            for x in range(0, 64, 6):
                draw.rectangle((x, 0, x + 2, 63), fill=(30, 30, 30))
        else:
            draw.ellipse((16, 16, 48, 48), fill=(90, 90, 90))
        image.save(image_path, format="JPEG")
        return image_path


def build_identity_phase1_workspace(root: Path) -> IdentityPhase1Workspace:
    paths = init_workspace_layout(root, root / ".hikbox")
    conn = connect_db(paths.db_path)
    apply_migrations(conn)

    threshold_count_row = conn.execute(
        "SELECT COUNT(*) AS c FROM identity_threshold_profile"
    ).fetchone()
    if threshold_count_row is None or int(threshold_count_row["c"]) <= 0:
        conn.execute(
            """
            INSERT INTO identity_threshold_profile(
                profile_name,
                profile_version,
                quality_formula_version,
                embedding_feature_type,
                embedding_model_key,
                embedding_distance_metric,
                embedding_schema_version,
                quality_area_weight,
                quality_sharpness_weight,
                quality_pose_weight,
                area_log_p10,
                area_log_p90,
                sharpness_log_p10,
                sharpness_log_p90,
                pose_score_p10,
                pose_score_p90,
                low_quality_threshold,
                high_quality_threshold,
                trusted_seed_quality_threshold,
                bootstrap_edge_accept_threshold,
                bootstrap_edge_candidate_threshold,
                bootstrap_margin_threshold,
                bootstrap_min_cluster_size,
                bootstrap_min_distinct_photo_count,
                bootstrap_min_high_quality_count,
                bootstrap_seed_min_count,
                bootstrap_seed_max_count,
                assignment_auto_min_quality,
                assignment_auto_distance_threshold,
                assignment_auto_margin_threshold,
                assignment_review_distance_threshold,
                assignment_require_photo_conflict_free,
                trusted_min_quality,
                trusted_centroid_distance_threshold,
                trusted_margin_threshold,
                trusted_block_exact_duplicate,
                trusted_block_burst_duplicate,
                burst_time_window_seconds,
                possible_merge_distance_threshold,
                possible_merge_margin_threshold,
                active,
                activated_at
            )
            VALUES (
                'fixture-threshold',
                'v3_1.fixture',
                'quality.v2',
                'face',
                'insightface',
                'cosine',
                'embedding.v1',
                0.40,
                0.30,
                0.30,
                -6.0,
                2.0,
                -6.0,
                2.0,
                0.0,
                1.0,
                0.45,
                0.75,
                0.60,
                0.20,
                0.28,
                0.05,
                2,
                2,
                1,
                2,
                5,
                0.55,
                0.35,
                0.08,
                0.45,
                1,
                0.60,
                0.35,
                0.08,
                1,
                1,
                60,
                0.30,
                0.05,
                1,
                CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()

    observation_row = conn.execute(
        """
        SELECT id
        FROM identity_observation_profile
        WHERE active = 1
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    cluster_row = conn.execute(
        """
        SELECT id
        FROM identity_cluster_profile
        WHERE active = 1
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if observation_row is None or cluster_row is None:
        raise RuntimeError("缺少 v3.1 observation/cluster active profile")

    return IdentityPhase1Workspace(
        root=root,
        paths=paths,
        conn=conn,
        observation_profile_id=int(observation_row["id"]),
        cluster_profile_id=int(cluster_row["id"]),
    )
