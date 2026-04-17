from pathlib import Path

import pytest

from hikbox_pictures.services.identity_cluster_profile_service import IdentityClusterProfileService

from .fixtures_identity_v3_1 import build_identity_phase1_workspace


def test_get_active_cluster_profile_id_returns_workspace_active_profile(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "cluster-profile-active")
    try:
        service = IdentityClusterProfileService(ws.conn)
        assert service.get_active_profile_id() == ws.cluster_profile_id
    finally:
        ws.close()


def test_get_active_cluster_profile_id_raises_when_no_active_profile(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "cluster-profile-missing")
    try:
        ws.conn.execute("UPDATE identity_cluster_profile SET active = 0, activated_at = NULL")
        ws.conn.commit()

        service = IdentityClusterProfileService(ws.conn)
        with pytest.raises(ValueError, match="active cluster profile"):
            service.get_active_profile_id()
    finally:
        ws.close()
