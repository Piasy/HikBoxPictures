from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

import pytest

from hikbox_pictures.services.observation_quality_backfill_service import ObservationQualityBackfillService

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace_observation_quality_backfill", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_identity_real_workspace = _MODULE.build_identity_real_workspace


@pytest.fixture
def identity_real_workspace(tmp_path: Path):
    workspace = tmp_path / "identity-real-workspace"
    ws = build_identity_real_workspace(workspace)
    try:
        yield ws
    finally:
        ws.close()


def test_backfill_reads_crop_or_recrops_from_original(identity_real_workspace) -> None:
    svc = ObservationQualityBackfillService(identity_real_workspace.conn)
    obs_keep_crop = identity_real_workspace.pick_observation_with_crop()
    obs_force_recrop = identity_real_workspace.pick_observation_with_crop()
    identity_real_workspace.break_crop_for_observation(obs_force_recrop)

    report = svc.backfill_all_observations(profile_id=identity_real_workspace.profile_id)
    assert int(report["updated_observation_count"]) >= 2

    row_a = identity_real_workspace.get_observation(obs_keep_crop)
    row_b = identity_real_workspace.get_observation(obs_force_recrop)
    assert row_a is not None
    assert row_b is not None
    assert float(row_a["sharpness_score"]) > 0.0
    assert float(row_b["sharpness_score"]) > 0.0
    assert float(row_a["sharpness_score"]) != float(row_b["sharpness_score"])
    assert row_b["quality_score"] is not None


def test_backfill_fails_if_crop_and_original_both_missing(identity_real_workspace) -> None:
    svc = ObservationQualityBackfillService(identity_real_workspace.conn)
    observation_id, photo_id = identity_real_workspace.pick_observation_and_photo()
    identity_real_workspace.break_crop_for_observation(observation_id)
    identity_real_workspace.break_original_for_photo(photo_id)

    with pytest.raises(FileNotFoundError):
        svc.backfill_all_observations(profile_id=identity_real_workspace.profile_id)


def test_backfill_returns_sharpness_quantiles_for_orchestrator(identity_real_workspace) -> None:
    svc = ObservationQualityBackfillService(identity_real_workspace.conn)
    report = svc.backfill_all_observations(
        profile_id=identity_real_workspace.profile_id,
        update_profile_quantiles=False,
    )
    assert float(report["sharpness_log_p90"]) > float(report["sharpness_log_p10"])
    assert float(report["area_log_p90"]) > float(report["area_log_p10"])


def test_backfill_does_not_rewrite_profile_quantiles_when_disabled(identity_real_workspace) -> None:
    svc = ObservationQualityBackfillService(identity_real_workspace.conn)
    before = identity_real_workspace.get_profile(identity_real_workspace.profile_id)
    assert before is not None

    svc.backfill_all_observations(
        profile_id=identity_real_workspace.profile_id,
        update_profile_quantiles=False,
    )
    after = identity_real_workspace.get_profile(identity_real_workspace.profile_id)
    assert after is not None
    assert float(after["area_log_p10"]) == float(before["area_log_p10"])
    assert float(after["area_log_p90"]) == float(before["area_log_p90"])
    assert float(after["sharpness_log_p10"]) == float(before["sharpness_log_p10"])
    assert float(after["sharpness_log_p90"]) == float(before["sharpness_log_p90"])


def test_backfill_can_update_profile_quantiles_when_explicitly_enabled(identity_real_workspace) -> None:
    svc = ObservationQualityBackfillService(identity_real_workspace.conn)
    svc.backfill_all_observations(
        profile_id=identity_real_workspace.profile_id,
        update_profile_quantiles=True,
    )
    profile = identity_real_workspace.get_profile(identity_real_workspace.profile_id)
    assert profile is not None
    assert float(profile["area_log_p90"]) > float(profile["area_log_p10"])
    assert float(profile["sharpness_log_p90"]) > float(profile["sharpness_log_p10"])


def test_backfill_rollbacks_without_partial_db_update_and_cleans_new_crop(identity_real_workspace, monkeypatch) -> None:
    conn = identity_real_workspace.conn
    svc = ObservationQualityBackfillService(conn)
    force_recrop_observation = identity_real_workspace.pick_observation_with_crop()
    identity_real_workspace.break_crop_for_observation(force_recrop_observation)
    before_rows = conn.execute(
        "SELECT id, sharpness_score, quality_score, crop_path FROM face_observation ORDER BY id ASC"
    ).fetchall()
    before_by_id = {int(row["id"]): dict(row) for row in before_rows}
    expected_new_crop = svc.face_crop_dir / f"obs-{int(force_recrop_observation)}.jpg"
    assert not expected_new_crop.exists()

    original_update_quality = svc.asset_repo.update_observation_quality_score
    call_count = 0

    def _fail_on_first_quality_update(observation_id: int, quality_score: float) -> int:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("inject-db-failure")
        return original_update_quality(observation_id, quality_score)

    monkeypatch.setattr(svc.asset_repo, "update_observation_quality_score", _fail_on_first_quality_update)
    with pytest.raises(RuntimeError, match="inject-db-failure"):
        svc.backfill_all_observations(profile_id=identity_real_workspace.profile_id)

    after_rows = conn.execute(
        "SELECT id, sharpness_score, quality_score, crop_path FROM face_observation ORDER BY id ASC"
    ).fetchall()
    after_by_id = {int(row["id"]): dict(row) for row in after_rows}
    assert after_by_id == before_by_id
    assert not expected_new_crop.exists()


def test_backfill_with_external_transaction_does_not_self_commit_or_rollback(
    identity_real_workspace,
    monkeypatch,
) -> None:
    conn = identity_real_workspace.conn
    svc = ObservationQualityBackfillService(conn)
    before_rows = conn.execute("SELECT id, sharpness_score FROM face_observation ORDER BY id ASC").fetchall()
    before_sharpness_by_id = {int(row["id"]): row["sharpness_score"] for row in before_rows}

    original_update_quality = svc.asset_repo.update_observation_quality_score
    call_count = 0

    def _fail_on_first_quality_update(observation_id: int, quality_score: float) -> int:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("inject-managed-failure")
        return original_update_quality(observation_id, quality_score)

    monkeypatch.setattr(svc.asset_repo, "update_observation_quality_score", _fail_on_first_quality_update)

    conn.execute("BEGIN")
    try:
        with pytest.raises(RuntimeError, match="inject-managed-failure"):
            svc.backfill_all_observations(profile_id=identity_real_workspace.profile_id)
        assert conn.in_transaction
        after_rows = conn.execute("SELECT id, sharpness_score FROM face_observation ORDER BY id ASC").fetchall()
        after_sharpness_by_id: dict[int, Any] = {int(row["id"]): row["sharpness_score"] for row in after_rows}
        assert any(after_sharpness_by_id[obs_id] != before_sharpness_by_id[obs_id] for obs_id in before_sharpness_by_id)
    finally:
        conn.rollback()
