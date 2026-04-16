from __future__ import annotations

import math
from typing import Any


class QualityScoreService:
    def compute_quality_score(
        self,
        *,
        face_area_ratio: float | None,
        sharpness_score: float | None,
        pose_score: float | None,
        profile: dict[str, Any],
    ) -> float:
        area_log = math.log10(max(float(face_area_ratio or 0.0), 1e-6))
        sharpness_log = math.log1p(max(float(sharpness_score or 0.0), 0.0))
        pose_norm = float(pose_score) if pose_score is not None else 0.0

        area_score = self._normalize(
            area_log,
            float(profile["area_log_p10"]),
            float(profile["area_log_p90"]),
        )
        sharpness_norm_score = self._normalize(
            sharpness_log,
            float(profile["sharpness_log_p10"]),
            float(profile["sharpness_log_p90"]),
        )
        weighted = (
            float(profile["quality_area_weight"]) * area_score
            + float(profile["quality_sharpness_weight"]) * sharpness_norm_score
            + float(profile.get("quality_pose_weight") or 0.0) * pose_norm
        )
        return min(1.0, max(0.0, float(weighted)))

    def _normalize(self, value: float, low: float, high: float) -> float:
        if high <= low:
            return 0.0
        return min(1.0, max(0.0, (float(value) - float(low)) / (float(high) - float(low))))
