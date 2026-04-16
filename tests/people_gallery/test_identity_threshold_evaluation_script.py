from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_FIXTURE_SPEC = spec_from_file_location("people_gallery_fixtures_workspace_task6_script", _FIXTURE_PATH)
if _FIXTURE_SPEC is None or _FIXTURE_SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_FIXTURE_MODULE = module_from_spec(_FIXTURE_SPEC)
sys.modules[_FIXTURE_SPEC.name] = _FIXTURE_MODULE
_FIXTURE_SPEC.loader.exec_module(_FIXTURE_MODULE)
build_identity_seed_workspace = _FIXTURE_MODULE.build_identity_seed_workspace
read_workspace_rebuild_summary = _FIXTURE_MODULE.read_workspace_rebuild_summary

_EVAL_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "evaluate_identity_thresholds.py"
_EVAL_SCRIPT_SPEC = spec_from_file_location("task6_evaluate_identity_thresholds_script", _EVAL_SCRIPT_PATH)
if _EVAL_SCRIPT_SPEC is None or _EVAL_SCRIPT_SPEC.loader is None:
    raise RuntimeError(f"无法加载评估脚本: {_EVAL_SCRIPT_PATH}")
_EVAL_SCRIPT_MODULE = module_from_spec(_EVAL_SCRIPT_SPEC)
sys.modules[_EVAL_SCRIPT_SPEC.name] = _EVAL_SCRIPT_MODULE
_EVAL_SCRIPT_SPEC.loader.exec_module(_EVAL_SCRIPT_MODULE)
evaluate_main = _EVAL_SCRIPT_MODULE.main

_REBUILD_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "rebuild_identities_v3.py"
_REBUILD_SCRIPT_SPEC = spec_from_file_location("task6_rebuild_identities_v3_script", _REBUILD_SCRIPT_PATH)
if _REBUILD_SCRIPT_SPEC is None or _REBUILD_SCRIPT_SPEC.loader is None:
    raise RuntimeError(f"无法加载重建脚本: {_REBUILD_SCRIPT_PATH}")
_REBUILD_SCRIPT_MODULE = module_from_spec(_REBUILD_SCRIPT_SPEC)
sys.modules[_REBUILD_SCRIPT_SPEC.name] = _REBUILD_SCRIPT_MODULE
_REBUILD_SCRIPT_SPEC.loader.exec_module(_REBUILD_SCRIPT_MODULE)
rebuild_main = _REBUILD_SCRIPT_MODULE.main


def test_evaluate_reuses_bootstrap_plan_algorithm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = build_identity_seed_workspace(tmp_path / "task6-plan-algo")
    output_dir = tmp_path / "identity-threshold-tuning" / "run-plan"
    try:
        ws.insert_observation_with_embedding(
            vector=[0.21, 0.22, 0.23, 0.24],
            quality_score=0.97,
            photo_label="task6-plan-algo-seed-a",
        )
        called = {"value": False}

        from hikbox_pictures.services.identity_bootstrap_service import IdentityBootstrapService

        def _fake_plan_bootstrap(self: object, *, profile_id: int) -> dict[str, object]:
            called["value"] = True
            return {
                "materialized_cluster_count": 1,
                "review_pending_cluster_count": 2,
                "discarded_cluster_count": 3,
                "estimated_low_confidence_assignment_count": 9,
                "cluster_size_distribution": {"1": 2},
                "distinct_photo_distribution": {"1": 2},
                "quality_distribution": {"count": 2, "min": 0.7, "max": 0.9, "avg": 0.8},
                "trusted_reject_reason_distribution": {"seed_insufficient_after_dedup": 2},
                "edge_reject_counts": {"not_mutual": 1, "distance_recheck_failed": 2, "photo_conflict": 3},
                "algorithm_version": "identity.bootstrap.v1",
            }

        monkeypatch.setattr(IdentityBootstrapService, "plan_bootstrap", _fake_plan_bootstrap)

        rc = evaluate_main(["--workspace", str(ws.root), "--output-dir", str(output_dir)])
        assert rc == 0
        assert called["value"] is True

        summary = ws.read_json(output_dir / "summary.json")
        assert int(summary["bootstrap_estimated_person_count"]) == 1
        assert int(summary["estimated_new_person_review_count"]) == 2
        assert int(summary["estimated_low_confidence_assignment_count"]) == 9
    finally:
        ws.close()


def test_evaluate_outputs_full_reports_and_does_not_mutate_db(tmp_path: Path) -> None:
    ws = build_identity_seed_workspace(tmp_path / "task6-eval")
    output_dir = tmp_path / "identity-threshold-tuning" / "run-a"
    try:
        ws.insert_observation_with_embedding(
            vector=[0.01, 0.02, 0.03, 0.04],
            quality_score=0.95,
            photo_label="task6-eval-seed-a",
        )
        checksum_before = ws.db_checksum()

        rc = evaluate_main(["--workspace", str(ws.root), "--output-dir", str(output_dir)])
        assert rc == 0

        checksum_after = ws.db_checksum()
        assert checksum_after == checksum_before

        summary = ws.read_json(output_dir / "summary.json")
        required_summary_keys = {
            "bootstrap_estimated_person_count",
            "estimated_new_person_review_count",
            "estimated_low_confidence_assignment_count",
            "cluster_size_distribution",
            "distinct_photo_distribution",
            "quality_distribution",
            "trusted_reject_reason_distribution",
            "diff_vs_active_profile",
        }
        assert required_summary_keys.issubset(summary.keys())

        candidate = ws.read_json(output_dir / "candidate-thresholds.json")
        assert set(candidate.keys()) == set(ws.identity_profile_roundtrip_columns())
    finally:
        ws.close()


def test_candidate_profile_can_be_consumed_by_rebuild_script(tmp_path: Path) -> None:
    ws = build_identity_seed_workspace(tmp_path / "task6-roundtrip")
    output_dir = tmp_path / "identity-threshold-tuning" / "run-b"
    closed = False
    try:
        ws.insert_observation_with_embedding(
            vector=[0.11, 0.12, 0.13, 0.14],
            quality_score=0.96,
            photo_label="task6-roundtrip-seed-a",
        )
        rc_eval = evaluate_main(["--workspace", str(ws.root), "--output-dir", str(output_dir)])
        assert rc_eval == 0

        candidate_path = output_dir / "candidate-thresholds.json"
        workspace_copy = ws.copy_workspace(tmp_path / "task6-roundtrip-copy")
        ws.close()
        closed = True

        rc_rebuild = rebuild_main(
            ["--workspace", str(workspace_copy), "--backup-db", "--threshold-profile", str(candidate_path)]
        )
        assert rc_rebuild == 0
        summary = read_workspace_rebuild_summary(workspace_copy)
        assert summary is not None
        assert summary["imported_threshold_profile"] is True
    finally:
        if not closed:
            ws.close()
