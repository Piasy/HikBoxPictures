from __future__ import annotations

from pathlib import Path
import shutil

import pytest

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.db.migrator import apply_migrations


FIXTURE_DB = Path(__file__).resolve().parents[1] / "data" / "legacy-v2-small.db"


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"]) for row in rows}


def _fk_targets(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    return {str(row["table"]) for row in rows}


def _index_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
    return {str(row["name"]) for row in rows}


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
    return int(row["c"])


def test_upgrade_v2_workspace_preserves_existing_rows_and_adds_v3_contract(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-v2-small.db"
    shutil.copy2(FIXTURE_DB, db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    before_versions = [int(row["version"]) for row in conn.execute("SELECT version FROM schema_migration ORDER BY version")]
    assert before_versions == [1, 2, 3]

    before_counts = {
        "person": _table_count(conn, "person"),
        "person_face_assignment": _table_count(conn, "person_face_assignment"),
        "person_face_exclusion": _table_count(conn, "person_face_exclusion"),
        "auto_cluster_batch": _table_count(conn, "auto_cluster_batch"),
        "auto_cluster": _table_count(conn, "auto_cluster"),
        "auto_cluster_member": _table_count(conn, "auto_cluster_member"),
    }

    apply_migrations(conn)

    after_counts = {
        "person": _table_count(conn, "person"),
        "person_face_assignment": _table_count(conn, "person_face_assignment"),
        "person_face_exclusion": _table_count(conn, "person_face_exclusion"),
        "auto_cluster_batch": _table_count(conn, "auto_cluster_batch"),
        "auto_cluster": _table_count(conn, "auto_cluster"),
        "auto_cluster_member": _table_count(conn, "auto_cluster_member"),
    }
    assert after_counts == before_counts

    pfa_cols = _table_columns(conn, "person_face_assignment")
    assert "confidence" not in pfa_cols
    assert {"diagnostic_json", "threshold_profile_id"}.issubset(pfa_cols)

    migrated_assignment = conn.execute(
        """
        SELECT assignment_source, diagnostic_json, threshold_profile_id
        FROM person_face_assignment
        WHERE id = 4
        """
    ).fetchone()
    assert migrated_assignment is not None
    assert str(migrated_assignment["assignment_source"]) == "manual"
    assert str(migrated_assignment["diagnostic_json"]) == "{}"
    assert migrated_assignment["threshold_profile_id"] is None

    person_cols = _table_columns(conn, "person")
    assert {"cover_observation_id", "origin_cluster_id"}.issubset(person_cols)
    person_fk = _fk_targets(conn, "person")
    assert "auto_cluster" in person_fk
    assert "face_observation" in person_fk
    person_row = conn.execute("SELECT cover_observation_id, origin_cluster_id FROM person WHERE id = 1").fetchone()
    assert person_row is not None
    assert int(person_row["cover_observation_id"]) == 101
    assert person_row["origin_cluster_id"] is None

    batch_cols = _table_columns(conn, "auto_cluster_batch")
    assert {"batch_type", "threshold_profile_id", "scan_session_id"}.issubset(batch_cols)
    non_bootstrap_batch_count = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM auto_cluster_batch
        WHERE batch_type <> 'bootstrap'
        """
    ).fetchone()
    assert non_bootstrap_batch_count is not None
    assert int(non_bootstrap_batch_count["c"]) == 0

    cluster_cols = _table_columns(conn, "auto_cluster")
    assert {"cluster_status", "resolved_person_id", "diagnostic_json"}.issubset(cluster_cols)
    assert "confidence" not in cluster_cols
    non_discarded_cluster_count = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM auto_cluster
        WHERE cluster_status <> 'discarded'
        """
    ).fetchone()
    assert non_discarded_cluster_count is not None
    assert int(non_discarded_cluster_count["c"]) == 0
    non_empty_cluster_diagnostic_count = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM auto_cluster
        WHERE diagnostic_json <> '{}'
        """
    ).fetchone()
    assert non_empty_cluster_diagnostic_count is not None
    assert int(non_empty_cluster_diagnostic_count["c"]) == 0

    member_cols = _table_columns(conn, "auto_cluster_member")
    assert {"quality_score_snapshot", "is_seed_candidate"}.issubset(member_cols)
    seed_candidate_count = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM auto_cluster_member
        WHERE is_seed_candidate <> 0
        """
    ).fetchone()
    assert seed_candidate_count is not None
    assert int(seed_candidate_count["c"]) == 0

    assert _table_columns(conn, "identity_threshold_profile")
    assert _table_columns(conn, "person_trusted_sample")
    assert "uq_identity_threshold_profile_active" in _index_names(conn, "identity_threshold_profile")
    assert "uq_person_trusted_sample_active_observation" in _index_names(conn, "person_trusted_sample")

    exclusion_fk = _fk_targets(conn, "person_face_exclusion")
    assert "person_face_assignment" in exclusion_fk
    assert "person_face_assignment_old" not in exclusion_fk

    fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    assert fk_violations == []

    applied_versions = [int(row["version"]) for row in conn.execute("SELECT version FROM schema_migration ORDER BY version")]
    assert applied_versions == [1, 2, 3, 4]


def test_upgrade_keeps_fk_and_unique_constraints_enabled(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-v2-small-fk.db"
    shutil.copy2(FIXTURE_DB, db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)

    person_ids = [int(row["id"]) for row in conn.execute("SELECT id FROM person ORDER BY id ASC").fetchall()]
    assert len(person_ids) >= 2
    person_id = person_ids[0]
    alt_person_id = person_ids[-1]
    observation_id = int(conn.execute("SELECT id FROM face_observation ORDER BY id ASC LIMIT 1").fetchone()["id"])
    conn.execute("PRAGMA foreign_keys = ON")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO person_face_assignment(
                person_id,
                face_observation_id,
                assignment_source,
                diagnostic_json,
                active
            )
            VALUES (?, ?, 'split', '{}', 1)
            """,
            (person_id, observation_id),
        )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO person_face_assignment(
                person_id,
                face_observation_id,
                assignment_source,
                diagnostic_json,
                active
            )
            VALUES (?, ?, 'manual', '{}', 1)
            """,
            (alt_person_id, observation_id),
        )

    conn.execute(
        """
        INSERT INTO person_face_assignment(
            person_id,
            face_observation_id,
            assignment_source,
            diagnostic_json,
            active
        )
        VALUES (?, ?, 'manual', '{}', 0)
        """,
        (alt_person_id, observation_id),
    )

    with pytest.raises(sqlite3.IntegrityError):
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
                active
            )
            VALUES (
                'v3-base', '1', 'v1', 'face', 'pipeline-stub-v1', 'cosine', 'face_embedding.v1',
                0.4, 0.4, 0.2,
                -3.0, -1.0, 0.1, 2.0, 0.1, 0.9,
                0.2, 0.8, 0.7,
                0.3, 0.2, 0.05,
                3, 3, 2, 2, 8,
                0.5, 0.35, 0.05, 0.45,
                1,
                0.7, 0.3, 0.05,
                1, 1, 30, 0.25, 0.05,
                1
            )
            """
        )
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
                active
            )
            VALUES (
                'v3-alt', '1', 'v1', 'face', 'pipeline-stub-v1', 'cosine', 'face_embedding.v1',
                0.4, 0.4, 0.2,
                -3.0, -1.0, 0.1, 2.0, 0.1, 0.9,
                0.2, 0.8, 0.7,
                0.3, 0.2, 0.05,
                3, 3, 2, 2, 8,
                0.5, 0.35, 0.05, 0.45,
                1,
                0.7, 0.3, 0.05,
                1, 1, 30, 0.25, 0.05,
                1
            )
            """
        )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO person_trusted_sample(
                person_id,
                face_observation_id,
                trust_source,
                trust_score,
                quality_score_snapshot,
                threshold_profile_id,
                active
            )
            VALUES (999999, ?, 'bootstrap_seed', 1.0, 0.8, 1, 1)
            """,
            (observation_id,),
        )
