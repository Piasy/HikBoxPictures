from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.db.migrator import apply_migrations
from hikbox_pictures.repositories.asset_repo import AssetRepo
from hikbox_pictures.repositories.identity_observation_repo import IdentityObservationRepo
from hikbox_pictures.repositories.source_repo import SourceRepo
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

    def close(self) -> None:
        self.conn.close()

    def new_observation_snapshot_service(self) -> IdentityObservationSnapshotService:
        return IdentityObservationSnapshotService(
            self.conn,
            observation_repo=IdentityObservationRepo(self.conn),
            quality_backfill_service=_NoopBackfillService(self),
        )

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
