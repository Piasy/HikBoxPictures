from pathlib import Path

from .fixtures_identity_v3_1 import build_identity_phase1_workspace


def test_algorithm_respects_mutual_knn_density_and_anchor_quantile_on_known_topology(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "cluster-known-topology")
    try:
        ws.seed_known_topology_case()
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
        ws.assert_known_topology_contract(run_id=int(run["run_id"]))
    finally:
        ws.close()


def test_algorithm_respects_raw_edge_max_distance_when_building_raw_components(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "cluster-raw-edge-threshold")
    try:
        observation_ids = ws.seed_raw_edge_threshold_case()
        ws.conn.execute(
            """
            UPDATE identity_cluster_profile
            SET discovery_knn_k = 3,
                raw_edge_max_distance = 0.03,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (ws.cluster_profile_id,),
        )
        ws.conn.commit()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        run = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=False,
        )
        raw_members = [
            item for item in ws.list_cluster_members(run_id=int(run["run_id"])) if str(item["cluster_stage"]) == "raw"
        ]
        clusters_by_id: dict[int, set[int]] = {}
        for member in raw_members:
            clusters_by_id.setdefault(int(member["cluster_id"]), set()).add(int(member["observation_id"]))
        assert {frozenset(member_ids) for member_ids in clusters_by_id.values()} == {
            frozenset({int(observation_ids["raw-a1"]), int(observation_ids["raw-a2"])}),
            frozenset({int(observation_ids["raw-b1"]), int(observation_ids["raw-b2"])}),
        }
    finally:
        ws.close()


def test_algorithm_respects_split_gap_threshold_when_gap_not_sufficient(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "cluster-low-split-gap")
    try:
        ws.seed_known_topology_case()
        ws.conn.execute(
            """
            UPDATE identity_cluster_profile
            SET split_min_medoid_gap = 1.0
            WHERE id = ?
            """,
            (ws.cluster_profile_id,),
        )
        ws.conn.commit()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        run = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=False,
        )
        lineage = ws.list_cluster_lineage(run_id=int(run["run_id"]))
        assert not any(item["relation_kind"] == "split" for item in lineage)
    finally:
        ws.close()


def test_algorithm_allows_zero_retained_attachment_when_thresholds_exclude_all(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "cluster-attachment-reject-all")
    try:
        ws.seed_split_and_attachment_case()
        ws.conn.execute(
            """
            UPDATE identity_cluster_profile
            SET attachment_max_distance = 0.0001,
                attachment_min_support_ratio = 0.99
            WHERE id = ?
            """,
            (ws.cluster_profile_id,),
        )
        ws.conn.commit()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        run = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=False,
        )
        members = ws.list_cluster_members(run_id=int(run["run_id"]))
        retained_attachment = [
            item
            for item in members
            if item["decision_status"] == "retained" and item["member_role"] == "attachment"
        ]
        assert not retained_attachment
    finally:
        ws.close()
