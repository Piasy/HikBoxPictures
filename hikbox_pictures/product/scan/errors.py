"""扫描状态机错误定义。"""

from __future__ import annotations


class ScanError(Exception):
    """扫描域基础异常。"""


class ScanActiveConflictError(ScanError):
    """存在活跃扫描会话，无法启动新会话。"""

    def __init__(self, active_session_id: int):
        super().__init__(f"存在活跃扫描会话，session_id={active_session_id}")
        self.active_session_id = active_session_id


class InvalidRunKindError(ScanError):
    """run_kind 非法。"""


class InvalidTriggeredByError(ScanError):
    """triggered_by 非法。"""


class SessionNotFoundError(ScanError):
    """会话不存在。"""

    def __init__(self, session_id: int):
        super().__init__(f"扫描会话不存在，session_id={session_id}")
        self.session_id = session_id


class ServeBlockedByActiveScanError(ScanError):
    """存在活跃扫描会话，阻断 serve 启动。"""

    def __init__(self, active_session_id: int):
        super().__init__(f"存在活跃扫描会话，禁止启动 serve，session_id={active_session_id}")
        self.active_session_id = active_session_id
