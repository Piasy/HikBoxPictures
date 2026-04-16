from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.services.identity_threshold_profile_service import IdentityThresholdProfileService
from hikbox_pictures.workspace import init_workspace_layout

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace_identity_threshold_profile", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_seed_workspace_with_mock_embeddings = _MODULE.build_seed_workspace_with_mock_embeddings
seed_active_identity_threshold_profile = _MODULE.seed_active_identity_threshold_profile


def _build_service_with_seed(tmp_path: Path) -> tuple[object, IdentityThresholdProfileService, dict[str, object]]:
    workspace = tmp_path / "workspace"
    build_seed_workspace_with_mock_embeddings(workspace)
    paths = init_workspace_layout(workspace, workspace / ".hikbox")
    conn = connect_db(paths.db_path)
    seed = seed_active_identity_threshold_profile(conn)
    service = IdentityThresholdProfileService(conn)
    return conn, service, seed


def test_roundtrip_key_set_strictly_matches_table_non_system_columns(tmp_path: Path) -> None:
    conn, service, seed = _build_service_with_seed(tmp_path)
    try:
        roundtrip_keys = set(service.roundtrip_columns())
        seeded_keys = set(dict(seed["candidate_profile"]).keys())
        assert roundtrip_keys == seeded_keys
    finally:
        conn.close()


def test_insert_candidate_rejects_missing_or_extra_keys(tmp_path: Path) -> None:
    conn, service, _ = _build_service_with_seed(tmp_path)
    try:
        candidate = service.build_candidate_profile_from_active()

        missing = dict(candidate)
        missing.pop("profile_name")
        with pytest.raises(ValueError, match="缺失字段"):
            service.insert_candidate_profile_from_json_dict(missing)

        extra = dict(candidate)
        extra["unexpected"] = 1
        with pytest.raises(ValueError, match="非法字段"):
            service.insert_candidate_profile_from_json_dict(extra)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("embedding_feature_type", "body"),
        ("embedding_model_key", "other-model"),
        ("embedding_distance_metric", "l2"),
        ("embedding_schema_version", "face_embedding.v2"),
    ],
)
def test_activate_rejects_embedding_binding_mismatch(tmp_path: Path, field: str, bad_value: str) -> None:
    conn, service, _ = _build_service_with_seed(tmp_path)
    try:
        candidate = service.build_candidate_profile_from_active()
        candidate[field] = bad_value
        profile_id = service.insert_candidate_profile_from_json_dict(candidate)
        with pytest.raises(ValueError, match="embedding"):
            service.activate_profile(profile_id)
    finally:
        conn.close()


def test_activate_rejects_invalid_bootstrap_seed_relation(tmp_path: Path) -> None:
    conn, service, _ = _build_service_with_seed(tmp_path)
    try:
        candidate = service.build_candidate_profile_from_active()
        candidate["bootstrap_min_high_quality_count"] = 2
        candidate["bootstrap_seed_min_count"] = 3
        profile_id = service.insert_candidate_profile_from_json_dict(candidate)
        with pytest.raises(ValueError, match="bootstrap_min_high_quality_count"):
            service.activate_profile(profile_id)
    finally:
        conn.close()


def test_export_import_activate_roundtrip_success(tmp_path: Path) -> None:
    conn, service, seed = _build_service_with_seed(tmp_path)
    try:
        old_active_id = int(seed["active_profile_id"])
        exported = service.build_candidate_profile_from_active()
        exported["profile_name"] = "roundtrip-导入版"
        exported["profile_version"] = "v2"

        candidate_id = service.insert_candidate_profile_from_json_dict(exported)
        activated = service.activate_profile(candidate_id)
        active = service.get_active_profile()
        old_row = conn.execute(
            "SELECT active FROM identity_threshold_profile WHERE id = ?",
            (old_active_id,),
        ).fetchone()

        assert int(activated["id"]) == candidate_id
        assert int(activated["active"]) == 1
        assert active is not None
        assert int(active["id"]) == candidate_id
        assert active["profile_name"] == "roundtrip-导入版"
        assert active["profile_version"] == "v2"
        assert old_row is not None
        assert int(old_row["active"]) == 0
    finally:
        conn.close()


def test_roundtrip_changes_persist_across_connection(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    build_seed_workspace_with_mock_embeddings(workspace)
    paths = init_workspace_layout(workspace, workspace / ".hikbox")

    conn = connect_db(paths.db_path)
    try:
        seed = seed_active_identity_threshold_profile(conn)
        service = IdentityThresholdProfileService(conn)
        old_active_id = int(seed["active_profile_id"])

        candidate = service.build_candidate_profile_from_active()
        candidate["profile_name"] = "persist-check"
        new_profile_id = service.insert_candidate_profile_from_json_dict(candidate)
        service.activate_profile(new_profile_id)
    finally:
        conn.close()

    reopened = connect_db(paths.db_path)
    try:
        active = reopened.execute(
            """
            SELECT id, profile_name
            FROM identity_threshold_profile
            WHERE active = 1
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        old_row = reopened.execute(
            "SELECT active FROM identity_threshold_profile WHERE id = ?",
            (old_active_id,),
        ).fetchone()
        assert active is not None
        assert int(active["id"]) == int(new_profile_id)
        assert str(active["profile_name"]) == "persist-check"
        assert old_row is not None
        assert int(old_row["active"]) == 0
    finally:
        reopened.close()


def test_activate_rejects_non_unique_workspace_embedding_binding(tmp_path: Path) -> None:
    conn, service, _ = _build_service_with_seed(tmp_path)
    try:
        conn.execute(
            """
            UPDATE face_embedding
            SET model_key = 'pipeline-stub-v2'
            WHERE id = (
                SELECT id
                FROM face_embedding
                ORDER BY id ASC
                LIMIT 1
            )
            """
        )
        conn.commit()

        candidate = service.build_candidate_profile_from_active()
        profile_id = service.insert_candidate_profile_from_json_dict(candidate)
        with pytest.raises(ValueError, match="不唯一"):
            service.activate_profile(profile_id)
    finally:
        conn.close()
