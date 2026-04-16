from __future__ import annotations

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
        try:
            profile_id = self.repo.insert_profile(
                {column: candidate[column] for column in self.roundtrip_columns()}
            )
            self.conn.commit()
            return profile_id
        except Exception:
            self.conn.rollback()
            raise

    def activate_profile(self, profile_id: int) -> dict[str, Any]:
        profile = self.repo.get_profile(profile_id)
        if profile is None:
            raise ValueError(f"目标 profile 不存在：{int(profile_id)}")
        self._validate_activation_preconditions(profile)

        try:
            changed = self.repo.activate_profile_transactional(profile_id)
            if changed == 0:
                raise RuntimeError(f"激活 profile 失败：{int(profile_id)}")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        active_profile = self.repo.get_active_profile()
        if active_profile is None:
            raise RuntimeError("激活后未找到 active profile。")
        return active_profile

    def get_active_profile(self) -> dict[str, Any] | None:
        return self.repo.get_active_profile()

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
