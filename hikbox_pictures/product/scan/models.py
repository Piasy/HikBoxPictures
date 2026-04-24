"""扫描会话与阶段模型定义。"""

from __future__ import annotations

from dataclasses import dataclass

ALLOWED_RUN_KIND = {"scan_full", "scan_incremental", "scan_resume"}
ALLOWED_TRIGGERED_BY = {"manual_webui", "manual_cli"}
ACTIVE_STATUS = {"running", "aborting"}


@dataclass(frozen=True)
class ScanSessionRecord:
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


@dataclass(frozen=True)
class ScanStartResult:
    session_id: int
    resumed: bool
    should_execute: bool


@dataclass(frozen=True)
class DiscoverSourceSummary:
    source_id: int
    discovered_assets: int
    processed_assets: int
    failed_assets: int
    should_rerun: bool


@dataclass(frozen=True)
class DiscoverStageSummary:
    by_source: dict[int, DiscoverSourceSummary]


@dataclass(frozen=True)
class MetadataSourceSummary:
    source_id: int
    processed_assets: int
    failed_assets: int
    live_photo_assets: int


@dataclass(frozen=True)
class MetadataStageSummary:
    by_source: dict[int, MetadataSourceSummary]
