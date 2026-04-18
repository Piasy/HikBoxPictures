from pathlib import Path

import pytest

from .fixtures_identity_v3_1 import build_identity_phase1_workspace


def _rewrite_embedding_model_key(*, ws, model_key: str) -> None:  # type: ignore[no-untyped-def]
    ws.conn.execute(
        """
        UPDATE face_embedding
        SET model_key = ?
        WHERE feature_type = 'face'
        """,
        (str(model_key),),
    )
    ws.conn.execute(
        """
        UPDATE identity_threshold_profile
        SET embedding_model_key = ?
        WHERE active = 1
        """,
        (str(model_key),),
    )
    ws.conn.execute(
        """
        UPDATE identity_observation_profile
        SET embedding_model_key = ?
        WHERE id = ?
        """,
        (str(model_key), int(ws.observation_profile_id)),
    )
    ws.conn.commit()


def test_execute_run_persists_lineage_member_roles_and_resolution_states(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "cluster-execute")
    try:
        ws.seed_split_and_attachment_case()
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

        assert run["run_status"] == "succeeded"
        lineage = ws.list_cluster_lineage(run_id=int(run["run_id"]))
        assert any(item["relation_kind"] == "split" for item in lineage)

        final_clusters = ws.list_clusters(run_id=int(run["run_id"]), cluster_stage="final")
        assert final_clusters
        assert any(item["cluster_state"] == "discarded" for item in final_clusters)

        resolutions = ws.list_cluster_resolutions(run_id=int(run["run_id"]))
        assert resolutions
        assert all(item["resolution_state"] in {"unresolved", "review_pending", "discarded"} for item in resolutions)
        assert all(item["resolution_state"] != "materialized" for item in resolutions)
        assert all(item["publish_state"] == "not_applicable" for item in resolutions)
        assert all(item["prototype_status"] == "not_applicable" for item in resolutions)
        assert all(item["ann_status"] == "not_applicable" for item in resolutions)

        members = ws.list_cluster_members(run_id=int(run["run_id"]))
        roles = {item["member_role"] for item in members if item["decision_status"] == "retained"}
        assert {"anchor_core", "core", "boundary"}.issubset(roles)
        assert "attachment" in roles
        assert any(item["decision_reason_code"] == "split_into_other_child" for item in members)
        assert any(item["decision_reason_code"] == "outside_boundary_radius" for item in members)

        ws.assert_member_support_ratio_formula(run_id=int(run["run_id"]), sample_size=8)
        ws.assert_intra_photo_conflict_ratio_formula(run_id=int(run["run_id"]))
        ws.assert_existence_gate_reason_consistent(run_id=int(run["run_id"]))
    finally:
        ws.close()


def test_execute_run_persists_gate_metrics_and_discard_reason_alignment(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "cluster-metrics-audit")
    try:
        ws.seed_split_and_attachment_case()
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
        ws.assert_final_gate_metrics_frozen_before_attachment(run_id=int(run["run_id"]))
        ws.assert_cluster_discard_reason_equals_resolution_reason(run_id=int(run["run_id"]))
    finally:
        ws.close()


def test_execute_run_uses_snapshot_embedding_model_key_instead_of_hardcoded_insightface(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "cluster-non-insightface-model")
    try:
        ws.seed_split_and_attachment_case()
        _rewrite_embedding_model_key(ws=ws, model_key="MockArcFace@retinaface")
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

        assert run["run_status"] == "succeeded"
        assert ws.count_clusters(run_id=int(run["run_id"])) > 0
    finally:
        ws.close()


def test_execute_run_reports_progress_events_with_total_completed_percent(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "cluster-progress")
    try:
        ws.seed_split_and_attachment_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        events: list[dict[str, object]] = []

        run = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=False,
            progress_reporter=events.append,
        )

        assert run["run_status"] == "succeeded"
        assert events
        assert any(
            str(event.get("phase")) == "cluster_run" and str(event.get("subphase")) == "build_raw_neighbors"
            for event in events
        )
        assert any(
            str(event.get("phase")) == "cluster_run" and str(event.get("subphase")) == "persist_final_clusters"
            for event in events
        )
        assert all("total_count" in event for event in events)
        assert all("completed_count" in event for event in events)
        assert all("percent" in event for event in events)
        assert all(int(event["completed_count"]) <= int(event["total_count"]) for event in events)
        assert any(int(event["completed_count"]) == int(event["total_count"]) for event in events)
    finally:
        ws.close()


def test_execute_run_failed_persists_failure_json(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "cluster-failed")
    try:
        ws.seed_split_and_attachment_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        service = ws.new_cluster_run_service()

        def _raise_in_algorithm(**_: object) -> dict[str, object]:
            raise RuntimeError("inject_algorithm_failure")

        service.algorithm.build_run_plan = _raise_in_algorithm  # type: ignore[method-assign]
        with pytest.raises(Exception):
            service.execute_run(
                observation_snapshot_id=int(snapshot["snapshot_id"]),
                cluster_profile_id=ws.cluster_profile_id,
                supersedes_run_id=None,
                select_as_review_target=False,
            )
        failed_rows = ws.conn.execute(
            """
            SELECT id, run_status, failure_json
            FROM identity_cluster_run
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchall()
        assert failed_rows
        assert str(failed_rows[0]["run_status"]) == "failed"
        assert "error" in str(failed_rows[0]["failure_json"])
        assert "inject_algorithm_failure" in str(failed_rows[0]["failure_json"])
    finally:
        ws.close()


def test_execute_run_rejects_invalid_snapshot_without_fallback(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "cluster-invalid-snapshot")
    try:
        before = int(
            ws.conn.execute("SELECT COUNT(*) AS c FROM identity_cluster_run").fetchone()["c"]  # type: ignore[index]
        )
        with pytest.raises(ValueError, match="snapshot"):
            ws.new_cluster_run_service().execute_run(
                observation_snapshot_id=999999,
                cluster_profile_id=ws.cluster_profile_id,
                supersedes_run_id=None,
                select_as_review_target=False,
            )
        after = int(
            ws.conn.execute("SELECT COUNT(*) AS c FROM identity_cluster_run").fetchone()["c"]  # type: ignore[index]
        )
        assert after == before
    finally:
        ws.close()


def test_execute_run_rolls_back_clusters_when_resolution_insert_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "cluster-rollback")
    try:
        ws.seed_split_and_attachment_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        service = ws.new_cluster_run_service()
        original_insert = service.cluster_repo.insert_cluster_resolution

        def _raise_once(*args: object, **kwargs: object) -> int:
            raise RuntimeError("inject_resolution_failure")

        monkeypatch.setattr(service.cluster_repo, "insert_cluster_resolution", _raise_once)
        with pytest.raises(RuntimeError, match="inject_resolution_failure"):
            service.execute_run(
                observation_snapshot_id=int(snapshot["snapshot_id"]),
                cluster_profile_id=ws.cluster_profile_id,
                supersedes_run_id=None,
                select_as_review_target=False,
            )

        failed_run = ws.conn.execute(
            """
            SELECT id, run_status, failure_json
            FROM identity_cluster_run
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        assert failed_run is not None
        run_id = int(failed_run["id"])
        assert str(failed_run["run_status"]) == "failed"
        assert "inject_resolution_failure" in str(failed_run["failure_json"])
        assert ws.count_clusters(run_id=run_id) == 0
        assert ws.count_cluster_members(run_id=run_id) == 0
        assert ws.count_cluster_resolutions(run_id=run_id) == 0

        monkeypatch.setattr(service.cluster_repo, "insert_cluster_resolution", original_insert)
    finally:
        ws.close()


def test_execute_run_marks_failed_when_lineage_stage_invalid(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "cluster-invalid-lineage")
    try:
        ws.seed_split_and_attachment_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        service = ws.new_cluster_run_service()
        original_build_plan = service.algorithm.build_run_plan

        def _build_plan_with_invalid_lineage(**kwargs: object) -> dict[str, object]:
            plan = original_build_plan(**kwargs)
            bad = dict(plan)
            bad_lineage = list(plan.get("lineage") or [])
            bad_lineage.append(
                {
                    "parent_stage": "raw",
                    "parent_index": 0,
                    "child_stage": "bad_stage",
                    "child_index": 0,
                    "relation_kind": "split",
                    "reason_code": "inject_bad_lineage",
                    "detail_json": {},
                }
            )
            bad["lineage"] = bad_lineage
            return bad

        service.algorithm.build_run_plan = _build_plan_with_invalid_lineage  # type: ignore[method-assign]
        with pytest.raises(ValueError, match="lineage child_stage 非法"):
            service.execute_run(
                observation_snapshot_id=int(snapshot["snapshot_id"]),
                cluster_profile_id=ws.cluster_profile_id,
                supersedes_run_id=None,
                select_as_review_target=False,
            )

        failed_run = ws.conn.execute(
            """
            SELECT id, run_status, failure_json
            FROM identity_cluster_run
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        assert failed_run is not None
        run_id = int(failed_run["id"])
        assert str(failed_run["run_status"]) == "failed"
        assert "lineage child_stage 非法" in str(failed_run["failure_json"])
        assert ws.count_clusters(run_id=run_id) == 0
        assert ws.count_cluster_members(run_id=run_id) == 0
        assert ws.count_cluster_resolutions(run_id=run_id) == 0
    finally:
        ws.close()
