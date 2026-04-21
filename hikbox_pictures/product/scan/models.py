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


@dataclass(frozen=True)
class AssetFileState:
    file_size: int
    mtime_ns: int


@dataclass(frozen=True)
class DiscoverSourceSummary:
    source_id: int
    discovered_assets: int
    rerun_assets: int
    unchanged_assets: int
    failed_assets: int = 0


@dataclass(frozen=True)
class DiscoverRunSummary:
    by_source: dict[int, DiscoverSourceSummary]


@dataclass(frozen=True)
class MetadataSourceSummary:
    processed_assets: int
    failed_assets: int = 0
