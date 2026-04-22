"""扫描会话状态机能力。"""

from hikbox_pictures.product.scan.errors import (
    ScanActiveConflictError,
    ServeBlockedByActiveScanError,
)
from hikbox_pictures.product.scan.models import ACTIVE_STATUS, ALLOWED_RUN_KIND, ScanSessionRecord, ScanStartResult
from hikbox_pictures.product.scan.session_service import (
    ScanSessionRepository,
    ScanSessionService,
    assert_no_active_scan_for_serve,
)

__all__ = [
    "ACTIVE_STATUS",
    "ALLOWED_RUN_KIND",
    "ScanActiveConflictError",
    "ScanSessionRecord",
    "ScanSessionRepository",
    "ScanSessionService",
    "ScanStartResult",
    "ServeBlockedByActiveScanError",
    "assert_no_active_scan_for_serve",
]
