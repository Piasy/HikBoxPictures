from __future__ import annotations

import html
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from hikbox_pictures.api.app import create_app
from tests.people_gallery.fixtures_identity_v3_1 import build_identity_phase1_workspace


def _extract_embedded_json(html_text: str) -> dict[str, object]:
    match = re.search(
        r'<script id="identity-tuning-data" type="application/json">\s*(.*?)\s*</script>',
        html_text,
        re.DOTALL,
    )
    assert match is not None, "页面缺少 identity-tuning-data JSON 脚本"
    payload = html.unescape(match.group(1)).strip()
    assert payload, "identity-tuning-data JSON 脚本为空"
    parsed = json.loads(payload)
    assert isinstance(parsed, dict)
    return parsed


def _execute_run(ws, *, select_as_review_target: bool) -> int:
    snapshot = ws.new_observation_snapshot_service().build_snapshot(
        observation_profile_id=ws.observation_profile_id,
        candidate_knn_limit=24,
    )
    run_payload = ws.new_cluster_run_service().execute_run(
        observation_snapshot_id=int(snapshot["snapshot_id"]),
        cluster_profile_id=ws.cluster_profile_id,
        supersedes_run_id=None,
        select_as_review_target=select_as_review_target,
    )
    return int(run_payload["run_id"])


def test_identity_tuning_page_defaults_to_review_target_and_supports_run_id(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "identity-tuning-run-selection")
    try:
        ws.seed_known_topology_case()
        review_target_run_id = _execute_run(ws, select_as_review_target=True)
        explicit_run_id = _execute_run(ws, select_as_review_target=False)

        client = TestClient(create_app(workspace=ws.root))
        default_response = client.get("/identity-tuning")
        explicit_response = client.get("/identity-tuning", params={"run_id": explicit_run_id})

        assert default_response.status_code == 200
        default_payload = _extract_embedded_json(default_response.text)
        review_run = default_payload["review_run"]
        assert isinstance(review_run, dict)
        assert int(review_run["id"]) == review_target_run_id

        assert explicit_response.status_code == 200
        explicit_payload = _extract_embedded_json(explicit_response.text)
        explicit_review_run = explicit_payload["review_run"]
        assert isinstance(explicit_review_run, dict)
        assert int(explicit_review_run["id"]) == explicit_run_id
    finally:
        ws.close()


def test_identity_tuning_page_payload_shape_and_cluster_db_reconcile(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "identity-tuning-payload")
    try:
        ws.seed_known_topology_case()
        run_id = _execute_run(ws, select_as_review_target=True)

        client = TestClient(create_app(workspace=ws.root))
        response = client.get("/identity-tuning")
        assert response.status_code == 200

        payload = _extract_embedded_json(response.text)
        for key in [
            "review_run",
            "observation_snapshot",
            "observation_profile",
            "cluster_profile",
            "run_summary",
            "clusters",
        ]:
            assert key in payload

        review_run = payload["review_run"]
        observation_snapshot = payload["observation_snapshot"]
        observation_profile = payload["observation_profile"]
        cluster_profile = payload["cluster_profile"]
        run_summary = payload["run_summary"]
        clusters = payload["clusters"]

        assert isinstance(review_run, dict)
        assert isinstance(observation_snapshot, dict)
        assert isinstance(observation_profile, dict)
        assert isinstance(cluster_profile, dict)
        assert isinstance(run_summary, dict)
        assert isinstance(clusters, list)
        assert clusters

        assert int(review_run["id"]) == run_id
        assert int(observation_snapshot["id"]) == int(review_run["observation_snapshot_id"])
        assert int(observation_profile["id"]) == int(observation_snapshot["observation_profile_id"])
        assert int(cluster_profile["id"]) == int(review_run["cluster_profile_id"])

        for key in [
            "observation_total",
            "pool_counts",
            "final_cluster_counts",
            "resolution_counts",
            "dedup_drop_distribution",
        ]:
            assert key in run_summary

        final_cluster_count = ws.conn.execute(
            "SELECT COUNT(*) AS c FROM identity_cluster WHERE run_id = ? AND cluster_stage = 'final'",
            (run_id,),
        ).fetchone()
        assert final_cluster_count is not None
        assert int(run_summary["cluster_count"]) == int(final_cluster_count["c"])
        assert len(clusters) == int(final_cluster_count["c"])

        first_cluster = clusters[0]
        assert isinstance(first_cluster, dict)
        for key in ["lineage", "metrics", "seed_audit", "resolution", "members"]:
            assert key in first_cluster

        cluster_id = int(first_cluster["cluster_id"])
        cluster_row = ws.conn.execute(
            "SELECT * FROM identity_cluster WHERE id = ? AND run_id = ?",
            (cluster_id, run_id),
        ).fetchone()
        assert cluster_row is not None

        metrics = first_cluster["metrics"]
        assert isinstance(metrics, dict)
        assert int(metrics["member_count"]) == int(cluster_row["member_count"])
        assert int(metrics["retained_member_count"]) == int(cluster_row["retained_member_count"])
        assert int(metrics["distinct_photo_count"]) == int(cluster_row["distinct_photo_count"])

        lineage = first_cluster["lineage"]
        assert isinstance(lineage, list)
        lineage_count = ws.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM identity_cluster_lineage
            WHERE parent_cluster_id = ? OR child_cluster_id = ?
            """,
            (cluster_id, cluster_id),
        ).fetchone()
        assert lineage_count is not None
        assert len(lineage) == int(lineage_count["c"])

        resolution = first_cluster["resolution"]
        seed_audit = first_cluster["seed_audit"]
        members = first_cluster["members"]
        assert isinstance(resolution, dict)
        assert isinstance(seed_audit, dict)
        assert isinstance(members, dict)
        assert set(members.keys()) == {
            "representative",
            "retained",
            "excluded",
            "excluded_reason_distribution",
        }

        resolution_row = ws.conn.execute(
            "SELECT * FROM identity_cluster_resolution WHERE cluster_id = ?",
            (cluster_id,),
        ).fetchone()
        assert resolution_row is not None
        assert str(resolution["resolution_state"]) == str(resolution_row["resolution_state"])
        assert resolution["resolution_reason"] == resolution_row["resolution_reason"]
        assert resolution["publish_state"] == resolution_row["publish_state"]
        assert int(seed_audit["trusted_seed_count"]) == int(resolution_row["trusted_seed_count"])
        assert int(seed_audit["trusted_seed_candidate_count"]) == int(resolution_row["trusted_seed_candidate_count"])
        assert seed_audit["trusted_seed_reject_distribution"] == json.loads(
            resolution_row["trusted_seed_reject_distribution_json"] or "{}"
        )

        member_rows = ws.conn.execute(
            "SELECT * FROM identity_cluster_member WHERE cluster_id = ? ORDER BY id ASC",
            (cluster_id,),
        ).fetchall()
        assert member_rows

        representative_members = members["representative"]
        retained_members = members["retained"]
        excluded_members = members["excluded"]
        excluded_reason_distribution = members["excluded_reason_distribution"]

        assert isinstance(representative_members, list)
        assert isinstance(retained_members, list)
        assert isinstance(excluded_members, list)
        assert isinstance(excluded_reason_distribution, dict)

        assert len(representative_members) == sum(1 for row in member_rows if int(row["is_representative"]) == 1)
        assert len(retained_members) == sum(1 for row in member_rows if str(row["decision_status"]) != "rejected")
        assert len(excluded_members) == sum(1 for row in member_rows if str(row["decision_status"]) == "rejected")

        payload_member_map: dict[int, dict[str, object]] = {}
        for item in [*representative_members, *retained_members, *excluded_members]:
            member_id = int(item["member_id"])
            existing = payload_member_map.get(member_id)
            if existing is not None:
                assert existing.get("seed_rank") == item.get("seed_rank")
                assert bool(existing.get("is_selected_trusted_seed")) == bool(item.get("is_selected_trusted_seed"))
                continue
            payload_member_map[member_id] = item

        db_seed_rank_map = {
            int(row["id"]): (int(row["seed_rank"]) if row["seed_rank"] is not None else None)
            for row in member_rows
        }
        payload_seed_rank_map = {
            member_id: (
                int(member["seed_rank"])
                if member.get("seed_rank") is not None
                else None
            )
            for member_id, member in payload_member_map.items()
        }
        assert payload_seed_rank_map == db_seed_rank_map

        for member in payload_member_map.values():
            if bool(member.get("is_selected_trusted_seed")):
                assert member.get("seed_rank") is not None
                assert int(member["seed_rank"]) >= 1

        expected_excluded_reason_distribution: dict[str, int] = {}
        for row in member_rows:
            if str(row["decision_status"]) != "rejected":
                continue
            reason = str(row["decision_reason_code"] or "unknown")
            expected_excluded_reason_distribution[reason] = expected_excluded_reason_distribution.get(reason, 0) + 1
        assert excluded_reason_distribution == expected_excluded_reason_distribution

        snapshot_id = int(review_run["observation_snapshot_id"])
        dedup_rows = ws.conn.execute(
            """
            SELECT excluded_reason, COUNT(*) AS c
            FROM identity_observation_pool_entry
            WHERE snapshot_id = ?
              AND pool_kind = 'excluded'
              AND dedup_group_key IS NOT NULL
              AND excluded_reason IS NOT NULL
            GROUP BY excluded_reason
            """,
            (snapshot_id,),
        ).fetchall()
        expected_dedup_drop_distribution = {str(row["excluded_reason"]): int(row["c"]) for row in dedup_rows}
        assert run_summary["dedup_drop_distribution"] == expected_dedup_drop_distribution
    finally:
        ws.close()


def test_identity_tuning_page_returns_409_when_no_review_target(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "identity-tuning-missing-review-target")
    try:
        ws.seed_known_topology_case()
        run_id = _execute_run(ws, select_as_review_target=True)
        ws.conn.execute(
            "UPDATE identity_cluster_run SET is_review_target = 0, review_selected_at = NULL WHERE id = ?",
            (run_id,),
        )
        ws.conn.commit()

        client = TestClient(create_app(workspace=ws.root))
        response = client.get("/identity-tuning")

        assert response.status_code == 409
        payload = response.json()
        assert "detail" in payload
        assert "完整性错误" in str(payload["detail"])
    finally:
        ws.close()


def test_identity_tuning_page_returns_404_when_run_id_not_found(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "identity-tuning-run-not-found")
    try:
        ws.seed_known_topology_case()
        _execute_run(ws, select_as_review_target=True)
        client = TestClient(create_app(workspace=ws.root))
        response = client.get("/identity-tuning", params={"run_id": 999999})
        assert response.status_code == 404
        payload = response.json()
        assert "detail" in payload
        assert "run 不存在" in str(payload["detail"])
    finally:
        ws.close()
