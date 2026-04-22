from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from .run_service import ExportRunService


def launch_export_runner_thread(*, library_db_path: Path, run_id: int, template_id: int) -> None:
    """在当前进程中异步执行导出运行，避免阻塞 Web 请求线程。"""

    def _worker() -> None:
        delay = _env_float("HIKBOX_EXPORT_WEB_RUNNER_DELAY", default=2.0)
        if delay > 0:
            time.sleep(delay)
        service = ExportRunService(library_db_path)
        service.execute_existing_run(run_id=int(run_id), template_id=int(template_id))

    thread = threading.Thread(
        target=_worker,
        name=f"hikbox-export-run-{int(run_id)}",
        daemon=True,
    )
    thread.start()


def _env_float(name: str, *, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value < 0:
        return default
    return value
