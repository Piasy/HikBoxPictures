from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import sqlite3

import numpy as np

from .fixtures_workspace import build_identity_seed_workspace


@dataclass(slots=True)
class IdentityV31ExportWorkspace:
    root: Path
    conn: sqlite3.Connection
    base_run_id: int
    latest_non_target_run_id: int
    failed_run_id: int
    snapshot_id: int
    cluster_ids: dict[str, int]
    observation_ids: dict[str, int]
    photo_ids: dict[str, int]
    embedding_probe_expected_dim: int
    embedding_probe_expected_model_key: str
    selected_snapshot_embedding_model_key: str
    latest_visible_profile_embedding_model_key: str
    expected_cluster_ids_by_run_id: dict[int, set[int]]
    expected_candidate_ids_by_run_id_and_source: dict[tuple[int, str], set[int]]

    def close(self) -> None:
        self.conn.close()


def _normalize_embedding(vector: list[float]) -> np.ndarray:
    arr = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if norm <= 0.0:
        raise ValueError("embedding 向量范数必须大于 0")
    return arr / norm


def _insert_photo(conn: sqlite3.Connection, *, source_id: int, path: Path, capture_second: int) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fixture-image")
    row_id = conn.execute(
        """
        INSERT INTO photo_asset(
            library_source_id,
            primary_path,
            processing_status,
            capture_datetime,
            capture_month
        )
        VALUES (?, ?, 'assignment_done', ?, '2026-04')
        """,
        (
            int(source_id),
            str(path.resolve()),
            f"2026-04-18T08:{int(capture_second // 60):02d}:{int(capture_second % 60):02d}+08:00",
        ),
    ).lastrowid
    assert row_id is not None
    return int(row_id)


def _insert_observation(
    conn: sqlite3.Connection,
    *,
    photo_id: int,
    quality: float,
    active: bool = True,
) -> int:
    row_id = conn.execute(
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
            crop_path,
            detector_key,
            detector_version,
            active
        )
        VALUES (?, 0.10, 0.90, 0.90, 0.10, 0.24, ?, 0.92, ?, NULL, 'fixture', 'identity-v3-1-export', ?)
        """,
        (
            int(photo_id),
            float(quality + 0.1),
            float(quality),
            1 if active else 0,
        ),
    ).lastrowid
    assert row_id is not None
    return int(row_id)


def _insert_embedding(
    conn: sqlite3.Connection,
    *,
    observation_id: int,
    model_key: str,
    vector: list[float],
    normalized: int = 1,
    feature_type: str = "face",
) -> None:
    embedding = _normalize_embedding(vector) if int(normalized) == 1 else np.asarray(vector, dtype=np.float32)
    conn.execute(
        """
        INSERT INTO face_embedding(
            face_observation_id,
            feature_type,
            model_key,
            dimension,
            vector_blob,
            normalized
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            int(observation_id),
            str(feature_type),
            str(model_key),
            int(embedding.size),
            embedding.tobytes(),
            int(normalized),
        ),
    )


def _insert_snapshot(conn: sqlite3.Connection, *, profile_id: int, suffix: str, status: str = "succeeded") -> int:
    row_id = conn.execute(
        """
        INSERT INTO identity_observation_snapshot(
            observation_profile_id,
            dataset_hash,
            candidate_policy_hash,
            max_knn_supported,
            algorithm_version,
            summary_json,
            status,
            started_at,
            finished_at
        )
        VALUES (?, ?, ?, 24, 'identity.snapshot.v3_1.export-fixture', '{}', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (int(profile_id), f"fixture-dataset-{suffix}", f"fixture-policy-{suffix}", str(status)),
    ).lastrowid
    assert row_id is not None
    return int(row_id)


def _insert_run(
    conn: sqlite3.Connection,
    *,
    snapshot_id: int,
    cluster_profile_id: int,
    run_status: str,
    is_review_target: bool,
) -> int:
    row_id = conn.execute(
        """
        INSERT INTO identity_cluster_run(
            observation_snapshot_id,
            cluster_profile_id,
            algorithm_version,
            run_status,
            summary_json,
            failure_json,
            is_review_target,
            started_at,
            finished_at
        )
        VALUES (?, ?, 'identity.cluster.v3_1.export-fixture', ?, '{}', '{}', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (
            int(snapshot_id),
            int(cluster_profile_id),
            str(run_status),
            1 if is_review_target else 0,
        ),
    ).lastrowid
    assert row_id is not None
    return int(row_id)


def _insert_cluster(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    representative_observation_id: int | None,
    cluster_state: str = "active",
    retained_member_count: int = 0,
    distinct_photo_count: int = 0,
    excluded_count: int = 0,
) -> int:
    row_id = conn.execute(
        """
        INSERT INTO identity_cluster(
            run_id,
            cluster_stage,
            cluster_state,
            member_count,
            retained_member_count,
            anchor_core_count,
            core_count,
            boundary_count,
            attachment_count,
            excluded_count,
            distinct_photo_count,
            representative_observation_id,
            summary_json
        )
        VALUES (?, 'final', ?, ?, ?, 0, 0, 0, 0, ?, ?, ?, '{}')
        """,
        (
            int(run_id),
            str(cluster_state),
            int(retained_member_count + excluded_count),
            int(retained_member_count),
            int(excluded_count),
            int(distinct_photo_count),
            int(representative_observation_id) if representative_observation_id is not None else None,
        ),
    ).lastrowid
    assert row_id is not None
    return int(row_id)


def _insert_cluster_member(
    conn: sqlite3.Connection,
    *,
    cluster_id: int,
    observation_id: int,
    source_pool_kind: str,
    member_role: str,
    decision_status: str,
    quality: float,
    is_selected_trusted_seed: bool = False,
    is_representative: bool = False,
) -> None:
    conn.execute(
        """
        INSERT INTO identity_cluster_member(
            cluster_id,
            observation_id,
            source_pool_kind,
            quality_score_snapshot,
            member_role,
            decision_status,
            is_trusted_seed_candidate,
            is_selected_trusted_seed,
            seed_rank,
            is_representative,
            diagnostic_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')
        """,
        (
            int(cluster_id),
            int(observation_id),
            str(source_pool_kind),
            float(quality),
            str(member_role),
            str(decision_status),
            1 if is_selected_trusted_seed else 0,
            1 if is_selected_trusted_seed else 0,
            1 if is_selected_trusted_seed else None,
            1 if is_representative else 0,
        ),
    )


def _insert_resolution(conn: sqlite3.Connection, *, cluster_id: int, run_id: int, state: str, trusted_seed_count: int) -> None:
    conn.execute(
        """
        INSERT INTO identity_cluster_resolution(
            cluster_id,
            resolution_state,
            resolution_reason,
            source_run_id,
            trusted_seed_count,
            trusted_seed_candidate_count
        )
        VALUES (?, ?, 'fixture', ?, ?, ?)
        """,
        (int(cluster_id), str(state), int(run_id), int(trusted_seed_count), int(trusted_seed_count)),
    )


def _insert_pool_entry(
    conn: sqlite3.Connection,
    *,
    snapshot_id: int,
    observation_id: int,
    pool_kind: str,
    excluded_reason: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO identity_observation_pool_entry(
            snapshot_id,
            observation_id,
            pool_kind,
            quality_score_snapshot,
            dedup_group_key,
            representative_observation_id,
            excluded_reason,
            diagnostic_json
        )
        VALUES (?, ?, ?, 0.66, NULL, NULL, ?, '{}')
        """,
        (int(snapshot_id), int(observation_id), str(pool_kind), excluded_reason),
    )


def _drop_face_embedding_unique_in_fixture_db(conn: sqlite3.Connection) -> None:
    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(
        """
        CREATE TABLE face_embedding_fixture_tmp (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            face_observation_id INTEGER NOT NULL,
            feature_type TEXT NOT NULL DEFAULT 'face' CHECK (feature_type IN ('face')),
            model_key TEXT,
            dimension INTEGER,
            vector_blob BLOB,
            normalized INTEGER NOT NULL DEFAULT 1 CHECK (normalized IN (0, 1)),
            generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (face_observation_id) REFERENCES face_observation(id) ON DELETE CASCADE
        );
        INSERT INTO face_embedding_fixture_tmp(
            id, face_observation_id, feature_type, model_key, dimension, vector_blob, normalized, generated_at
        )
        SELECT
            id, face_observation_id, feature_type, model_key, dimension, vector_blob, normalized, generated_at
        FROM face_embedding;
        DROP TABLE face_embedding;
        ALTER TABLE face_embedding_fixture_tmp RENAME TO face_embedding;
        """
    )
    conn.execute("PRAGMA foreign_keys = ON")


def build_identity_v3_1_export_workspace(root: Path) -> IdentityV31ExportWorkspace:
    seed_workspace = build_identity_seed_workspace(root)
    conn = seed_workspace.conn
    source_id = int(seed_workspace.source_id)
    workspace_root = seed_workspace.root

    selected_snapshot_embedding_model_key = str(seed_workspace.model_key)
    active_profile_row = conn.execute(
        """
        SELECT id, embedding_model_key
        FROM identity_observation_profile
        WHERE active = 1
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if active_profile_row is None:
        raise AssertionError("缺少 active identity_observation_profile")
    latest_visible_profile_embedding_model_key = str(active_profile_row["embedding_model_key"])
    if latest_visible_profile_embedding_model_key == selected_snapshot_embedding_model_key:
        latest_visible_profile_embedding_model_key = "insightface-visible-v2"

    conn.execute(
        """
        UPDATE identity_observation_profile
        SET active = 0, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (int(active_profile_row["id"]),),
    )

    selected_profile_id = conn.execute(
        """
        INSERT INTO identity_observation_profile(
            profile_name,
            profile_version,
            embedding_feature_type,
            embedding_model_key,
            embedding_distance_metric,
            embedding_schema_version,
            quality_formula_version,
            quality_area_weight,
            quality_sharpness_weight,
            quality_pose_weight,
            core_quality_threshold,
            attachment_quality_threshold,
            exact_duplicate_distance_threshold,
            same_photo_keep_best,
            burst_window_seconds,
            burst_duplicate_distance_threshold,
            pool_exclusion_rules_version,
            active
        )
        SELECT
            profile_name || '-selected',
            profile_version || '.selected',
            embedding_feature_type,
            ?,
            embedding_distance_metric,
            embedding_schema_version,
            quality_formula_version,
            quality_area_weight,
            quality_sharpness_weight,
            quality_pose_weight,
            core_quality_threshold,
            attachment_quality_threshold,
            exact_duplicate_distance_threshold,
            same_photo_keep_best,
            burst_window_seconds,
            burst_duplicate_distance_threshold,
            pool_exclusion_rules_version,
            0
        FROM identity_observation_profile
        WHERE id = ?
        """,
        (selected_snapshot_embedding_model_key, int(active_profile_row["id"])),
    ).lastrowid
    assert selected_profile_id is not None
    latest_visible_profile_id = conn.execute(
        """
        INSERT INTO identity_observation_profile(
            profile_name,
            profile_version,
            embedding_feature_type,
            embedding_model_key,
            embedding_distance_metric,
            embedding_schema_version,
            quality_formula_version,
            quality_area_weight,
            quality_sharpness_weight,
            quality_pose_weight,
            core_quality_threshold,
            attachment_quality_threshold,
            exact_duplicate_distance_threshold,
            same_photo_keep_best,
            burst_window_seconds,
            burst_duplicate_distance_threshold,
            pool_exclusion_rules_version,
            active,
            activated_at
        )
        SELECT
            profile_name || '-latest',
            profile_version || '.latest-visible',
            embedding_feature_type,
            ?,
            embedding_distance_metric,
            embedding_schema_version,
            quality_formula_version,
            quality_area_weight,
            quality_sharpness_weight,
            quality_pose_weight,
            core_quality_threshold,
            attachment_quality_threshold,
            exact_duplicate_distance_threshold,
            same_photo_keep_best,
            burst_window_seconds,
            burst_duplicate_distance_threshold,
            pool_exclusion_rules_version,
            1,
            CURRENT_TIMESTAMP
        FROM identity_observation_profile
        WHERE id = ?
        """,
        (latest_visible_profile_embedding_model_key, int(active_profile_row["id"])),
    ).lastrowid
    assert latest_visible_profile_id is not None

    cluster_profile_row = conn.execute(
        """
        SELECT id
        FROM identity_cluster_profile
        WHERE active = 1
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if cluster_profile_row is None:
        raise AssertionError("缺少 active identity_cluster_profile")
    cluster_profile_id = int(cluster_profile_row["id"])

    snapshot_base = _insert_snapshot(conn, profile_id=int(selected_profile_id), suffix="base", status="succeeded")
    snapshot_latest = _insert_snapshot(
        conn,
        profile_id=int(latest_visible_profile_id),
        suffix="latest",
        status="succeeded",
    )
    snapshot_failed = _insert_snapshot(conn, profile_id=int(selected_profile_id), suffix="failed", status="failed")

    base_run_id = _insert_run(
        conn,
        snapshot_id=snapshot_base,
        cluster_profile_id=cluster_profile_id,
        run_status="succeeded",
        is_review_target=True,
    )
    latest_non_target_run_id = _insert_run(
        conn,
        snapshot_id=snapshot_latest,
        cluster_profile_id=cluster_profile_id,
        run_status="succeeded",
        is_review_target=False,
    )
    failed_run_id = _insert_run(
        conn,
        snapshot_id=snapshot_failed,
        cluster_profile_id=cluster_profile_id,
        run_status="failed",
        is_review_target=False,
    )

    photo_ids: dict[str, int] = {}
    observation_ids: dict[str, int] = {}

    def add_observation(
        *,
        key: str,
        photo_key: str,
        capture_second: int,
        quality: float,
        model_key: str | None,
        vector: list[float] | None,
        active: bool = True,
    ) -> int:
        if photo_key not in photo_ids:
            photo_ids[photo_key] = _insert_photo(
                conn,
                source_id=source_id,
                path=workspace_root / "identity-v3-1-export-input" / f"{photo_key}.jpg",
                capture_second=capture_second,
            )
        obs_id = _insert_observation(conn, photo_id=photo_ids[photo_key], quality=quality, active=active)
        observation_ids[key] = obs_id
        if model_key is not None and vector is not None:
            _insert_embedding(conn, observation_id=obs_id, model_key=model_key, vector=vector, normalized=1)
        return obs_id

    add_observation(
        key="seed_primary_a",
        photo_key="seed-primary-a",
        capture_second=1,
        quality=0.96,
        model_key=selected_snapshot_embedding_model_key,
        vector=[0.99, 0.01, 0.00, 0.00],
    )
    add_observation(
        key="seed_primary_b",
        photo_key="seed-primary-b",
        capture_second=2,
        quality=0.94,
        model_key=selected_snapshot_embedding_model_key,
        vector=[0.97, 0.03, 0.00, 0.00],
    )
    add_observation(
        key="seed_primary_c",
        photo_key="seed-primary-c",
        capture_second=3,
        quality=0.90,
        model_key=selected_snapshot_embedding_model_key,
        vector=[0.95, 0.05, 0.00, 0.00],
    )
    add_observation(
        key="seed_fallback_a",
        photo_key="seed-fallback-a",
        capture_second=4,
        quality=0.88,
        model_key=selected_snapshot_embedding_model_key,
        vector=[0.00, 0.95, 0.05, 0.00],
    )
    add_observation(
        key="seed_fallback_b",
        photo_key="seed-fallback-b",
        capture_second=5,
        quality=0.86,
        model_key=selected_snapshot_embedding_model_key,
        vector=[0.00, 0.93, 0.07, 0.00],
    )
    add_observation(
        key="seed_invalid_a",
        photo_key="seed-invalid-a",
        capture_second=6,
        quality=0.80,
        model_key=None,
        vector=None,
    )
    add_observation(
        key="seed_invalid_b",
        photo_key="seed-invalid-b",
        capture_second=7,
        quality=0.79,
        model_key=None,
        vector=None,
    )
    add_observation(
        key="pending_attachment_overlap",
        photo_key="pending-overlap",
        capture_second=8,
        quality=0.74,
        model_key=selected_snapshot_embedding_model_key,
        vector=[0.70, 0.20, 0.10, 0.00],
    )
    add_observation(
        key="pending_promotable_peer",
        photo_key="pending-peer",
        capture_second=9,
        quality=0.73,
        model_key=selected_snapshot_embedding_model_key,
        vector=[0.68, 0.22, 0.10, 0.00],
    )
    add_observation(
        key="embedding_probe",
        photo_key="embedding-probe",
        capture_second=10,
        quality=0.69,
        model_key=selected_snapshot_embedding_model_key,
        vector=[0.45, 0.44, 0.11, 0.00],
    )
    add_observation(
        key="attachment_auto",
        photo_key="attachment-auto",
        capture_second=11,
        quality=0.71,
        model_key=selected_snapshot_embedding_model_key,
        vector=[0.89, 0.10, 0.01, 0.00],
    )
    photo_ids["attachment_auto"] = photo_ids["attachment-auto"]
    add_observation(
        key="attachment_review_margin",
        photo_key="attachment-review-margin",
        capture_second=12,
        quality=0.67,
        model_key=selected_snapshot_embedding_model_key,
        vector=[0.80, 0.18, 0.02, 0.00],
    )
    photo_ids["seed_primary_a"] = photo_ids["seed-primary-a"]
    photo_ids["attachment_same_photo_conflict"] = photo_ids["seed-primary-a"]
    same_photo_conflict_obs = _insert_observation(
        conn,
        photo_id=photo_ids["attachment_same_photo_conflict"],
        quality=0.66,
        active=True,
    )
    observation_ids["attachment_same_photo_conflict"] = same_photo_conflict_obs
    _insert_embedding(
        conn,
        observation_id=same_photo_conflict_obs,
        model_key=selected_snapshot_embedding_model_key,
        vector=[0.91, 0.08, 0.01, 0.00],
        normalized=1,
    )
    add_observation(
        key="attachment_reject",
        photo_key="attachment-reject",
        capture_second=13,
        quality=0.55,
        model_key=selected_snapshot_embedding_model_key,
        vector=[0.20, 0.20, 0.60, 0.00],
    )
    add_observation(
        key="attachment_missing_embedding",
        photo_key="attachment-missing-embedding",
        capture_second=14,
        quality=0.60,
        model_key=None,
        vector=None,
    )
    add_observation(
        key="attachment_dim_mismatch",
        photo_key="attachment-dim-mismatch",
        capture_second=15,
        quality=0.62,
        model_key=selected_snapshot_embedding_model_key,
        vector=[0.30, 0.30, 0.40],
    )
    add_observation(
        key="other_snapshot_attachment",
        photo_key="other-snapshot-attachment",
        capture_second=16,
        quality=0.70,
        model_key=latest_visible_profile_embedding_model_key,
        vector=[0.55, 0.35, 0.10, 0.00],
    )
    add_observation(
        key="warmup_active",
        photo_key="warmup-active",
        capture_second=17,
        quality=0.65,
        model_key=selected_snapshot_embedding_model_key,
        vector=[0.11, 0.11, 0.78, 0.00],
    )
    add_observation(
        key="other_run_seed_a",
        photo_key="other-run-seed-a",
        capture_second=18,
        quality=0.90,
        model_key=latest_visible_profile_embedding_model_key,
        vector=[0.00, 0.00, 0.99, 0.01],
    )
    add_observation(
        key="other_run_seed_b",
        photo_key="other-run-seed-b",
        capture_second=19,
        quality=0.88,
        model_key=latest_visible_profile_embedding_model_key,
        vector=[0.00, 0.00, 0.97, 0.03],
    )
    add_observation(
        key="other_run_pending",
        photo_key="other-run-pending",
        capture_second=20,
        quality=0.73,
        model_key=latest_visible_profile_embedding_model_key,
        vector=[0.05, 0.05, 0.88, 0.02],
    )

    cluster_ids: dict[str, int] = {}
    cluster_ids["seed_primary"] = _insert_cluster(
        conn,
        run_id=base_run_id,
        representative_observation_id=observation_ids["seed_primary_a"],
        retained_member_count=3,
        distinct_photo_count=3,
    )
    _insert_cluster_member(
        conn,
        cluster_id=cluster_ids["seed_primary"],
        observation_id=observation_ids["seed_primary_a"],
        source_pool_kind="core_discovery",
        member_role="anchor_core",
        decision_status="retained",
        quality=0.96,
        is_selected_trusted_seed=True,
        is_representative=True,
    )
    _insert_cluster_member(
        conn,
        cluster_id=cluster_ids["seed_primary"],
        observation_id=observation_ids["seed_primary_b"],
        source_pool_kind="core_discovery",
        member_role="core",
        decision_status="retained",
        quality=0.94,
        is_selected_trusted_seed=True,
    )
    _insert_cluster_member(
        conn,
        cluster_id=cluster_ids["seed_primary"],
        observation_id=observation_ids["seed_primary_c"],
        source_pool_kind="core_discovery",
        member_role="boundary",
        decision_status="retained",
        quality=0.90,
    )
    _insert_resolution(
        conn,
        cluster_id=cluster_ids["seed_primary"],
        run_id=base_run_id,
        state="materialized",
        trusted_seed_count=2,
    )

    cluster_ids["seed_fallback"] = _insert_cluster(
        conn,
        run_id=base_run_id,
        representative_observation_id=observation_ids["seed_fallback_a"],
        retained_member_count=2,
        distinct_photo_count=2,
    )
    _insert_cluster_member(
        conn,
        cluster_id=cluster_ids["seed_fallback"],
        observation_id=observation_ids["seed_fallback_a"],
        source_pool_kind="core_discovery",
        member_role="core",
        decision_status="retained",
        quality=0.88,
        is_representative=True,
    )
    _insert_cluster_member(
        conn,
        cluster_id=cluster_ids["seed_fallback"],
        observation_id=observation_ids["seed_fallback_b"],
        source_pool_kind="core_discovery",
        member_role="core",
        decision_status="retained",
        quality=0.86,
    )
    _insert_resolution(
        conn,
        cluster_id=cluster_ids["seed_fallback"],
        run_id=base_run_id,
        state="materialized",
        trusted_seed_count=0,
    )

    cluster_ids["seed_invalid"] = _insert_cluster(
        conn,
        run_id=base_run_id,
        representative_observation_id=observation_ids["seed_invalid_a"],
        retained_member_count=2,
        distinct_photo_count=2,
    )
    _insert_cluster_member(
        conn,
        cluster_id=cluster_ids["seed_invalid"],
        observation_id=observation_ids["seed_invalid_a"],
        source_pool_kind="core_discovery",
        member_role="core",
        decision_status="retained",
        quality=0.80,
        is_representative=True,
    )
    _insert_cluster_member(
        conn,
        cluster_id=cluster_ids["seed_invalid"],
        observation_id=observation_ids["seed_invalid_b"],
        source_pool_kind="core_discovery",
        member_role="core",
        decision_status="retained",
        quality=0.79,
    )
    _insert_resolution(
        conn,
        cluster_id=cluster_ids["seed_invalid"],
        run_id=base_run_id,
        state="materialized",
        trusted_seed_count=1,
    )

    cluster_ids["pending_promotable"] = _insert_cluster(
        conn,
        run_id=base_run_id,
        representative_observation_id=observation_ids["pending_attachment_overlap"],
        retained_member_count=2,
        distinct_photo_count=2,
    )
    _insert_cluster_member(
        conn,
        cluster_id=cluster_ids["pending_promotable"],
        observation_id=observation_ids["pending_attachment_overlap"],
        source_pool_kind="attachment",
        member_role="attachment",
        decision_status="retained",
        quality=0.74,
        is_representative=True,
    )
    _insert_cluster_member(
        conn,
        cluster_id=cluster_ids["pending_promotable"],
        observation_id=observation_ids["pending_promotable_peer"],
        source_pool_kind="core_discovery",
        member_role="core",
        decision_status="retained",
        quality=0.73,
    )
    _insert_resolution(
        conn,
        cluster_id=cluster_ids["pending_promotable"],
        run_id=base_run_id,
        state="review_pending",
        trusted_seed_count=0,
    )

    cluster_ids["discarded_final"] = _insert_cluster(
        conn,
        run_id=base_run_id,
        representative_observation_id=observation_ids["attachment_reject"],
        cluster_state="discarded",
        retained_member_count=0,
        excluded_count=1,
        distinct_photo_count=1,
    )
    _insert_cluster_member(
        conn,
        cluster_id=cluster_ids["discarded_final"],
        observation_id=observation_ids["attachment_reject"],
        source_pool_kind="excluded",
        member_role="excluded",
        decision_status="rejected",
        quality=0.55,
        is_representative=True,
    )
    _insert_resolution(
        conn,
        cluster_id=cluster_ids["discarded_final"],
        run_id=base_run_id,
        state="discarded",
        trusted_seed_count=0,
    )

    cluster_ids["other_run_materialized"] = _insert_cluster(
        conn,
        run_id=latest_non_target_run_id,
        representative_observation_id=observation_ids["other_run_seed_a"],
        retained_member_count=2,
        distinct_photo_count=2,
    )
    _insert_cluster_member(
        conn,
        cluster_id=cluster_ids["other_run_materialized"],
        observation_id=observation_ids["other_run_seed_a"],
        source_pool_kind="core_discovery",
        member_role="core",
        decision_status="retained",
        quality=0.90,
        is_representative=True,
    )
    _insert_cluster_member(
        conn,
        cluster_id=cluster_ids["other_run_materialized"],
        observation_id=observation_ids["other_run_seed_b"],
        source_pool_kind="core_discovery",
        member_role="core",
        decision_status="retained",
        quality=0.88,
    )
    _insert_resolution(
        conn,
        cluster_id=cluster_ids["other_run_materialized"],
        run_id=latest_non_target_run_id,
        state="materialized",
        trusted_seed_count=1,
    )

    cluster_ids["other_run_pending"] = _insert_cluster(
        conn,
        run_id=latest_non_target_run_id,
        representative_observation_id=observation_ids["other_run_pending"],
        retained_member_count=1,
        distinct_photo_count=1,
    )
    _insert_cluster_member(
        conn,
        cluster_id=cluster_ids["other_run_pending"],
        observation_id=observation_ids["other_run_pending"],
        source_pool_kind="attachment",
        member_role="attachment",
        decision_status="retained",
        quality=0.73,
        is_representative=True,
    )
    _insert_resolution(
        conn,
        cluster_id=cluster_ids["other_run_pending"],
        run_id=latest_non_target_run_id,
        state="review_pending",
        trusted_seed_count=0,
    )

    base_attachment_keys = [
        "pending_attachment_overlap",
        "embedding_probe",
        "attachment_auto",
        "attachment_review_margin",
        "attachment_same_photo_conflict",
        "attachment_reject",
        "attachment_missing_embedding",
        "attachment_dim_mismatch",
    ]
    for key in base_attachment_keys:
        _insert_pool_entry(conn, snapshot_id=snapshot_base, observation_id=observation_ids[key], pool_kind="attachment")
    _insert_pool_entry(
        conn,
        snapshot_id=snapshot_latest,
        observation_id=observation_ids["other_snapshot_attachment"],
        pool_kind="attachment",
    )
    _insert_pool_entry(
        conn,
        snapshot_id=snapshot_latest,
        observation_id=observation_ids["other_run_pending"],
        pool_kind="attachment",
    )

    _drop_face_embedding_unique_in_fixture_db(conn)
    _insert_embedding(
        conn,
        observation_id=observation_ids["embedding_probe"],
        model_key="wrong-model-key",
        vector=[0.10, 0.80, 0.10, 0.00],
        normalized=1,
    )
    _insert_embedding(
        conn,
        observation_id=observation_ids["embedding_probe"],
        model_key=selected_snapshot_embedding_model_key,
        vector=[0.30, 0.30, 0.30, 0.10],
        normalized=0,
    )

    conn.commit()

    expected_cluster_ids_by_run_id: dict[int, set[int]] = {
        base_run_id: {
            cluster_ids["seed_primary"],
            cluster_ids["seed_fallback"],
            cluster_ids["seed_invalid"],
            cluster_ids["pending_promotable"],
        },
        latest_non_target_run_id: {
            cluster_ids["other_run_materialized"],
            cluster_ids["other_run_pending"],
        },
    }
    expected_candidate_ids_by_run_id_and_source: dict[tuple[int, str], set[int]] = {
        (
            base_run_id,
            "review_pending",
        ): {
            observation_ids["pending_attachment_overlap"],
            observation_ids["pending_promotable_peer"],
        },
        (
            base_run_id,
            "attachment",
        ): {observation_ids[item] for item in base_attachment_keys},
        (
            base_run_id,
            "all",
        ): {
            observation_ids["pending_attachment_overlap"],
            observation_ids["pending_promotable_peer"],
            observation_ids["embedding_probe"],
            observation_ids["attachment_auto"],
            observation_ids["attachment_review_margin"],
            observation_ids["attachment_same_photo_conflict"],
            observation_ids["attachment_reject"],
            observation_ids["attachment_missing_embedding"],
            observation_ids["attachment_dim_mismatch"],
        },
        (
            latest_non_target_run_id,
            "review_pending",
        ): {observation_ids["other_run_pending"]},
        (
            latest_non_target_run_id,
            "attachment",
        ): {
            observation_ids["other_snapshot_attachment"],
            observation_ids["other_run_pending"],
        },
        (
            latest_non_target_run_id,
            "all",
        ): {
            observation_ids["other_snapshot_attachment"],
            observation_ids["other_run_pending"],
        },
    }

    embedding_probe_expected_dim = int(
        conn.execute(
            """
            SELECT dimension
            FROM face_embedding
            WHERE face_observation_id = ?
              AND feature_type = 'face'
              AND model_key = ?
              AND normalized = 1
            ORDER BY id ASC
            LIMIT 1
            """,
            (int(observation_ids["embedding_probe"]), selected_snapshot_embedding_model_key),
        ).fetchone()[0]
    )
    if embedding_probe_expected_dim <= 0:
        raise AssertionError("embedding_probe_expected_dim 必须大于 0")
    if selected_snapshot_embedding_model_key == latest_visible_profile_embedding_model_key:
        raise AssertionError("selected_snapshot_embedding_model_key 必须不同于 latest_visible_profile_embedding_model_key")
    if not math.isfinite(float(embedding_probe_expected_dim)):
        raise AssertionError("embedding_probe_expected_dim 必须是有限值")

    return IdentityV31ExportWorkspace(
        root=workspace_root,
        conn=conn,
        base_run_id=base_run_id,
        latest_non_target_run_id=latest_non_target_run_id,
        failed_run_id=failed_run_id,
        snapshot_id=snapshot_base,
        cluster_ids=cluster_ids,
        observation_ids=observation_ids,
        photo_ids=photo_ids,
        embedding_probe_expected_dim=embedding_probe_expected_dim,
        embedding_probe_expected_model_key=selected_snapshot_embedding_model_key,
        selected_snapshot_embedding_model_key=selected_snapshot_embedding_model_key,
        latest_visible_profile_embedding_model_key=latest_visible_profile_embedding_model_key,
        expected_cluster_ids_by_run_id=expected_cluster_ids_by_run_id,
        expected_candidate_ids_by_run_id_and_source=expected_candidate_ids_by_run_id_and_source,
    )
