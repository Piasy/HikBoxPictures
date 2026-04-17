import json
import shutil
import sqlite3
from pathlib import Path

from hikbox_pictures.db.migrator import apply_migrations


FIXTURE_DB = Path(__file__).resolve().parents[1] / "data" / "identity-v3-phase1-small.db"


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        """
    ).fetchall()
    return {str(row[0]) for row in rows}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def test_migrate_phase1_v3_workspace_to_v3_1_runtime_truth(tmp_path):
    db_path = tmp_path / "identity-v3-phase1-small.db"
    shutil.copy2(FIXTURE_DB, db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    legacy_origin_rows = conn.execute(
        """
        SELECT id, origin_cluster_id
        FROM person
        WHERE origin_cluster_id IS NOT NULL
        """
    ).fetchall()
    legacy_origin_person_ids = {int(row["id"]) for row in legacy_origin_rows}

    apply_migrations(conn)

    expected_tables = {
        "identity_observation_profile",
        "identity_observation_snapshot",
        "identity_observation_pool_entry",
        "identity_cluster_profile",
        "identity_cluster_run",
        "identity_cluster",
        "identity_cluster_lineage",
        "identity_cluster_member",
        "identity_cluster_resolution",
        "person_cluster_origin",
    }
    assert expected_tables.issubset(_table_names(conn))

    assert "origin_cluster_id" not in _table_columns(conn, "person")
    assert {"source_run_id", "source_cluster_id", "active"}.issubset(
        _table_columns(conn, "person_face_assignment")
    )
    assert {"source_run_id", "source_cluster_id", "active"}.issubset(
        _table_columns(conn, "person_trusted_sample")
    )

    observation_profiles = conn.execute(
        "SELECT COUNT(*) AS c FROM identity_observation_profile"
    ).fetchone()
    cluster_profiles = conn.execute(
        "SELECT COUNT(*) AS c FROM identity_cluster_profile"
    ).fetchone()
    assert int(observation_profiles["c"]) >= 1
    assert int(cluster_profiles["c"]) >= 1

    origin_rows = conn.execute(
        """
        SELECT person_id, origin_cluster_id, source_run_id, active
        FROM person_cluster_origin
        """
    ).fetchall()
    assert len(origin_rows) >= len(legacy_origin_rows)
    assert {int(row["person_id"]) for row in origin_rows}.issuperset(legacy_origin_person_ids)

    run_table = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'identity_cluster_run'
        """
    ).fetchone()
    assert run_table is not None
    run_table_sql = str(run_table["sql"])
    assert "created" in run_table_sql
    assert "running" in run_table_sql
    assert "succeeded" in run_table_sql
    assert "failed" in run_table_sql
    assert "cancelled" in run_table_sql

    run_index_rows = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'index'
          AND tbl_name = 'identity_cluster_run'
        """
    ).fetchall()
    run_index_sql = "\n".join(str(row["sql"] or "") for row in run_index_rows)
    assert "is_review_target = 1" in run_index_sql
    assert "is_materialization_owner = 1" in run_index_sql

    snapshot_columns = _table_columns(conn, "identity_observation_snapshot")
    assert {
        "observation_profile_id",
        "dataset_hash",
        "candidate_policy_hash",
        "max_knn_supported",
        "algorithm_version",
    }.issubset(snapshot_columns)

    run_columns = _table_columns(conn, "identity_cluster_run")
    assert {
        "observation_snapshot_id",
        "cluster_profile_id",
        "algorithm_version",
        "run_status",
        "is_review_target",
        "review_selected_at",
        "is_materialization_owner",
        "supersedes_run_id",
        "started_at",
        "finished_at",
        "activated_at",
        "prepared_artifact_root",
        "prepared_ann_manifest_json",
        "summary_json",
        "failure_json",
    }.issubset(run_columns)

    resolution_columns = _table_columns(conn, "identity_cluster_resolution")
    assert {
        "cluster_id",
        "resolution_state",
        "resolution_reason",
        "publish_state",
        "publish_failure_reason",
        "person_id",
        "source_run_id",
        "trusted_seed_count",
        "trusted_seed_candidate_count",
        "trusted_seed_reject_distribution_json",
        "prepared_bundle_manifest_json",
        "prototype_status",
        "ann_status",
    }.issubset(resolution_columns)

    member_columns = _table_columns(conn, "identity_cluster_member")
    assert {
        "cluster_id",
        "observation_id",
        "source_pool_kind",
        "quality_score_snapshot",
        "member_role",
        "decision_status",
        "distance_to_medoid",
        "density_radius",
        "support_ratio",
        "attachment_support_ratio",
        "nearest_competing_cluster_distance",
        "separation_gap",
        "decision_reason_code",
        "is_trusted_seed_candidate",
        "is_selected_trusted_seed",
        "seed_rank",
        "is_representative",
    }.issubset(member_columns)

    resolution_sql = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'identity_cluster_resolution'
        """
    ).fetchone()
    assert resolution_sql is not None
    resolution_sql_text = str(resolution_sql["sql"])
    assert "materialized" in resolution_sql_text
    assert "review_pending" in resolution_sql_text
    assert "discarded" in resolution_sql_text
    assert "unresolved" in resolution_sql_text
    assert "publish_failed" in resolution_sql_text
    assert "not_applicable" in resolution_sql_text

    cluster_sql = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'identity_cluster'
        """
    ).fetchone()
    assert cluster_sql is not None
    cluster_sql_text = str(cluster_sql["sql"])
    assert "raw" in cluster_sql_text and "cleaned" in cluster_sql_text and "final" in cluster_sql_text

    cluster_columns = _table_columns(conn, "identity_cluster")
    assert {
        "run_id",
        "cluster_stage",
        "cluster_state",
        "member_count",
        "retained_member_count",
        "anchor_core_count",
        "core_count",
        "boundary_count",
        "attachment_count",
        "excluded_count",
        "distinct_photo_count",
        "compactness_p50",
        "compactness_p90",
        "support_ratio_p10",
        "support_ratio_p50",
        "intra_photo_conflict_ratio",
        "nearest_cluster_distance",
        "separation_gap",
        "boundary_ratio",
        "discard_reason_code",
        "representative_observation_id",
        "summary_json",
    }.issubset(cluster_columns)

    legacy_run = conn.execute(
        """
        SELECT id, summary_json
        FROM identity_cluster_run
        WHERE algorithm_version = 'identity.cluster.legacy_migration.v3_to_v3_1'
          AND run_status = 'succeeded'
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert legacy_run is not None
    legacy_run_id = int(legacy_run["id"])
    legacy_summary = json.loads(str(legacy_run["summary_json"]))
    assert bool(legacy_summary.get("legacy_migration")) is True
    unexpected_legacy_runs = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM identity_cluster_run
        WHERE algorithm_version = 'identity.cluster.legacy_migration.v3_to_v3_1'
          AND run_status <> 'succeeded'
        """
    ).fetchone()
    assert unexpected_legacy_runs is not None
    assert int(unexpected_legacy_runs["c"]) == 0

    legacy_clusters = conn.execute(
        """
        SELECT id
        FROM identity_cluster
        WHERE run_id = ?
        """,
        (legacy_run_id,),
    ).fetchall()
    assert len(legacy_clusters) > 0
    legacy_cluster_ids = {int(row["id"]) for row in legacy_clusters}

    legacy_cluster_members = conn.execute(
        """
        SELECT cluster_id, observation_id, member_role, decision_status
        FROM identity_cluster_member
        WHERE cluster_id IN (
            SELECT id
            FROM identity_cluster
            WHERE run_id = ?
        )
        ORDER BY cluster_id, observation_id
        """,
        (legacy_run_id,),
    ).fetchall()
    legacy_member_truth = conn.execute(
        """
        SELECT acm.cluster_id, acm.face_observation_id, acm.is_seed_candidate, ac.cluster_status
        FROM auto_cluster_member AS acm
        JOIN auto_cluster AS ac
          ON ac.id = acm.cluster_id
        ORDER BY acm.cluster_id, acm.face_observation_id
        """
    ).fetchall()
    assert len(legacy_cluster_members) == len(legacy_member_truth)
    assert {
        (int(row["cluster_id"]), int(row["observation_id"]))
        for row in legacy_cluster_members
    } == {
        (int(row["cluster_id"]), int(row["face_observation_id"]))
        for row in legacy_member_truth
    }
    expected_member_states = {
        (int(row["cluster_id"]), int(row["face_observation_id"])): (
            "anchor_core" if int(row["is_seed_candidate"]) == 1 else "core",
            "discarded" if str(row["cluster_status"]) == "discarded" else "retained",
        )
        for row in legacy_member_truth
    }
    actual_member_states = {
        (int(row["cluster_id"]), int(row["observation_id"])): (
            str(row["member_role"]),
            str(row["decision_status"]),
        )
        for row in legacy_cluster_members
    }
    assert actual_member_states == {
        key: (role, "rejected" if run_state == "discarded" else "retained")
        for key, (role, run_state) in expected_member_states.items()
    }

    resolution_rows = conn.execute(
        """
        SELECT c.id AS cluster_id, r.source_run_id, r.resolution_state, run_ref.run_status AS source_run_status
        FROM identity_cluster AS c
        LEFT JOIN identity_cluster_resolution AS r
          ON r.cluster_id = c.id
        LEFT JOIN identity_cluster_run AS run_ref
          ON run_ref.id = r.source_run_id
        WHERE c.run_id = ?
        ORDER BY c.id
        """,
        (legacy_run_id,),
    ).fetchall()
    assert len(resolution_rows) == len(legacy_cluster_ids)
    assert all(row["source_run_id"] is not None for row in resolution_rows)
    assert all(int(row["source_run_id"]) == legacy_run_id for row in resolution_rows)
    assert all(str(row["source_run_status"]) == "succeeded" for row in resolution_rows)

    expected_resolution_states = {
        int(row["id"]): (
            "materialized"
            if str(row["cluster_status"]) == "materialized"
            else "discarded"
            if str(row["cluster_status"]) == "discarded"
            else "unresolved"
        )
        for row in conn.execute("SELECT id, cluster_status FROM auto_cluster").fetchall()
    }
    assert {
        int(row["cluster_id"]): str(row["resolution_state"])
        for row in resolution_rows
    } == expected_resolution_states

    lineage_columns = _table_columns(conn, "identity_cluster_lineage")
    assert {
        "parent_cluster_id",
        "child_cluster_id",
        "relation_kind",
        "reason_code",
    }.issubset(lineage_columns)

    pool_entry_columns = _table_columns(conn, "identity_observation_pool_entry")
    assert {
        "snapshot_id",
        "observation_id",
        "pool_kind",
        "quality_score_snapshot",
        "dedup_group_key",
        "representative_observation_id",
        "excluded_reason",
        "diagnostic_json",
    }.issubset(pool_entry_columns)
