from pathlib import Path

from .fixtures_identity_v3_1 import build_identity_phase1_workspace


def test_prepare_run_writes_verified_manifests_and_only_then_marks_materialized_prepared(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "prepare-run")
    try:
        ws.seed_materialize_candidate_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        run = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=True,
        )
        before_states = ws.list_cluster_resolutions(run_id=int(run["run_id"]))
        assert all(item["resolution_state"] != "materialized" for item in before_states)

        result = ws.new_cluster_prepare_service().prepare_run(run_id=int(run["run_id"]))

        active_people = ws.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM person
            WHERE status = 'active'
              AND ignored = 0
            """
        ).fetchone()
        prepared_clusters = ws.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM identity_cluster_resolution
            WHERE source_run_id = ?
              AND resolution_state = 'materialized'
              AND publish_state = 'prepared'
            """,
            (int(run["run_id"]),),
        ).fetchone()
        ann_manifest = ws.get_run_ann_manifest(int(run["run_id"]))
        assert int(result["prepared_cluster_count"]) >= 1
        assert int(active_people["c"]) == 0
        assert int(prepared_clusters["c"]) >= 1
        assert ann_manifest["artifact_checksum"]
        assert ann_manifest["artifact_path"]

        ranked = ws.conn.execute(
            """
            SELECT
                m.observation_id,
                m.member_role,
                m.seed_rank,
                m.support_ratio,
                m.distance_to_medoid,
                m.cluster_id,
                fo.quality_score
            FROM identity_cluster_member AS m
            JOIN identity_cluster AS c ON c.id = m.cluster_id
            JOIN face_observation AS fo ON fo.id = m.observation_id
            WHERE c.run_id = ?
              AND c.cluster_stage = 'final'
              AND m.is_selected_trusted_seed = 1
            ORDER BY m.cluster_id ASC, m.seed_rank ASC
            """,
            (int(run["run_id"]),),
        ).fetchall()
        assert ranked
        role_order = {"anchor_core": 0, "core": 1, "boundary": 2}
        by_cluster: dict[int, list] = {}
        for row in ranked:
            by_cluster.setdefault(int(row["cluster_id"]), []).append(row)
        for rows in by_cluster.values():
            expected = sorted(
                rows,
                key=lambda row: (
                    role_order.get(str(row["member_role"]), 9),
                    -float(row["quality_score"] or 0.0),
                    -float(row["support_ratio"] or 0.0),
                    float(row["distance_to_medoid"] or 0.0),
                    int(row["observation_id"]),
                ),
            )
            assert [int(row["observation_id"]) for row in rows] == [
                int(row["observation_id"]) for row in expected
            ]
    finally:
        ws.close()


def test_prepare_run_rolls_all_candidates_back_to_review_pending_when_run_ann_bundle_failed(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "prepare-run-ann-failed")
    try:
        ws.seed_materialize_candidate_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        run = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=True,
        )
        ws.stub_run_ann_prepare_failure(run_id=int(run["run_id"]))
        result = ws.new_cluster_prepare_service().prepare_run(run_id=int(run["run_id"]))
        states = ws.list_cluster_resolutions(run_id=int(run["run_id"]))
        assert int(result["prepared_cluster_count"]) == 0
        assert all(item["resolution_state"] in {"review_pending", "discarded"} for item in states)
        assert all(
            item["publish_state"] == "not_applicable" for item in states if item["resolution_state"] != "discarded"
        )
    finally:
        ws.close()


def test_prepare_run_does_not_materialize_cluster_below_gate_thresholds(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "prepare-run-gate-negative")
    try:
        ws.seed_materialize_gate_negative_case(
            scenario="anchor_core_below_materialize_min",
        )
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        run = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=True,
        )
        result = ws.new_cluster_prepare_service().prepare_run(run_id=int(run["run_id"]))

        assert int(result["prepared_cluster_count"]) >= 0
        rows = ws.conn.execute(
            """
            SELECT c.id, r.resolution_state, r.publish_state
            FROM identity_cluster AS c
            JOIN identity_cluster_resolution AS r ON r.cluster_id = c.id
            WHERE c.run_id = ?
              AND c.cluster_stage = 'final'
              AND c.cluster_state = 'active'
            """,
            (int(run["run_id"]),),
        ).fetchall()
        assert rows
        assert any(
            row["resolution_state"] == "review_pending" and row["publish_state"] == "not_applicable"
            for row in rows
        )
    finally:
        ws.close()
