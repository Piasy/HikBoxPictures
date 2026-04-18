from __future__ import annotations

from dataclasses import fields
from pathlib import Path
import sqlite3
import pytest

from hikbox_experiments.identity_v3_1.models import (
    AssignParameters,
    AssignmentEvaluation,
    AssignmentRecord,
    AssignmentSummary,
    BaseRunContext,
    ClusterMemberRecord,
    ClusterRecord,
    ObservationCandidateRecord,
    QueryContext,
    SeedBuildResult,
    SeedIdentityRecord,
    SnapshotContext,
    TopCandidateRecord,
)

from .fixtures_identity_v3_1_export import build_identity_v3_1_export_workspace


def test_build_identity_v3_1_export_workspace_seeds_expected_topology(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "identity-v3-1-export-fixture")
    try:
        assert ws.base_run_id > 0
        assert ws.latest_non_target_run_id > 0
        assert ws.failed_run_id > 0
        assert ws.snapshot_id > 0

        assert {
            "seed_primary",
            "seed_fallback",
            "seed_invalid",
            "pending_promotable",
            "other_run_materialized",
        }.issubset(set(ws.cluster_ids))

        assert {
            "attachment_auto",
            "pending_attachment_overlap",
            "embedding_probe",
            "attachment_missing_embedding",
            "attachment_dim_mismatch",
            "other_snapshot_attachment",
            "warmup_active",
        }.issubset(set(ws.observation_ids))

        assert ws.embedding_probe_expected_dim > 0
        assert ws.embedding_probe_expected_model_key
        assert ws.selected_snapshot_embedding_model_key == ws.embedding_probe_expected_model_key
        assert ws.latest_visible_profile_embedding_model_key
        assert ws.selected_snapshot_embedding_model_key != ws.latest_visible_profile_embedding_model_key

        assert ws.expected_cluster_ids_by_run_id[ws.base_run_id] == {
            ws.cluster_ids["seed_primary"],
            ws.cluster_ids["seed_fallback"],
            ws.cluster_ids["seed_invalid"],
            ws.cluster_ids["pending_promotable"],
        }
        assert ws.expected_cluster_ids_by_run_id[ws.latest_non_target_run_id]
        assert ws.expected_cluster_ids_by_run_id[ws.latest_non_target_run_id] != ws.expected_cluster_ids_by_run_id[
            ws.base_run_id
        ]
        assert (ws.base_run_id, "review_pending") in ws.expected_candidate_ids_by_run_id_and_source
        assert (ws.base_run_id, "attachment") in ws.expected_candidate_ids_by_run_id_and_source
        assert ws.expected_candidate_ids_by_run_id_and_source[
            (ws.latest_non_target_run_id, "all")
        ] != ws.expected_candidate_ids_by_run_id_and_source[(ws.base_run_id, "all")]
        assert ws.observation_ids["other_snapshot_attachment"] not in ws.expected_candidate_ids_by_run_id_and_source[
            (ws.base_run_id, "all")
        ]
        assert ws.observation_ids["warmup_active"] not in ws.expected_candidate_ids_by_run_id_and_source[
            (ws.base_run_id, "all")
        ]
        assert ws.photo_ids["attachment_same_photo_conflict"] == ws.photo_ids["seed_primary_a"]
        assert ws.photo_ids["attachment_auto"] != ws.photo_ids["seed_primary_a"]
    finally:
        ws.close()


def _load_run_binding(conn: sqlite3.Connection, *, run_id: int) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT
            r.id AS run_id,
            s.id AS snapshot_id,
            s.observation_profile_id AS profile_id,
            p.embedding_model_key AS embedding_model_key
        FROM identity_cluster_run AS r
        JOIN identity_observation_snapshot AS s ON s.id = r.observation_snapshot_id
        JOIN identity_observation_profile AS p ON p.id = s.observation_profile_id
        WHERE r.id = ?
        """,
        (int(run_id),),
    ).fetchone()
    if row is None:
        raise AssertionError(f"run 不存在: {int(run_id)}")
    return row


def test_build_identity_v3_1_export_workspace_binds_latest_profile_and_run_correctly(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "identity-v3-1-export-profile-binding")
    try:
        base_binding = _load_run_binding(ws.conn, run_id=ws.base_run_id)
        latest_binding = _load_run_binding(ws.conn, run_id=ws.latest_non_target_run_id)
        failed_binding = _load_run_binding(ws.conn, run_id=ws.failed_run_id)

        assert int(base_binding["snapshot_id"]) == ws.snapshot_id
        assert str(base_binding["embedding_model_key"]) == ws.selected_snapshot_embedding_model_key
        assert str(latest_binding["embedding_model_key"]) == ws.latest_visible_profile_embedding_model_key
        assert str(failed_binding["embedding_model_key"]) == ws.selected_snapshot_embedding_model_key
        assert int(latest_binding["profile_id"]) > int(base_binding["profile_id"])
        assert int(latest_binding["profile_id"]) > int(failed_binding["profile_id"])
    finally:
        ws.close()


def test_build_identity_v3_1_export_workspace_keeps_row_level_invariants(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "identity-v3-1-export-row-invariants")
    try:
        discarded_row = ws.conn.execute(
            """
            SELECT c.cluster_stage, c.cluster_state, c.retained_member_count, r.resolution_state
            FROM identity_cluster AS c
            JOIN identity_cluster_resolution AS r ON r.cluster_id = c.id
            WHERE c.id = ?
            """,
            (int(ws.cluster_ids["discarded_final"]),),
        ).fetchone()
        assert discarded_row is not None
        assert str(discarded_row["cluster_stage"]) == "final"
        assert str(discarded_row["cluster_state"]) == "discarded"
        assert int(discarded_row["retained_member_count"]) == 0
        assert str(discarded_row["resolution_state"]) == "discarded"

        pending_cluster_row = ws.conn.execute(
            """
            SELECT c.retained_member_count, r.resolution_state
            FROM identity_cluster AS c
            JOIN identity_cluster_resolution AS r ON r.cluster_id = c.id
            WHERE c.id = ?
            """,
            (int(ws.cluster_ids["pending_promotable"]),),
        ).fetchone()
        assert pending_cluster_row is not None
        assert int(pending_cluster_row["retained_member_count"]) == 2
        assert str(pending_cluster_row["resolution_state"]) == "review_pending"

        retained_member_count = int(
            ws.conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM identity_cluster_member
                WHERE cluster_id = ?
                  AND decision_status = 'retained'
                """,
                (int(ws.cluster_ids["pending_promotable"]),),
            ).fetchone()["c"]
        )
        assert retained_member_count == 2

        overlap_observation_id = int(ws.observation_ids["pending_attachment_overlap"])
        retained_overlap_count = int(
            ws.conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM identity_cluster_member
                WHERE cluster_id = ?
                  AND observation_id = ?
                  AND decision_status = 'retained'
                """,
                (int(ws.cluster_ids["pending_promotable"]), overlap_observation_id),
            ).fetchone()["c"]
        )
        assert retained_overlap_count == 1

        attachment_overlap_count = int(
            ws.conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM identity_observation_pool_entry
                WHERE snapshot_id = ?
                  AND observation_id = ?
                  AND pool_kind = 'attachment'
                """,
                (int(ws.snapshot_id), overlap_observation_id),
            ).fetchone()["c"]
        )
        assert attachment_overlap_count == 1
    finally:
        ws.close()


def test_build_identity_v3_1_export_workspace_embeding_probe_rows_are_expected(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "identity-v3-1-export-embedding-probe")
    try:
        rows = ws.conn.execute(
            """
            SELECT model_key, normalized
            FROM face_embedding
            WHERE face_observation_id = ?
              AND feature_type = 'face'
            ORDER BY id ASC
            """,
            (int(ws.observation_ids["embedding_probe"]),),
        ).fetchall()
        assert len(rows) == 3
        exists_selected_normalized = any(
            str(row["model_key"]) == ws.embedding_probe_expected_model_key and int(row["normalized"]) == 1 for row in rows
        )
        exists_wrong_model = any(str(row["model_key"]) == "wrong-model-key" for row in rows)
        exists_non_normalized = any(
            str(row["model_key"]) == ws.embedding_probe_expected_model_key and int(row["normalized"]) == 0 for row in rows
        )
        assert exists_selected_normalized
        assert exists_wrong_model
        assert exists_non_normalized
    finally:
        ws.close()


def test_identity_v3_1_models_contract_field_names_are_stable() -> None:
    assert tuple(item.name for item in fields(BaseRunContext)) == (
        "id",
        "run_status",
        "observation_snapshot_id",
        "cluster_profile_id",
        "is_review_target",
    )
    assert tuple(item.name for item in fields(SnapshotContext)) == (
        "id",
        "observation_profile_id",
        "embedding_model_key",
    )
    assert tuple(item.name for item in fields(ClusterMemberRecord)) == (
        "cluster_id",
        "observation_id",
        "photo_id",
        "source_pool_kind",
        "member_role",
        "decision_status",
        "is_selected_trusted_seed",
        "is_representative",
        "quality_score_snapshot",
        "primary_path",
        "embedding_vector",
        "embedding_dim",
    )
    assert tuple(item.name for item in fields(TopCandidateRecord)) == (
        "rank",
        "identity_id",
        "cluster_id",
        "distance",
    )
    assert tuple(item.name for item in fields(ClusterRecord)) == (
        "cluster_id",
        "cluster_stage",
        "cluster_state",
        "resolution_state",
        "representative_observation_id",
        "retained_member_count",
        "distinct_photo_count",
        "representative_count",
        "retained_count",
        "excluded_count",
        "members",
    )
    assert tuple(item.name for item in fields(ObservationCandidateRecord)) == (
        "observation_id",
        "photo_id",
        "source_kind",
        "source_cluster_id",
        "primary_path",
        "embedding_vector",
        "embedding_dim",
        "embedding_model_key",
    )
    assert tuple(item.name for item in fields(SeedIdentityRecord)) == (
        "identity_id",
        "source_cluster_id",
        "resolution_state",
        "seed_member_count",
        "fallback_used",
        "prototype_dimension",
        "representative_observation_id",
        "member_observation_ids",
        "valid",
        "error_code",
        "error_message",
        "prototype_vector",
    )
    assert tuple(item.name for item in fields(SeedBuildResult)) == (
        "valid_seeds_by_cluster",
        "invalid_seeds",
        "errors",
        "prototype_dimension",
    )
    assert tuple(item.name for item in fields(AssignmentRecord)) == (
        "observation_id",
        "photo_id",
        "source_kind",
        "source_cluster_id",
        "best_identity_id",
        "best_cluster_id",
        "best_distance",
        "second_best_distance",
        "distance_margin",
        "same_photo_conflict",
        "decision",
        "reason_code",
        "top_candidates",
        "assets",
        "missing_assets",
    )
    assert tuple(item.name for item in fields(AssignmentSummary)) == (
        "candidate_count",
        "auto_assign_count",
        "review_count",
        "reject_count",
        "same_photo_conflict_count",
        "missing_embedding_count",
        "dimension_mismatch_count",
    )
    assert tuple(item.name for item in fields(AssignmentEvaluation)) == (
        "assignments",
        "by_observation_id",
        "excluded_seed_observation_ids",
        "summary",
    )
    assert tuple(item.name for item in fields(QueryContext)) == (
        "base_run",
        "snapshot",
        "clusters",
        "clusters_by_id",
        "candidate_observations",
        "non_rejected_member_observation_ids_by_cluster",
        "source_candidate_observation_ids",
        "warnings",
    )


def test_assign_parameters_validate_covers_success_and_error_branches() -> None:
    validated = AssignParameters(
        base_run_id=7,
        assign_source="attachment",
        top_k=2,
        auto_max_distance=0.22,
        review_max_distance=0.35,
        min_margin=0.01,
    ).validate()
    assert isinstance(validated, AssignParameters)
    assert validated.base_run_id == 7

    with pytest.raises(ValueError, match="不支持的 assign_source: bogus"):
        AssignParameters(assign_source="bogus").validate()
    with pytest.raises(ValueError, match="top_k 必须大于 0"):
        AssignParameters(top_k=0).validate()
    with pytest.raises(ValueError, match="auto_max_distance 不能大于 review_max_distance"):
        AssignParameters(auto_max_distance=0.4, review_max_distance=0.35).validate()
    with pytest.raises(ValueError, match="min_margin 不能小于 0"):
        AssignParameters(min_margin=-0.01).validate()
