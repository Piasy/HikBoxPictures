from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_FIXTURE_SPEC = spec_from_file_location("people_gallery_fixtures_workspace_task5_real", _FIXTURE_PATH)
if _FIXTURE_SPEC is None or _FIXTURE_SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_FIXTURE_MODULE = module_from_spec(_FIXTURE_SPEC)
sys.modules[_FIXTURE_SPEC.name] = _FIXTURE_MODULE
_FIXTURE_SPEC.loader.exec_module(_FIXTURE_MODULE)
build_identity_real_workspace = _FIXTURE_MODULE.build_identity_real_workspace

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "rebuild_identities_v3.py"
_SCRIPT_SPEC = spec_from_file_location("task5_rebuild_identities_v3_script_real", _SCRIPT_PATH)
if _SCRIPT_SPEC is None or _SCRIPT_SPEC.loader is None:
    raise RuntimeError(f"无法加载重建脚本: {_SCRIPT_PATH}")
_SCRIPT_MODULE = module_from_spec(_SCRIPT_SPEC)
sys.modules[_SCRIPT_SPEC.name] = _SCRIPT_MODULE
_SCRIPT_SPEC.loader.exec_module(_SCRIPT_MODULE)
rebuild_main = _SCRIPT_MODULE.main


def test_rebuild_v3_real_pipeline_outputs_person_trusted_sample_and_prototype_with_score_spread(tmp_path: Path) -> None:
    ws = build_identity_real_workspace(tmp_path / "identity-real-task5")
    try:
        candidate = ws.build_profile_candidate()
        candidate.update(
            {
                "profile_name": "real-pipeline-task5",
                "bootstrap_min_cluster_size": 2,
                "bootstrap_min_distinct_photo_count": 2,
                "bootstrap_min_high_quality_count": 2,
                "bootstrap_seed_min_count": 2,
                "bootstrap_seed_max_count": 4,
                "high_quality_threshold": 0.01,
                "trusted_seed_quality_threshold": 0.01,
                "bootstrap_edge_accept_threshold": 1.0,
                "bootstrap_edge_candidate_threshold": 1.2,
                "bootstrap_margin_threshold": 0.0,
            }
        )
        profile_path = ws.root / ".tmp" / "task5" / "real-candidate.json"
        ws.write_json(profile_path, candidate)

        rc = rebuild_main(["--workspace", str(ws.root), "--threshold-profile", str(profile_path)])
        assert rc == 0

        summary = ws.load_last_rebuild_summary()
        assert summary is not None
        assert summary["dry_run"] is False
        assert summary["profile"]["profile_mode"] == "imported"
        assert summary["active_threshold_profile_id"] == summary["threshold_profile_id"]
        assert summary["post_rebuild"]["active_threshold_profile"]["id"] == summary["threshold_profile_id"]
        assert summary["executed_phase1_order"] == [
            "profile_resolve",
            "clear_identity_export_layers",
            "quality_backfill",
            "bootstrap_materialize",
            "prototype_ann_rebuild_optional",
            "summary",
        ]

        assert ws.count_person_rows() >= 1
        assert ws.count_table("person_trusted_sample") >= 2
        assert ws.count_table("person_prototype") >= 1
        assert summary["post_rebuild"]["person_count"] == ws.count_table("person")
        assert summary["post_rebuild"]["trusted_sample_count"] == ws.count_table("person_trusted_sample")
        assert summary["post_rebuild"]["prototype_count"] == ws.count_table("person_prototype")

        scores = ws.list_observation_scores()
        assert len(scores) >= 2
        spread = max(scores) - min(scores)
        assert spread >= 0.05

        sharpness_scores = ws.list_observation_sharpness_scores()
        assert len(sharpness_scores) >= 2
        sharpness_spread = max(sharpness_scores) - min(sharpness_scores)
        assert sharpness_spread >= 1.0

        assert ws.any_cluster_diagnostic("decision_kind", "materialized")
        diagnostic = ws.find_cluster_diagnostic_by_status("materialized")
        assert diagnostic is not None
        assert "cluster_size" in diagnostic
        assert "distinct_photo_count" in diagnostic
        assert int(diagnostic["cluster_size"]) >= 2
        assert int(diagnostic["distinct_photo_count"]) >= 2

        latest_batch = ws.latest_auto_cluster_batch()
        assert latest_batch is not None
        assert latest_batch["batch_type"] == "bootstrap"
        assert int(latest_batch["threshold_profile_id"]) == int(summary["threshold_profile_id"])
        assert summary["materialized_cluster_count"] >= 1
    finally:
        ws.close()
