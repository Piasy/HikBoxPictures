from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path

from hikbox_pictures.product.sources import WorkspaceContext


class WorkspaceOperationLockError(RuntimeError):
    """工作区运行锁获取失败。"""


@contextmanager
def acquire_workspace_operation_lock(
    *,
    workspace_context: WorkspaceContext,
    operation_name: str,
) -> Iterator[None]:
    lock_path = _workspace_operation_lock_path(workspace_context)
    lock_path.touch(exist_ok=True)
    with lock_path.open("r+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkspaceOperationLockError(
                _build_conflict_message(
                    lock_path=lock_path,
                    requested_operation=operation_name,
                )
            ) from exc
        _write_lock_metadata(handle, operation_name=operation_name)
        try:
            yield
        finally:
            handle.seek(0)
            handle.truncate()
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _workspace_operation_lock_path(workspace_context: WorkspaceContext) -> Path:
    return workspace_context.workspace_path / ".hikbox" / "operation.lock"


def _write_lock_metadata(handle: object, *, operation_name: str) -> None:
    payload = {
        "operation": operation_name,
        "pid": os.getpid(),
    }
    handle.seek(0)
    handle.truncate()
    json.dump(payload, handle, ensure_ascii=False)
    handle.flush()


def _build_conflict_message(*, lock_path: Path, requested_operation: str) -> str:
    active_operation = _read_active_operation(lock_path)
    if active_operation == "scan" and requested_operation == "serve":
        return "当前工作区存在运行中的扫描任务，scan 与 serve 互斥，不能启动 WebUI。"
    if active_operation == "serve" and requested_operation == "scan":
        return "当前工作区存在运行中的 WebUI serve，scan 与 serve 互斥，不能启动扫描。"
    if active_operation == "scan":
        return "当前工作区存在运行中的扫描任务，不能并发执行新的 scan。"
    if active_operation == "serve":
        return "当前工作区存在运行中的 WebUI serve，不能重复启动 serve。"
    return "当前工作区已有运行中的操作，scan 与 serve 互斥。"


def _read_active_operation(lock_path: Path) -> str | None:
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    operation = payload.get("operation")
    if not isinstance(operation, str) or not operation.strip():
        return None
    return operation
