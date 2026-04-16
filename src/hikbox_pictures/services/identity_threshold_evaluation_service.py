from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.repositories.identity_repo import IdentityRepo
from hikbox_pictures.services.identity_bootstrap_service import IdentityBootstrapService
from hikbox_pictures.services.identity_threshold_profile_service import IdentityThresholdProfileService


class IdentityThresholdEvaluationService:
    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        db_path = self.workspace / ".hikbox" / "library.db"
        if not db_path.exists():
            raise FileNotFoundError(f"数据库不存在: {db_path}")
        self.conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.identity_repo = IdentityRepo(self.conn)
        self.profile_service = IdentityThresholdProfileService(self.conn)
        self.bootstrap_service = IdentityBootstrapService(
            self.conn,
            identity_repo=self.identity_repo,
            person_repo=None,
            prototype_service=None,
        )

    def close(self) -> None:
        self.conn.close()

    def evaluate(self) -> dict[str, Any]:
        active_profile = self.profile_service.get_active_profile()
        if active_profile is None:
            raise ValueError("当前没有 active profile，无法执行阈值评估。")

        candidate_profile = self.profile_service.build_candidate_profile_from_active()
        planned = self.bootstrap_service.plan_bootstrap(profile_id=int(active_profile["id"]))

        summary: dict[str, Any] = {
            "bootstrap_estimated_person_count": int(planned["materialized_cluster_count"]),
            "estimated_new_person_review_count": int(planned["review_pending_cluster_count"]),
            "estimated_low_confidence_assignment_count": int(planned["estimated_low_confidence_assignment_count"]),
            "cluster_size_distribution": planned["cluster_size_distribution"],
            "distinct_photo_distribution": planned["distinct_photo_distribution"],
            "quality_distribution": planned["quality_distribution"],
            "trusted_reject_reason_distribution": planned["trusted_reject_reason_distribution"],
            "diff_vs_active_profile": self._diff_profile(active_profile, candidate_profile),
        }
        return {
            "summary": summary,
            "candidate_profile": candidate_profile,
        }

    def _diff_profile(self, active_profile: dict[str, Any], candidate_profile: dict[str, Any]) -> dict[str, Any]:
        diff: dict[str, Any] = {}
        for key, candidate_value in candidate_profile.items():
            active_value = active_profile.get(key)
            if active_value != candidate_value:
                diff[key] = {
                    "from": active_value,
                    "to": candidate_value,
                }
        return diff
