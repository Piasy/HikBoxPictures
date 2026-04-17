from __future__ import annotations

from pathlib import Path

from hikbox_pictures.services.identity_observation_profile_service import IdentityObservationProfileService

from .fixtures_identity_v3_1 import build_identity_phase1_workspace


def test_get_active_profile_id_returns_migrated_observation_profile(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "profile-active")
    try:
        service = IdentityObservationProfileService(ws.conn)
        assert service.get_active_profile_id() == ws.observation_profile_id
    finally:
        ws.close()


def test_get_active_profile_id_rejects_missing_active_profile(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "profile-missing")
    try:
        ws.conn.execute("UPDATE identity_observation_profile SET active = 0")
        ws.conn.commit()

        service = IdentityObservationProfileService(ws.conn)
        try:
            service.get_active_profile_id()
            raise AssertionError("预期抛出 ValueError")
        except ValueError as exc:
            assert "active observation profile" in str(exc)
    finally:
        ws.close()
