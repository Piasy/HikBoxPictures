from __future__ import annotations

import json
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_FIXTURE_SPEC = spec_from_file_location("people_gallery_fixtures_workspace_export_observation_neighbors", _FIXTURE_PATH)
if _FIXTURE_SPEC is None or _FIXTURE_SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_FIXTURE_MODULE = module_from_spec(_FIXTURE_SPEC)
sys.modules[_FIXTURE_SPEC.name] = _FIXTURE_MODULE
_FIXTURE_SPEC.loader.exec_module(_FIXTURE_MODULE)
build_identity_seed_workspace = _FIXTURE_MODULE.build_identity_seed_workspace

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "export_observation_neighbors.py"
_SCRIPT_SPEC = spec_from_file_location("task7_export_observation_neighbors_script", _SCRIPT_PATH)
if _SCRIPT_SPEC is None or _SCRIPT_SPEC.loader is None:
    raise RuntimeError(f"无法加载导出脚本: {_SCRIPT_PATH}")
_SCRIPT_MODULE = module_from_spec(_SCRIPT_SPEC)
sys.modules[_SCRIPT_SPEC.name] = _SCRIPT_MODULE
_SCRIPT_SPEC.loader.exec_module(_SCRIPT_MODULE)
export_main = _SCRIPT_MODULE.main


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
        "snapshot_id": int(snapshot_id),
    }


def test_export_observation_neighbors_script_accepts_run_and_cluster_arguments(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: dict[str, object] = {}

    class _StubService:
        def __init__(self, workspace: Path) -> None:
            calls["workspace"] = Path(workspace)

        def export(self, **kwargs: object) -> dict[str, Path]:
            calls["export_kwargs"] = dict(kwargs)
            output_dir = Path(tmp_path / "script-run-cluster" / "bundle")
            output_dir.mkdir(parents=True, exist_ok=True)
            return {
                "output_dir": output_dir,
                "index_path": output_dir / "index.html",
                "manifest_path": output_dir / "manifest.json",
            }

    monkeypatch.setattr(_SCRIPT_MODULE, "ObservationNeighborExportService", _StubService)

    rc = export_main(
        [
            "--workspace",
            str(tmp_path / "ws"),
            "--run-id",
            "101",
            "--cluster-id",
            "202",
            "--neighbor-count",
            "3",
            "--output-root",
            str(tmp_path / "script-run-cluster"),
        ]
    )

    assert rc == 0
    assert calls["workspace"] == Path(tmp_path / "ws")
    export_kwargs = calls["export_kwargs"]
    assert isinstance(export_kwargs, dict)
    assert int(export_kwargs["run_id"]) == 101
    assert int(export_kwargs["cluster_id"]) == 202
    assert export_kwargs["observation_ids"] is None


def test_export_observation_neighbors_script_exports_bundle_and_rounds_html_numbers(
    tmp_path: Path,
) -> None:
    ws = build_identity_seed_workspace(tmp_path / "task7-export-observation-neighbors")
    output_root = tmp_path / "script-output"
    try:
        target_a = ws.insert_observation_with_embedding(
            vector=[0.00, 0.00, 0.00, 0.00],
            quality_score=0.9567,
            photo_label="tool-a",
        )
        neighbor_a1 = ws.insert_observation_with_embedding(
            vector=[0.01, 0.00, 0.00, 0.00],
            quality_score=0.9345,
            photo_label="tool-b",
        )
        neighbor_a2 = ws.insert_observation_with_embedding(
            vector=[0.03, 0.00, 0.00, 0.00],
            quality_score=0.9123,
            photo_label="tool-c",
        )
        target_b = ws.insert_observation_with_embedding(
            vector=[1.00, 0.00, 0.00, 0.00],
            quality_score=0.9876,
            photo_label="tool-d",
        )
        neighbor_b1 = ws.insert_observation_with_embedding(
            vector=[1.01, 0.00, 0.00, 0.00],
            quality_score=0.9765,
            photo_label="tool-e",
        )
        neighbor_b2 = ws.insert_observation_with_embedding(
            vector=[1.03, 0.00, 0.00, 0.00],
            quality_score=0.9654,
            photo_label="tool-f",
        )

        rc = export_main(
            [
                "--workspace",
                str(ws.root),
                "--observation-ids",
                f"{target_a['observation_id']},{target_b['observation_id']}",
                "--neighbor-count",
                "2",
                "--output-root",
                str(output_root),
            ]
        )

        assert rc == 0
        output_dirs = [path for path in output_root.iterdir() if path.is_dir()]
        assert len(output_dirs) == 1
        output_dir = output_dirs[0]

        index_path = output_dir / "index.html"
        manifest_path = output_dir / "manifest.json"
        assert index_path.is_file()
        assert manifest_path.is_file()

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert int(manifest["neighbor_count_per_target"]) == 2
        targets = manifest["targets"]
        assert len(targets) == 2
        first_target = targets[0]["target"]
        assert "observation_snapshot_id" in first_target
        assert "exclusion_reason" in first_target

        html_text = index_path.read_text(encoding="utf-8")
        assert (
            f"observation {target_a['observation_id']} / photo {target_a['asset_id']} / quality 0.96"
            in html_text
        )
        assert (
            f"observation {target_b['observation_id']} / photo {target_b['asset_id']} / quality 0.99"
            in html_text
        )
        assert "distance 0.01" in html_text
        assert "distance 0.03" in html_text
        assert "distance 1.00" not in html_text
        assert "quality 0.9567" not in html_text

        for observation_id in (target_a["observation_id"], target_b["observation_id"]):
            observation_dir = output_dir / f"obs-{observation_id}"
            assert observation_dir.is_dir()

        expected_files = {
            output_dir / f"obs-{target_a['observation_id']}" / f"00-target_obs-{target_a['observation_id']}_photo-{target_a['asset_id']}__crop.jpg",
            output_dir / f"obs-{target_a['observation_id']}" / f"00-target_obs-{target_a['observation_id']}_photo-{target_a['asset_id']}__preview.jpg",
            output_dir / f"obs-{target_a['observation_id']}" / f"01-nn_obs-{neighbor_a1['observation_id']}_photo-{neighbor_a1['asset_id']}__crop.jpg",
            output_dir / f"obs-{target_a['observation_id']}" / f"02-nn_obs-{neighbor_a2['observation_id']}_photo-{neighbor_a2['asset_id']}__preview.jpg",
            output_dir / f"obs-{target_b['observation_id']}" / f"01-nn_obs-{neighbor_b1['observation_id']}_photo-{neighbor_b1['asset_id']}__crop.jpg",
            output_dir / f"obs-{target_b['observation_id']}" / f"02-nn_obs-{neighbor_b2['observation_id']}_photo-{neighbor_b2['asset_id']}__preview.jpg",
        }
        for expected_file in expected_files:
            assert expected_file.is_file()
    finally:
        ws.close()


def test_export_observation_neighbors_script_observation_ids_default_review_target_run(
    tmp_path: Path,
) -> None:
    ws = build_identity_seed_workspace(tmp_path / "task8-script-observation-ids-review-target")
    output_root = tmp_path / "script-review-target-output"
    try:
        review_obs = ws.insert_observation_with_embedding(
            vector=[0.00, 0.00, 0.00, 0.00],
            quality_score=0.96,
            photo_label="script-review-target-member",
        )
        latest_obs = ws.insert_observation_with_embedding(
            vector=[1.00, 0.00, 0.00, 0.00],
            quality_score=0.96,
            photo_label="script-latest-run-member",
        )
        helper_obs = ws.insert_observation_with_embedding(
            vector=[0.02, 0.00, 0.00, 0.00],
            quality_score=0.95,
            photo_label="script-helper-neighbor",
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
            VALUES (?, 'final', 'active', 2, 2, 1, 1, 0, 0, 0, 2, ?, '{}')
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
            VALUES (?, ?, 'core_discovery', 0.96, 'anchor_core', 'retained', 0.20, 0.04, 1, 1, 1, '{}')
            """,
            (int(review_cluster_id), int(review_obs["observation_id"])),
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
            VALUES (?, ?, 'attachment', 0.95, 'core', 'retained', 0.18, 0.03, 1, 2, 0, '{}')
            """,
            (int(review_cluster_id), int(helper_obs["observation_id"])),
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
            VALUES (?, ?, 'core_discovery', 0.96, 'anchor_core', 'retained', 0.20, 0.04, 1, 1, 1, '{}')
            """,
            (int(latest_cluster_id), int(latest_obs["observation_id"])),
        )
        ws.conn.commit()

        rc = export_main(
            [
                "--workspace",
                str(ws.root),
                "--observation-ids",
                str(review_obs["observation_id"]),
                "--neighbor-count",
                "1",
                "--output-root",
                str(output_root),
            ]
        )

        assert rc == 0
        output_dirs = [path for path in output_root.iterdir() if path.is_dir()]
        assert len(output_dirs) == 1
        manifest_path = output_dirs[0] / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert int(manifest["run_id"]) == int(review_run["run_id"])
        assert int(manifest["run_id"]) != int(latest_run["run_id"])
        assert int(manifest["observation_snapshot_id"]) == int(review_run["snapshot_id"])
        target = manifest["targets"][0]["target"]
        assert int(target["run_id"]) == int(review_run["run_id"])
        assert int(target["observation_snapshot_id"]) == int(review_run["snapshot_id"])
    finally:
        ws.close()
