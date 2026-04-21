from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScanSession:
    id: int
    run_kind: str
    status: str
    triggered_by: str
    resume_from_session_id: int | None
    started_at: str | None
    finished_at: str | None
    last_error: str | None
    created_at: str
    updated_at: str
    resumed: bool = False
