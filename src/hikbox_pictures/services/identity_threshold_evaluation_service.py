from __future__ import annotations

from pathlib import Path
from typing import Any


class IdentityThresholdEvaluationService:
    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()

    def close(self) -> None:
        return None

    def evaluate(self) -> dict[str, Any]:
        raise RuntimeError(
            "v3.1 Phase 1 已改为 snapshot + rerun + review 流程；不再支持 evaluate_identity_thresholds。"
        )
