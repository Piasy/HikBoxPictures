from __future__ import annotations

from pathlib import Path

import pytest

from .fixtures_identity_v3_1 import build_identity_phase1_workspace


def test_build_snapshot_persists_pool_counts_and_dedup_metadata(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "snapshot-build")
    try:
        ws.seed_observation_mix_case()
        service = ws.new_observation_snapshot_service()

        snapshot = service.build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=16,
        )

        assert snapshot["reused"] is False
        assert snapshot["pool_counts"] == {
            "core_discovery": 4,
            "attachment": 2,
            "excluded": 3,
        }
        shadow_rows = ws.list_pool_entries(
            snapshot_id=int(snapshot["snapshot_id"]),
            pool_kind="excluded",
            excluded_reason="duplicate_shadow",
        )
        assert shadow_rows
        assert shadow_rows[0]["representative_observation_id"] is not None
        assert shadow_rows[0]["dedup_group_key"]
        assert shadow_rows[0]["diagnostic_json"]["dedup_group_key"]
        assert ws.backfill_call_count == 1
    finally:
        ws.close()


def test_snapshot_reuses_when_profile_dataset_and_candidate_policy_match(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "snapshot-reuse")
    try:
        ws.seed_observation_mix_case()
        service = ws.new_observation_snapshot_service()

        first = service.build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        second = service.build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=12,
        )

        assert first["snapshot_id"] == second["snapshot_id"]
        assert second["reused"] is True
        assert ws.backfill_call_count == 2
    finally:
        ws.close()


def test_snapshot_rebuilds_when_required_knn_exceeds_max_supported(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "snapshot-rebuild-knn")
    try:
        ws.seed_observation_mix_case()
        service = ws.new_observation_snapshot_service()

        first = service.build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=12,
        )
        second = service.build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )

        assert first["snapshot_id"] != second["snapshot_id"]
        assert second["reused"] is False
    finally:
        ws.close()


def test_snapshot_rebuilds_when_candidate_policy_or_dataset_changes(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "snapshot-rebuild-policy")
    try:
        ws.seed_observation_mix_case()
        service = ws.new_observation_snapshot_service()

        first = service.build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        ws.conn.execute(
            """
            UPDATE identity_observation_profile
            SET burst_window_seconds = burst_window_seconds + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (ws.observation_profile_id,),
        )
        ws.conn.commit()
        second = service.build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        assert first["snapshot_id"] != second["snapshot_id"]
        assert second["reused"] is False

        ws.seed_additional_observation_for_dataset_change()
        third = service.build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        assert third["snapshot_id"] != second["snapshot_id"]
        assert third["reused"] is False
    finally:
        ws.close()


def test_snapshot_rebuilds_when_quality_score_changes(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "snapshot-rebuild-quality-score")
    try:
        ws.seed_observation_mix_case()
        service = ws.new_observation_snapshot_service()

        first = service.build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        target = ws.conn.execute(
            """
            SELECT id
            FROM face_observation
            WHERE active = 1
            ORDER BY id ASC
            LIMIT 1
            """
        ).fetchone()
        assert target is not None
        ws.conn.execute(
            """
            UPDATE face_observation
            SET quality_score = quality_score + 0.123456
            WHERE id = ?
            """,
            (int(target["id"]),),
        )
        ws.conn.commit()
        second = service.build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )

        assert second["reused"] is False
        assert int(second["snapshot_id"]) != int(first["snapshot_id"])
    finally:
        ws.close()


def test_snapshot_rebuilds_when_algorithm_version_changes(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "snapshot-rebuild-algorithm-version")
    try:
        ws.seed_observation_mix_case()
        service = ws.new_observation_snapshot_service()

        first = service.build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        ws.conn.execute(
            """
            UPDATE identity_observation_snapshot
            SET algorithm_version = 'identity.observation_snapshot.v0'
            WHERE id = ?
            """,
            (int(first["snapshot_id"]),),
        )
        ws.conn.commit()

        second = service.build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        assert second["reused"] is False
        assert int(second["snapshot_id"]) != int(first["snapshot_id"])
    finally:
        ws.close()


def test_snapshot_build_marks_failed_when_population_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "snapshot-build-failed")
    try:
        ws.seed_observation_mix_case()
        service = ws.new_observation_snapshot_service()
        created_snapshot_ids: list[int] = []

        original_create_snapshot = service.observation_repo.create_snapshot

        def _track_create_snapshot(**kwargs):
            snapshot_id = original_create_snapshot(**kwargs)
            created_snapshot_ids.append(int(snapshot_id))
            return snapshot_id

        def _fail_population(**kwargs):
            raise RuntimeError("inject_population_failure")

        monkeypatch.setattr(service.observation_repo, "create_snapshot", _track_create_snapshot)
        monkeypatch.setattr(service.observation_repo, "populate_snapshot_entries", _fail_population)

        with pytest.raises(RuntimeError, match="inject_population_failure"):
            service.build_snapshot(
                observation_profile_id=ws.observation_profile_id,
                candidate_knn_limit=24,
            )

        assert created_snapshot_ids
        snapshot_id = int(created_snapshot_ids[0])
        snapshot_row = ws.conn.execute(
            """
            SELECT status, finished_at
            FROM identity_observation_snapshot
            WHERE id = ?
            """,
            (snapshot_id,),
        ).fetchone()
        assert snapshot_row is not None
        assert str(snapshot_row["status"]) == "failed"
        assert snapshot_row["finished_at"] is not None
        pool_entry_count = ws.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM identity_observation_pool_entry
            WHERE snapshot_id = ?
            """,
            (snapshot_id,),
        ).fetchone()
        assert pool_entry_count is not None
        assert int(pool_entry_count["c"]) == 0
    finally:
        ws.close()
