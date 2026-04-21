from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScanCheckpoint:
    scan_session_id: int
    stage: str
    cursor_json: str
    processed_count: int


class ScanCheckpointService:
    """Task 2 最小占位接口，供后续任务扩展真实 checkpoint 持久化。"""

    def save_checkpoint(
        self,
        *,
        scan_session_id: int,
        stage: str,
        cursor_json: str,
        processed_count: int,
    ) -> ScanCheckpoint:
        return ScanCheckpoint(
            scan_session_id=scan_session_id,
            stage=stage,
            cursor_json=cursor_json,
            processed_count=processed_count,
        )
