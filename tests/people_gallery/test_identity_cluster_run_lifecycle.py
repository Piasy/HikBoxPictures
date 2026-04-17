from datetime import datetime
from pathlib import Path
import sqlite3

import pytest

from .fixtures_identity_v3_1 import build_identity_phase1_workspace


def _build_snapshot(ws) -> int:
    ws.seed_observation_mix_case()
    snapshot = ws.new_observation_snapshot_service().build_snapshot(
        observation_profile_id=ws.observation_profile_id,
        candidate_knn_limit=24,
    )
    return int(snapshot["snapshot_id"])


def _query_run_status(db_path: Path, run_id: int) -> str:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT run_status
            FROM identity_cluster_run
            WHERE id = ?
            """,
            (int(run_id),),
        ).fetchone()
        if row is None:
            raise AssertionError(f"cluster run 不存在: {int(run_id)}")
        return str(row[0])
    finally:
        conn.close()


def test_run_status_flow_covers_created_running_succeeded_failed_cancelled(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "run-status-flow")
    try:
        snapshot_id = _build_snapshot(ws)
        service = ws.new_cluster_run_service()

        running_run = service.create_run(
            observation_snapshot_id=snapshot_id,
            cluster_profile_id=ws.cluster_profile_id,
            algorithm_version="identity.cluster.v3_1",
            supersedes_run_id=None,
        )
        service.mark_run_running(run_id=int(running_run["run_id"]))
        service.mark_run_succeeded(
            run_id=int(running_run["run_id"]),
            summary_json={"cluster_count": 0},
            select_as_review_target=False,
        )
        succeeded_row = ws.get_cluster_run(int(running_run["run_id"]))
        assert succeeded_row["run_status"] == "succeeded"

        failed_run = service.create_run(
            observation_snapshot_id=snapshot_id,
            cluster_profile_id=ws.cluster_profile_id,
            algorithm_version="identity.cluster.v3_1",
            supersedes_run_id=int(running_run["run_id"]),
        )
        service.mark_run_running(run_id=int(failed_run["run_id"]))
        service.mark_run_failed(
            run_id=int(failed_run["run_id"]),
            reason="cluster execution failed",
        )
        failed_row = ws.get_cluster_run(int(failed_run["run_id"]))
        assert failed_row["run_status"] == "failed"

        cancelled_run = service.create_run(
            observation_snapshot_id=snapshot_id,
            cluster_profile_id=ws.cluster_profile_id,
            algorithm_version="identity.cluster.v3_1",
            supersedes_run_id=int(failed_run["run_id"]),
        )
        service.mark_run_cancelled(
            run_id=int(cancelled_run["run_id"]),
            reason="operator_cancelled_for_rerun",
        )
        cancelled_row = ws.get_cluster_run(int(cancelled_run["run_id"]))
        assert cancelled_row["run_status"] == "cancelled"
    finally:
        ws.close()


def test_first_succeeded_run_auto_selected_as_review_target(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "run-lifecycle")
    try:
        snapshot_id = _build_snapshot(ws)
        service = ws.new_cluster_run_service()

        run_a = service.create_run(
            observation_snapshot_id=snapshot_id,
            cluster_profile_id=ws.cluster_profile_id,
            algorithm_version="identity.cluster.v3_1",
            supersedes_run_id=None,
        )
        service.mark_run_succeeded(
            run_id=int(run_a["run_id"]),
            summary_json={"cluster_count": 0},
            select_as_review_target=False,
        )

        first = ws.get_cluster_run(int(run_a["run_id"]))
        assert bool(first["is_review_target"]) is True
        assert first["review_selected_at"] == first["finished_at"]
        assert str(first["review_selected_at"]).count("T") == 0
        assert str(first["finished_at"]).count("T") == 0
        datetime.strptime(str(first["review_selected_at"]), "%Y-%m-%d %H:%M:%S")
        datetime.strptime(str(first["finished_at"]), "%Y-%m-%d %H:%M:%S")
        assert ws.count_review_targets() == 1
    finally:
        ws.close()


def test_select_review_target_switches_default_run_without_touching_owner(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "run-lifecycle-switch")
    try:
        snapshot_id = _build_snapshot(ws)
        service = ws.new_cluster_run_service()

        run_a = service.create_run(
            observation_snapshot_id=snapshot_id,
            cluster_profile_id=ws.cluster_profile_id,
            algorithm_version="identity.cluster.v3_1",
            supersedes_run_id=None,
        )
        service.mark_run_succeeded(
            run_id=int(run_a["run_id"]),
            summary_json={"cluster_count": 0},
            select_as_review_target=False,
        )

        run_b = service.create_run(
            observation_snapshot_id=snapshot_id,
            cluster_profile_id=ws.cluster_profile_id,
            algorithm_version="identity.cluster.v3_1",
            supersedes_run_id=int(run_a["run_id"]),
        )
        service.mark_run_succeeded(
            run_id=int(run_b["run_id"]),
            summary_json={"cluster_count": 0},
            select_as_review_target=False,
        )

        ws.conn.execute(
            "UPDATE identity_cluster_run SET is_materialization_owner = 1, activated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (int(run_a["run_id"]),),
        )
        ws.conn.commit()

        service.select_review_target(run_id=int(run_b["run_id"]))

        selected = ws.get_cluster_run(int(run_b["run_id"]))
        previous = ws.get_cluster_run(int(run_a["run_id"]))
        assert bool(selected["is_review_target"]) is True
        assert bool(previous["is_review_target"]) is False
        assert bool(selected["is_materialization_owner"]) is False
        assert bool(previous["is_materialization_owner"]) is True
        assert str(selected["review_selected_at"]).count("T") == 0
        datetime.strptime(str(selected["review_selected_at"]), "%Y-%m-%d %H:%M:%S")
        assert ws.count_review_targets() == 1
    finally:
        ws.close()


def test_select_review_target_rejects_non_succeeded_run(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "run-lifecycle-guard")
    try:
        snapshot_id = _build_snapshot(ws)
        service = ws.new_cluster_run_service()

        created = service.create_run(
            observation_snapshot_id=snapshot_id,
            cluster_profile_id=ws.cluster_profile_id,
            algorithm_version="identity.cluster.v3_1",
            supersedes_run_id=None,
        )
        with pytest.raises(ValueError, match="succeeded"):
            service.select_review_target(run_id=int(created["run_id"]))
    finally:
        ws.close()


def test_cancelled_run_cannot_be_selected_as_review_target(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "run-lifecycle-cancelled")
    try:
        snapshot_id = _build_snapshot(ws)
        service = ws.new_cluster_run_service()

        created = service.create_run(
            observation_snapshot_id=snapshot_id,
            cluster_profile_id=ws.cluster_profile_id,
            algorithm_version="identity.cluster.v3_1",
            supersedes_run_id=None,
        )
        service.mark_run_cancelled(
            run_id=int(created["run_id"]),
            reason="operator_cancelled_for_rerun",
        )
        cancelled = ws.get_cluster_run(int(created["run_id"]))
        assert cancelled["run_status"] == "cancelled"
        with pytest.raises(ValueError, match="succeeded"):
            service.select_review_target(run_id=int(created["run_id"]))
    finally:
        ws.close()


def test_state_transitions_are_committed_for_cross_connection_visibility(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "run-lifecycle-cross-conn")
    try:
        snapshot_id = _build_snapshot(ws)
        service = ws.new_cluster_run_service()
        created = service.create_run(
            observation_snapshot_id=snapshot_id,
            cluster_profile_id=ws.cluster_profile_id,
            algorithm_version="identity.cluster.v3_1",
            supersedes_run_id=None,
        )
        run_id = int(created["run_id"])
        assert _query_run_status(ws.paths.db_path, run_id) == "created"

        service.mark_run_running(run_id=run_id)
        assert _query_run_status(ws.paths.db_path, run_id) == "running"

        service.mark_run_failed(run_id=run_id, reason="cross_connection_visibility")
        assert _query_run_status(ws.paths.db_path, run_id) == "failed"
    finally:
        ws.close()


def test_conditional_status_transition_rejects_stale_expected_status(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "run-lifecycle-cas")
    try:
        snapshot_id = _build_snapshot(ws)
        service = ws.new_cluster_run_service()
        created = service.create_run(
            observation_snapshot_id=snapshot_id,
            cluster_profile_id=ws.cluster_profile_id,
            algorithm_version="identity.cluster.v3_1",
            supersedes_run_id=None,
        )
        run_id = int(created["run_id"])
        updated = service.cluster_run_repo.update_run_status(
            run_id=run_id,
            run_status="running",
            summary_json={},
            failure_json={},
            expected_statuses=("failed",),
        )
        assert updated is False
        persisted = ws.get_cluster_run(run_id)
        assert persisted["run_status"] == "created"
    finally:
        ws.close()


def test_mark_run_succeeded_rolls_back_when_review_target_update_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "run-lifecycle-rollback")
    try:
        snapshot_id = _build_snapshot(ws)
        service = ws.new_cluster_run_service()
        created = service.create_run(
            observation_snapshot_id=snapshot_id,
            cluster_profile_id=ws.cluster_profile_id,
            algorithm_version="identity.cluster.v3_1",
            supersedes_run_id=None,
        )
        run_id = int(created["run_id"])

        def _raise_set_review_target(*, run_id: int, review_selected_at: str | None = None) -> None:
            raise RuntimeError("inject_set_review_target_failure")

        monkeypatch.setattr(service.cluster_run_repo, "set_review_target", _raise_set_review_target)
        with pytest.raises(RuntimeError, match="inject_set_review_target_failure"):
            service.mark_run_succeeded(
                run_id=run_id,
                summary_json={"cluster_count": 3},
                select_as_review_target=False,
            )

        persisted = ws.get_cluster_run(run_id)
        assert persisted["run_status"] == "created"
        assert bool(persisted["is_review_target"]) is False
        assert persisted["review_selected_at"] is None
    finally:
        ws.close()


def test_mark_run_succeeded_raises_when_cas_update_reports_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "run-lifecycle-service-cas")
    try:
        snapshot_id = _build_snapshot(ws)
        service = ws.new_cluster_run_service()
        created = service.create_run(
            observation_snapshot_id=snapshot_id,
            cluster_profile_id=ws.cluster_profile_id,
            algorithm_version="identity.cluster.v3_1",
            supersedes_run_id=None,
        )
        run_id = int(created["run_id"])
        original_update = service.cluster_run_repo.update_run_status

        def _conflict_once(
            *,
            run_id: int,
            run_status: str,
            summary_json: dict[str, object] | None,
            failure_json: dict[str, object] | None,
            expected_statuses: tuple[str, ...] | None = None,
        ) -> bool:
            if run_status == "succeeded":
                return False
            return original_update(
                run_id=run_id,
                run_status=run_status,
                summary_json=summary_json,
                failure_json=failure_json,
                expected_statuses=expected_statuses,
            )

        monkeypatch.setattr(service.cluster_run_repo, "update_run_status", _conflict_once)
        with pytest.raises(ValueError, match="并发冲突"):
            service.mark_run_succeeded(
                run_id=run_id,
                summary_json={"cluster_count": 1},
                select_as_review_target=False,
            )
        persisted = ws.get_cluster_run(run_id)
        assert persisted["run_status"] == "created"
        assert bool(persisted["is_review_target"]) is False
    finally:
        ws.close()
