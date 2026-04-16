from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.repositories import IdentityRepo


class IdentityThresholdProfileService:
    SYSTEM_COLUMNS = {"id", "active", "activated_at", "created_at", "updated_at"}
    EMBEDDING_BINDING_COLUMNS = (
        "embedding_feature_type",
        "embedding_model_key",
        "embedding_distance_metric",
        "embedding_schema_version",
    )

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.repo = IdentityRepo(conn)

    def roundtrip_columns(self) -> list[str]:
        columns = self.repo.list_profile_columns()
        return [name for name in columns if name not in self.SYSTEM_COLUMNS]

    def validate_candidate_keys(self, candidate: dict[str, Any]) -> None:
        required = set(self.roundtrip_columns())
        incoming = set(candidate.keys())
        missing = sorted(required - incoming)
        extra = sorted(incoming - required)
        if missing:
            raise ValueError(f"candidate profile 缺失字段: {missing}")
        if extra:
            raise ValueError(f"candidate profile 非法字段: {extra}")

    def build_candidate_profile_from_active(self) -> dict[str, Any]:
        active_profile = self.repo.get_active_profile()
        if active_profile is None:
            raise ValueError("当前没有 active profile，无法导出候选配置。")
        return {key: active_profile[key] for key in self.roundtrip_columns()}

    def insert_candidate_profile_from_json_dict(self, candidate: dict[str, Any]) -> int:
        self.validate_candidate_keys(candidate)
        managed_transaction = not self.conn.in_transaction
        try:
            profile_id = self.repo.insert_profile(
                {column: candidate[column] for column in self.roundtrip_columns()}
            )
            if managed_transaction:
                self.conn.commit()
            return profile_id
        except Exception:
            if managed_transaction and self.conn.in_transaction:
                self.conn.rollback()
            raise

    def activate_profile(self, profile_id: int) -> dict[str, Any]:
        profile = self.repo.get_profile(profile_id)
        if profile is None:
            raise ValueError(f"目标 profile 不存在：{int(profile_id)}")
        self._validate_activation_preconditions(profile)

        managed_transaction = not self.conn.in_transaction
        try:
            changed = self.repo.activate_profile_transactional(profile_id)
            if changed == 0:
                raise RuntimeError(f"激活 profile 失败：{int(profile_id)}")
            if managed_transaction:
                self.conn.commit()
        except Exception:
            if managed_transaction and self.conn.in_transaction:
                self.conn.rollback()
            raise

        active_profile = self.repo.get_active_profile()
        if active_profile is None:
            raise RuntimeError("激活后未找到 active profile。")
        return active_profile

    def get_active_profile(self) -> dict[str, Any] | None:
        return self.repo.get_active_profile()

    def resolve_profile_for_rebuild(self, threshold_profile_path: Path | None) -> dict[str, Any]:
        if threshold_profile_path is not None:
            payload = json.loads(Path(threshold_profile_path).read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("threshold-profile JSON 必须是对象")
            profile_id = self.insert_candidate_profile_from_json_dict(payload)
            self.activate_profile(profile_id)
            return {
                "profile_id": int(profile_id),
                "profile_mode": "imported",
                "imported_threshold_profile": True,
                "update_profile_quantiles": False,
            }

        active = self.get_active_profile()
        if active is None:
            raise ValueError("当前没有 active profile，无法执行重建。")
        candidate = self.build_candidate_profile_from_active()
        profile_id = self.insert_candidate_profile_from_json_dict(candidate)
        self.activate_profile(profile_id)
        return {
            "profile_id": int(profile_id),
            "profile_mode": "derived",
            "imported_threshold_profile": False,
            "update_profile_quantiles": True,
        }

    def get_profile_model_key(self, profile_id: int) -> str:
        profile = self.repo.get_profile_required(int(profile_id))
        model_key = str(profile.get("embedding_model_key") or "").strip()
        if not model_key:
            raise ValueError(f"profile 缺少 embedding_model_key: {int(profile_id)}")
        return model_key

    def _validate_activation_preconditions(self, profile: dict[str, Any]) -> None:
        if int(profile["bootstrap_min_high_quality_count"]) < int(profile["bootstrap_seed_min_count"]):
            raise ValueError("bootstrap_min_high_quality_count 不得小于 bootstrap_seed_min_count")

        workspace_binding = self.repo.detect_workspace_embedding_binding()
        if workspace_binding is None:
            raise ValueError("embedding 绑定缺失，当前 workspace 没有可用向量。")

        profile_binding = {
            key: str(profile[key]) for key in self.EMBEDDING_BINDING_COLUMNS
        }
        if profile_binding != workspace_binding:
            raise ValueError(
                f"embedding 绑定不匹配: profile={profile_binding}, workspace={workspace_binding}"
            )
