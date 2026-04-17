from __future__ import annotations

import json
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from hikbox_pictures.services.observation_neighbor_export_service import ObservationNeighborExportService

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_FIXTURE_SPEC = spec_from_file_location("people_gallery_fixtures_workspace_cluster_neighbor_export", _FIXTURE_PATH)
if _FIXTURE_SPEC is None or _FIXTURE_SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_FIXTURE_MODULE = module_from_spec(_FIXTURE_SPEC)
sys.modules[_FIXTURE_SPEC.name] = _FIXTURE_MODULE
_FIXTURE_SPEC.loader.exec_module(_FIXTURE_MODULE)
build_identity_seed_workspace = _FIXTURE_MODULE.build_identity_seed_workspace


def _seed_run_context(
    ws: object,
    *,
    run_status: str,
    is_review_target: bool,
) -> dict[str, int]:
    conn = ws.conn  # type: ignore[attr-defined]
    observation_profile_row = conn.execute(
        """
        SELECT id
        FROM identity_observation_profile
        WHERE active = 1
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    cluster_profile_row = conn.execute(
        """
        SELECT id
        FROM identity_cluster_profile
        WHERE active = 1
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if observation_profile_row is None or cluster_profile_row is None:
        raise AssertionError("测试夹具缺少 active profile")

    snapshot_id = conn.execute(
        """
        INSERT INTO identity_observation_snapshot(
            observation_profile_id,
            dataset_hash,
            candidate_policy_hash,
            max_knn_supported,
            algorithm_version,
            summary_json,
            status
        )
        VALUES (?, 'ds-hash', 'policy-hash', 32, 'identity.snapshot.test', '{}', 'succeeded')
        """,
        (int(observation_profile_row["id"]),),
    ).lastrowid
    assert snapshot_id is not None

    run_id = conn.execute(
        """
        INSERT INTO identity_cluster_run(
            observation_snapshot_id,
            cluster_profile_id,
            algorithm_version,
            run_status,
            summary_json,
            failure_json,
            is_review_target
        )
        VALUES (?, ?, 'identity.cluster.test', ?, '{}', '{}', ?)
        """,
        (
            int(snapshot_id),
            int(cluster_profile_row["id"]),
            str(run_status),
            1 if is_review_target else 0,
        ),
    ).lastrowid
    assert run_id is not None

    conn.commit()
    return {
        "run_id": int(run_id),
        "observation_profile_id": int(observation_profile_row["id"]),
        "cluster_profile_id": int(cluster_profile_row["id"]),
        "snapshot_id": int(snapshot_id),
    }


def test_export_run_cluster_manifest_contains_context_fields_and_member_roles(tmp_path: Path) -> None:
    ws = build_identity_seed_workspace(tmp_path / "task8-cluster-neighbor-export")
    try:
        target = ws.insert_observation_with_embedding(
            vector=[0.00, 0.00, 0.00, 0.00],
            quality_score=0.98,
            photo_label="run-target",
        )
        retained = ws.insert_observation_with_embedding(
            vector=[0.01, 0.00, 0.00, 0.00],
            quality_score=0.96,
            photo_label="run-retained",
        )
        excluded = ws.insert_observation_with_embedding(
            vector=[0.03, 0.00, 0.00, 0.00],
            quality_score=0.95,
            photo_label="run-excluded",
        )
        competitor = ws.insert_observation_with_embedding(
            vector=[0.02, 0.00, 0.00, 0.00],
            quality_score=0.97,
            photo_label="run-competitor",
        )

        run = _seed_run_context(ws, run_status="succeeded", is_review_target=True)

        cluster_a = ws.conn.execute(
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
            VALUES (?, 'final', 'active', 3, 2, 1, 1, 0, 0, 1, 3, ?, '{}')
            """,
            (int(run["run_id"]), int(target["observation_id"])),
        ).lastrowid
        assert cluster_a is not None
        cluster_b = ws.conn.execute(
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
            VALUES (?, 'final', 'active', 1, 1, 1, 0, 0, 0, 0, 1, ?, '{}')
            """,
            (int(run["run_id"]), int(competitor["observation_id"])),
        ).lastrowid
        assert cluster_b is not None

        ws.conn.execute(
            """
            INSERT INTO identity_cluster_resolution(
                cluster_id,
                resolution_state,
                publish_state,
                source_run_id
            )
            VALUES (?, 'materialized', 'prepared', ?)
            """,
            (int(cluster_a), int(run["run_id"])),
        )
        ws.conn.execute(
            """
            INSERT INTO identity_cluster_resolution(
                cluster_id,
                resolution_state,
                publish_state,
                source_run_id
            )
            VALUES (?, 'review_pending', 'not_applicable', ?)
            """,
            (int(cluster_b), int(run["run_id"])),
        )

        ws.conn.execute(
            """
            INSERT INTO identity_cluster_member(
                cluster_id,
                observation_id,
                source_pool_kind,
                quality_score_snapshot,
                member_role,
                decision_status,
                nearest_competing_cluster_distance,
                separation_gap,
                is_selected_trusted_seed,
                seed_rank,
                is_representative,
                diagnostic_json
            )
            VALUES (?, ?, 'core_discovery', 0.98, 'anchor_core', 'retained', 0.42, 0.07, 1, 1, 1, '{}')
            """,
            (int(cluster_a), int(target["observation_id"])),
        )
        ws.conn.execute(
            """
            INSERT INTO identity_cluster_member(
                cluster_id,
                observation_id,
                source_pool_kind,
                quality_score_snapshot,
                member_role,
                decision_status,
                nearest_competing_cluster_distance,
                separation_gap,
                is_selected_trusted_seed,
                seed_rank,
                is_representative,
                diagnostic_json
            )
            VALUES (?, ?, 'attachment', 0.96, 'core', 'retained', 0.36, 0.05, 1, 2, 0, '{}')
            """,
            (int(cluster_a), int(retained["observation_id"])),
        )
        ws.conn.execute(
            """
            INSERT INTO identity_cluster_member(
                cluster_id,
                observation_id,
                source_pool_kind,
                quality_score_snapshot,
                member_role,
                decision_status,
                decision_reason_code,
                nearest_competing_cluster_distance,
                separation_gap,
                is_selected_trusted_seed,
                seed_rank,
                is_representative,
                diagnostic_json
            )
            VALUES (?, ?, 'excluded', 0.95, 'excluded', 'rejected', 'low_separation_gap', 0.31, 0.01, 0, NULL, 0, '{}')
            """,
            (int(cluster_a), int(excluded["observation_id"])),
        )
        ws.conn.execute(
            """
            INSERT INTO identity_cluster_member(
                cluster_id,
                observation_id,
                source_pool_kind,
                quality_score_snapshot,
                member_role,
                decision_status,
                nearest_competing_cluster_distance,
                separation_gap,
                is_selected_trusted_seed,
                seed_rank,
                is_representative,
                diagnostic_json
            )
            VALUES (?, ?, 'core_discovery', 0.97, 'anchor_core', 'retained', 0.00, 0.00, 1, 1, 1, '{}')
            """,
            (int(cluster_b), int(competitor["observation_id"])),
        )
        ws.conn.commit()

        result = ObservationNeighborExportService(ws.root).export(
            run_id=int(run["run_id"]),
            cluster_id=int(cluster_a),
            observation_ids=None,
            output_root=tmp_path / "export-by-run",
            neighbor_count=2,
        )

        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        assert int(manifest["run_id"]) == int(run["run_id"])
        assert int(manifest["observation_profile_id"]) == int(run["observation_profile_id"])
        assert int(manifest["cluster_profile_id"]) == int(run["cluster_profile_id"])
        assert int(manifest["observation_snapshot_id"]) == int(run["snapshot_id"])

        targets = manifest["targets"]
        assert len(targets) == 3

        member_roles = {str(item["target"]["member_role"]) for item in targets}
        assert {"anchor_core", "core", "excluded"}.issubset(member_roles)
        decision_statuses = {str(item["target"]["decision_status"]) for item in targets}
        assert {"retained", "rejected"}.issubset(decision_statuses)

        representative = [item["target"] for item in targets if int(item["target"]["is_representative"]) == 1]
        assert representative
        assert all(item["cluster_stage"] == "final" for item in representative)
        assert all(item["publish_state"] == "prepared" for item in representative)

        first_target = targets[0]["target"]
        assert "nearest_competing_cluster_distance" in first_target
        assert "separation_gap" in first_target
        assert "is_selected_trusted_seed" in first_target
        assert "seed_rank" in first_target
        assert "observation_snapshot_id" in first_target
        assert "exclusion_reason" in first_target
        excluded_target = next(
            item["target"]
            for item in targets
            if int(item["target"]["observation_id"]) == int(excluded["observation_id"])
        )
        assert excluded_target["exclusion_reason"] == "low_separation_gap"
    finally:
        ws.close()


def test_export_without_run_id_defaults_to_review_target_not_latest_run(tmp_path: Path) -> None:
    ws = build_identity_seed_workspace(tmp_path / "task8-cluster-neighbor-review-target")
    try:
        review_obs = ws.insert_observation_with_embedding(
            vector=[0.00, 0.00, 0.00, 0.00],
            quality_score=0.96,
            photo_label="review-target-member",
        )
        latest_obs = ws.insert_observation_with_embedding(
            vector=[1.00, 0.00, 0.00, 0.00],
            quality_score=0.96,
            photo_label="latest-run-member",
        )

        review_run = _seed_run_context(ws, run_status="succeeded", is_review_target=True)
        latest_run = _seed_run_context(ws, run_status="succeeded", is_review_target=False)

        review_cluster_id = ws.conn.execute(
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
            VALUES (?, 'final', 'active', 1, 1, 1, 0, 0, 0, 0, 1, ?, '{}')
            """,
            (int(review_run["run_id"]), int(review_obs["observation_id"])),
        ).lastrowid
        assert review_cluster_id is not None
        latest_cluster_id = ws.conn.execute(
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
            VALUES (?, 'final', 'active', 1, 1, 1, 0, 0, 0, 0, 1, ?, '{}')
            """,
            (int(latest_run["run_id"]), int(latest_obs["observation_id"])),
        ).lastrowid
        assert latest_cluster_id is not None

        ws.conn.executemany(
            """
            INSERT INTO identity_cluster_resolution(
                cluster_id,
                resolution_state,
                publish_state,
                source_run_id
            )
            VALUES (?, 'review_pending', 'not_applicable', ?)
            """,
            [
                (int(review_cluster_id), int(review_run["run_id"])),
                (int(latest_cluster_id), int(latest_run["run_id"])),
            ],
        )
        ws.conn.executemany(
            """
            INSERT INTO identity_cluster_member(
                cluster_id,
                observation_id,
                source_pool_kind,
                quality_score_snapshot,
                member_role,
                decision_status,
                nearest_competing_cluster_distance,
                separation_gap,
                is_selected_trusted_seed,
                seed_rank,
                is_representative,
                diagnostic_json
            )
            VALUES (?, ?, 'core_discovery', 0.96, 'anchor_core', 'retained', 0.20, 0.04, 1, 1, 1, '{}')
            """,
            [
                (int(review_cluster_id), int(review_obs["observation_id"])),
                (int(latest_cluster_id), int(latest_obs["observation_id"])),
            ],
        )
        ws.conn.commit()

        result = ObservationNeighborExportService(ws.root).export(
            run_id=None,
            cluster_id=int(review_cluster_id),
            observation_ids=None,
            output_root=tmp_path / "export-default-review-target",
            neighbor_count=1,
        )

        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        assert int(manifest["run_id"]) == int(review_run["run_id"])
        assert int(manifest["run_id"]) != int(latest_run["run_id"])
        assert len(manifest["targets"]) == 1
        assert int(manifest["targets"][0]["target"]["observation_id"]) == int(review_obs["observation_id"])
    finally:
        ws.close()


def test_export_observation_ids_without_run_id_defaults_to_review_target(tmp_path: Path) -> None:
    ws = build_identity_seed_workspace(tmp_path / "task8-observation-ids-review-target")
    try:
        review_obs = ws.insert_observation_with_embedding(
            vector=[0.00, 0.00, 0.00, 0.00],
            quality_score=0.96,
            photo_label="review-target-member-observation-ids",
        )
        latest_obs = ws.insert_observation_with_embedding(
            vector=[1.00, 0.00, 0.00, 0.00],
            quality_score=0.96,
            photo_label="latest-run-member-observation-ids",
        )

        review_run = _seed_run_context(ws, run_status="succeeded", is_review_target=True)
        latest_run = _seed_run_context(ws, run_status="succeeded", is_review_target=False)

        review_cluster_id = ws.conn.execute(
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
            VALUES (?, 'final', 'active', 1, 1, 1, 0, 0, 0, 0, 1, ?, '{}')
            """,
            (int(review_run["run_id"]), int(review_obs["observation_id"])),
        ).lastrowid
        assert review_cluster_id is not None
        latest_cluster_id = ws.conn.execute(
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
            VALUES (?, 'final', 'active', 1, 1, 1, 0, 0, 0, 0, 1, ?, '{}')
            """,
            (int(latest_run["run_id"]), int(latest_obs["observation_id"])),
        ).lastrowid
        assert latest_cluster_id is not None

        ws.conn.executemany(
            """
            INSERT INTO identity_cluster_member(
                cluster_id,
                observation_id,
                source_pool_kind,
                quality_score_snapshot,
                member_role,
                decision_status,
                nearest_competing_cluster_distance,
                separation_gap,
                is_selected_trusted_seed,
                seed_rank,
                is_representative,
                diagnostic_json
            )
            VALUES (?, ?, 'core_discovery', 0.96, 'anchor_core', 'retained', 0.20, 0.04, 1, 1, 1, '{}')
            """,
            [
                (int(review_cluster_id), int(review_obs["observation_id"])),
                (int(latest_cluster_id), int(latest_obs["observation_id"])),
            ],
        )
        ws.conn.commit()

        result = ObservationNeighborExportService(ws.root).export(
            run_id=None,
            cluster_id=None,
            observation_ids=[int(review_obs["observation_id"])],
            output_root=tmp_path / "export-observation-ids-default-review-target",
            neighbor_count=1,
        )

        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        assert int(manifest["run_id"]) == int(review_run["run_id"])
        assert int(manifest["run_id"]) != int(latest_run["run_id"])
        assert int(manifest["observation_snapshot_id"]) == int(review_run["snapshot_id"])
        assert len(manifest["targets"]) == 1
        target = manifest["targets"][0]["target"]
        assert int(target["observation_id"]) == int(review_obs["observation_id"])
        assert int(target["run_id"]) == int(review_run["run_id"])
        assert int(target["observation_snapshot_id"]) == int(review_run["snapshot_id"])
    finally:
        ws.close()
