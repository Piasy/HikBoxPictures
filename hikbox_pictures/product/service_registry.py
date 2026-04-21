from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .audit.service import AuditSamplingService
from .ops_event import OpsEventService
from .scan.session_service import SQLiteScanSessionRepository, ScanSessionService
from .source.repository import SQLiteSourceRepository
from .source.service import SourceService


@dataclass(frozen=True)
class ServiceRegistry:
    scan_session_service: ScanSessionService
    source_service: SourceService
    audit_service: AuditSamplingService
    ops_event_service: OpsEventService


def build_service_registry(*, library_db_path: Path) -> ServiceRegistry:
    scan_repo = SQLiteScanSessionRepository(library_db_path)
    source_repo = SQLiteSourceRepository(library_db_path)
    return ServiceRegistry(
        scan_session_service=ScanSessionService(scan_repo),
        source_service=SourceService(source_repo),
        audit_service=AuditSamplingService(library_db_path),
        ops_event_service=OpsEventService(library_db_path),
    )
