from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_TOKEN_SANITIZE_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")
_APP_LOGGER_CACHE: dict[str, logging.Logger] = {}

APP_LOG_FILE_NAME = "app.log"
APP_LOG_MAX_BYTES = 20 * 1024 * 1024
APP_LOG_BACKUP_COUNT = 10


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dumps_json_line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def sanitize_log_token(value: str | None, *, fallback: str = "unknown") -> str:
    raw = (value or "").strip()
    if not raw:
        return fallback
    cleaned = _TOKEN_SANITIZE_PATTERN.sub("_", raw).strip("._-")
    return cleaned or fallback


def build_run_log_path(runs_dir: Path, *, run_kind: str | None, run_id: str | None) -> Path:
    safe_kind = sanitize_log_token(run_kind, fallback="unknown")
    safe_run_id = sanitize_log_token(run_id, fallback="unknown")
    return runs_dir / f"{safe_kind}-{safe_run_id}.jsonl"


def parse_run_log_name(path: Path) -> tuple[str | None, str | None]:
    stem = path.stem
    if "-" not in stem:
        return None, None
    run_kind, run_id = stem.split("-", 1)
    return run_kind or None, run_id or None


def get_app_logger(logs_dir: Path) -> logging.Logger:
    logs_dir.mkdir(parents=True, exist_ok=True)
    key = str((logs_dir / APP_LOG_FILE_NAME).resolve())
    cached = _APP_LOGGER_CACHE.get(key)
    if cached is not None:
        return cached

    logger_name = f"hikbox_pictures.app_log.{sanitize_log_token(key, fallback='default')}"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()

    handler = RotatingFileHandler(
        logs_dir / APP_LOG_FILE_NAME,
        maxBytes=APP_LOG_MAX_BYTES,
        backupCount=APP_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)

    _APP_LOGGER_CACHE[key] = logger
    return logger
