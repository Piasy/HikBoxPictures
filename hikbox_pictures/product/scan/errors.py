from __future__ import annotations


class ScanServiceError(RuntimeError):
    """扫描服务基础异常。"""


class ScanActiveConflictError(ScanServiceError):
    """存在 active 扫描会话，拒绝启动新扫描。"""

    def __init__(self, active_session_id: int | None) -> None:
        self.active_session_id = active_session_id
        super().__init__(f"存在进行中的扫描会话: session_id={active_session_id}")


class ServeBlockedByActiveScanError(ScanServiceError):
    """serve 启动前发现 active 扫描会话，需阻断。"""

    def __init__(self, active_session_id: int) -> None:
        self.active_session_id = active_session_id
        super().__init__(f"扫描会话仍在进行中，禁止启动服务: session_id={active_session_id}")


class ScanSessionNotFoundError(ScanServiceError):
    """扫描会话不存在。"""

    def __init__(self, session_id: int) -> None:
        self.session_id = session_id
        super().__init__(f"扫描会话不存在: session_id={session_id}")


class ScanSessionIllegalStatusError(ScanServiceError):
    """扫描会话状态不允许执行当前操作。"""

    def __init__(self, session_id: int, status: str) -> None:
        self.session_id = session_id
        self.status = status
        super().__init__(f"当前状态不允许中止: session_id={session_id}, status={status}")
