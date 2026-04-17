from __future__ import annotations

import html
import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from hikbox_pictures.api.app import create_app

from .fixtures_identity_v3_1 import build_identity_phase1_workspace


def _extract_payload(html_text: str) -> dict[str, object]:
    match = re.search(
        r'<script id="identity-tuning-data" type="application/json">\s*(.*?)\s*</script>',
        html_text,
        re.DOTALL,
    )
    assert match is not None
    return json.loads(html.unescape(match.group(1)).strip())


def _create_cluster_profile_variant(
    ws,
    *,
    profile_name: str,
    discovery_knn_k: int,
    density_min_samples: int,
) -> int:
    row = ws.conn.execute(
        """
        INSERT INTO identity_cluster_profile(
            profile_name,
            profile_version,
            discovery_knn_k,
            density_min_samples,
            raw_cluster_min_size,
            raw_cluster_min_distinct_photo_count,
            intra_photo_conflict_policy_version,
            anchor_core_min_support_ratio,
            anchor_core_radius_quantile,
            core_min_support_ratio,
            boundary_min_support_ratio,
            boundary_radius_multiplier,
            split_min_component_size,
            split_min_medoid_gap,
            existence_min_retained_count,
            existence_min_anchor_core_count,
            existence_min_distinct_photo_count,
            existence_min_support_ratio_p50,
            existence_max_intra_photo_conflict_ratio,
            attachment_max_distance,
            attachment_candidate_knn_k,
            attachment_min_support_ratio,
            attachment_min_separation_gap,
            materialize_min_anchor_core_count,
            materialize_min_distinct_photo_count,
            materialize_max_compactness_p90,
            materialize_min_separation_gap,
            materialize_max_boundary_ratio,
            trusted_seed_min_quality,
            trusted_seed_min_count,
            trusted_seed_max_count,
            trusted_seed_allow_boundary,
            active,
            created_at,
            updated_at
        )
        SELECT
            ?,
            profile_version || '.phase1e2e',
            ?,
            ?,
            raw_cluster_min_size,
            raw_cluster_min_distinct_photo_count,
            intra_photo_conflict_policy_version,
            anchor_core_min_support_ratio,
            anchor_core_radius_quantile,
            core_min_support_ratio,
            boundary_min_support_ratio,
            boundary_radius_multiplier,
            split_min_component_size,
            split_min_medoid_gap,
            existence_min_retained_count,
            existence_min_anchor_core_count,
            existence_min_distinct_photo_count,
            existence_min_support_ratio_p50,
            existence_max_intra_photo_conflict_ratio,
            attachment_max_distance,
            attachment_candidate_knn_k,
            attachment_min_support_ratio,
            attachment_min_separation_gap,
            materialize_min_anchor_core_count,
            materialize_min_distinct_photo_count,
            materialize_max_compactness_p90,
            materialize_min_separation_gap,
            materialize_max_boundary_ratio,
            trusted_seed_min_quality,
            trusted_seed_min_count,
            trusted_seed_max_count,
            trusted_seed_allow_boundary,
            0,
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        FROM identity_cluster_profile
        WHERE id = ?
        """,
        (profile_name, int(discovery_knn_k), int(density_min_samples), int(ws.cluster_profile_id)),
    )
    ws.conn.commit()
    return int(row.lastrowid)


def test_phase1_e2e_same_snapshot_double_run_review_activate_alignment(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "phase1-e2e")
    try:
        ws.seed_split_and_attachment_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )

        run_a = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=True,
        )
        run_a_id = int(run_a["run_id"])

        cluster_profile_b = _create_cluster_profile_variant(
            ws,
            profile_name="phase1-e2e-alt",
            discovery_knn_k=12,
            density_min_samples=3,
        )
        run_b = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=int(cluster_profile_b),
            supersedes_run_id=run_a_id,
            select_as_review_target=False,
        )
        run_b_id = int(run_b["run_id"])

        ws.new_cluster_prepare_service().prepare_run(run_id=run_a_id)
        ws.new_run_activation_service().activate_run(run_id=run_a_id)

        client = TestClient(create_app(workspace=ws.root))
        response = client.get("/identity-tuning")
        payload = _extract_payload(response.text)

        assert response.status_code == 200
        review_run = payload["review_run"]
        assert isinstance(review_run, dict)
        assert int(review_run["id"]) == run_a_id
        assert bool(review_run["is_materialization_owner"]) is True

        run_a_row = ws.get_cluster_run(run_a_id)
        run_b_row = ws.get_cluster_run(run_b_id)
        assert int(run_a_row["observation_snapshot_id"]) == int(run_b_row["observation_snapshot_id"])
        assert int(run_a_row["cluster_profile_id"]) != int(run_b_row["cluster_profile_id"])
        assert bool(run_a_row["is_review_target"]) is True
        assert bool(run_b_row["is_review_target"]) is False
        assert bool(run_a_row["is_materialization_owner"]) is True
        assert bool(run_b_row["is_materialization_owner"]) is False

        clusters = payload["clusters"]
        assert isinstance(clusters, list)
        assert len(clusters) > 0
    finally:
        ws.close()
