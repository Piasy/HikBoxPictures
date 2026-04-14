from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import json

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.logging_config import (
    build_run_log_path,
    dumps_json_line,
    get_app_logger,
    now_utc_iso,
    parse_run_log_name,
    sanitize_log_token,
)
from hikbox_pictures.repositories import OpsEventRepo
from hikbox_pictures.workspace import load_workspace_paths, load_workspace_paths_from_db_path


class ObservabilityService:
    def __init__(self, conn: sqlite3.Connection, *, workspace: Path | None = None) -> None:
        self.conn = conn
        self.ops_event_repo = OpsEventRepo(conn)
        self.logs_dir = self._resolve_logs_dir(workspace)
        self.runs_dir = self.logs_dir / "runs" if self.logs_dir is not None else None
        self.app_logger = get_app_logger(self.logs_dir) if self.logs_dir is not None else None

    def emit_event(
        self,
        *,
        level: str,
        component: str,
        event_type: str,
        message: str | None = None,
        detail: dict[str, Any] | None = None,
        run_kind: str | None = None,
        run_id: str | None = None,
    ) -> int | None:
        detail_json = self._safe_detail_json(detail)
        event_id: int | None = None
        started_without_tx = not bool(getattr(self.conn, "in_transaction", False))

        try:
            event_id = self.ops_event_repo.append_event(
                level=level,
                component=component,
                event_type=event_type,
                message=message,
                detail_json=detail_json,
                run_kind=run_kind,
                run_id=run_id,
            )
            if started_without_tx:
                self.conn.commit()
        except Exception:
            if started_without_tx and bool(getattr(self.conn, "in_transaction", False)):
                try:
                    self.conn.rollback()
                except Exception:
                    pass
            event_id = None

        structured_payload = self._build_structured_payload(
            level=level,
            component=component,
            event_type=event_type,
            message=message,
            detail=detail,
            run_kind=run_kind,
            run_id=run_id,
            event_id=event_id,
        )

        try:
            self._append_run_log(structured_payload)
        except Exception:
            pass

        try:
            self._append_app_log(structured_payload)
        except Exception:
            pass

        return event_id

    def list_events(
        self,
        *,
        limit: int = 50,
        run_kind: str | None = None,
        event_type: str | None = None,
        run_id: str | None = None,
        level: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.ops_event_repo.list_recent(
            limit=limit,
            run_kind=run_kind,
            event_type=event_type,
            run_id=run_id,
            level=level,
        )

    def prune_ops_events(self, *, days: int, batch_size: int = 5000) -> int:
        deleted = self.ops_event_repo.prune_older_than_days(days=days, batch_size=batch_size)
        self._prune_run_logs(days=days, keep_latest_runs=200)
        self.conn.commit()
        return deleted

    def tail_run_logs(
        self,
        *,
        run_kind: str | None = None,
        run_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 1000))
        if self.runs_dir is None or not self.runs_dir.exists():
            return []

        rows: deque[dict[str, Any]] = deque(maxlen=safe_limit)
        for path in self._select_log_files(run_kind=run_kind, run_id=run_id):
            default_run_kind, default_run_id = self._resolve_default_run_from_path(path)
            with path.open("r", encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue
                    parsed = self._parse_log_line(
                        line,
                        default_run_kind=default_run_kind,
                        default_run_id=default_run_id,
                    )
                    if run_kind is not None and parsed.get("run_kind") != run_kind:
                        continue
                    if run_id is not None and parsed.get("run_id") != run_id:
                        continue
                    rows.append(parsed)
        return list(rows)

    def _resolve_logs_dir(self, workspace: Path | None) -> Path | None:
        if workspace is not None:
            logs_dir = load_workspace_paths(Path(workspace)).logs_dir
            logs_dir.mkdir(parents=True, exist_ok=True)
            return logs_dir

        row = self.conn.execute("PRAGMA database_list").fetchone()
        if row is None:
            return None

        db_file = row["file"] if isinstance(row, sqlite3.Row) else row[2]
        if not db_file:
            return None
        logs_dir = load_workspace_paths_from_db_path(Path(str(db_file))).logs_dir
        logs_dir.mkdir(parents=True, exist_ok=True)
        return logs_dir

    def _safe_detail_json(self, detail: dict[str, Any] | None) -> str | None:
        if detail is None:
            return None
        try:
            return dumps_json_line(detail)
        except Exception:
            return dumps_json_line({"raw_detail": str(detail)})

    def _append_run_log(self, payload: dict[str, Any]) -> None:
        if self.runs_dir is None:
            return
        run_kind = payload.get("run_kind")
        run_id = payload.get("run_id")
        if run_kind is None or run_id is None:
            return

        path = build_run_log_path(self.runs_dir, run_kind=run_kind, run_id=run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(dumps_json_line(payload))
            handle.write("\n")

    def _append_app_log(self, payload: dict[str, Any]) -> None:
        if self.app_logger is None:
            return
        self.app_logger.info(dumps_json_line(payload))

    def _select_log_files(self, *, run_kind: str | None, run_id: str | None) -> list[Path]:
        if self.runs_dir is None:
            return []

        if run_kind is not None and run_id is not None:
            candidates = [
                build_run_log_path(self.runs_dir, run_kind=run_kind, run_id=run_id),
                self.runs_dir / sanitize_log_token(run_kind) / f"{sanitize_log_token(run_id)}.log",
            ]
            return [path for path in candidates if path.exists() and path.is_file()]

        if run_kind is not None:
            safe_kind = sanitize_log_token(run_kind)
            jsonl_paths = self.runs_dir.glob(f"{safe_kind}-*.jsonl")
            legacy_paths = (self.runs_dir / safe_kind).glob("*.log")
            return sorted([path for path in [*jsonl_paths, *legacy_paths] if path.is_file()])

        if run_id is not None:
            safe_run_id = sanitize_log_token(run_id)
            jsonl_paths = self.runs_dir.glob(f"*-{safe_run_id}.jsonl")
            legacy_paths = self.runs_dir.glob(f"*/{safe_run_id}.log")
            return sorted([path for path in [*jsonl_paths, *legacy_paths] if path.is_file()])

        return sorted(
            [
                path
                for path in [*self.runs_dir.glob("*.jsonl"), *self.runs_dir.glob("*/*.log")]
                if path.is_file()
            ]
        )

    def _resolve_default_run_from_path(self, path: Path) -> tuple[str, str]:
        if path.suffix == ".jsonl":
            run_kind, run_id = parse_run_log_name(path)
            return run_kind or "unknown", run_id or "unknown"
        return path.parent.name, path.stem

    def _parse_log_line(
        self,
        line: str,
        *,
        default_run_kind: str,
        default_run_id: str,
    ) -> dict[str, Any]:
        try:
            payload = json.loads(line)
            if isinstance(payload, dict):
                payload.setdefault("run_kind", payload.get("run_kind") or default_run_kind)
                payload.setdefault("run_id", payload.get("run_id") or default_run_id)
                return payload
        except Exception:
            pass
        return {
            "ts": now_utc_iso(),
            "level": "info",
            "component": "run_log",
            "event_type": "log.run.raw_line",
            "run_kind": default_run_kind,
            "run_id": default_run_id,
            "message": line,
            "phase": None,
            "status": None,
            "duration_ms": None,
            "error_code": None,
            "error_type": None,
            "error_message": None,
            "error_stack": None,
        }

    def _build_structured_payload(
        self,
        *,
        level: str,
        component: str,
        event_type: str,
        message: str | None,
        detail: dict[str, Any] | None,
        run_kind: str | None,
        run_id: str | None,
        event_id: int | None,
    ) -> dict[str, Any]:
        detail_map = detail if isinstance(detail, dict) else {}
        payload: dict[str, Any] = {
            "ts": now_utc_iso(),
            "level": level,
            "event_type": event_type,
            "component": component,
            "run_kind": run_kind,
            "run_id": run_id,
            "event_id": event_id,
            "message": message,
            "detail": detail if detail is not None else None,
            "phase": detail_map.get("phase"),
            "status": detail_map.get("status"),
            "duration_ms": detail_map.get("duration_ms"),
            "error_code": detail_map.get("error_code"),
            "error_type": detail_map.get("error_type"),
            "error_message": detail_map.get("error_message"),
            "error_stack": detail_map.get("error_stack"),
        }
        return payload

    def _prune_run_logs(self, *, days: int, keep_latest_runs: int) -> int:
        if self.runs_dir is None or not self.runs_dir.exists():
            return 0

        safe_days = max(1, int(days))
        safe_keep_latest = max(1, int(keep_latest_runs))
        cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=safe_days)).timestamp()

        candidates = [path for path in [*self.runs_dir.glob("*.jsonl"), *self.runs_dir.glob("*/*.log")] if path.is_file()]
        if not candidates:
            return 0

        sorted_candidates = sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)
        keep_latest = {path.resolve() for path in sorted_candidates[:safe_keep_latest]}

        deleted = 0
        for path in sorted_candidates:
            resolved = path.resolve()
            mtime = path.stat().st_mtime
            if resolved in keep_latest or mtime >= cutoff_ts:
                continue
            try:
                path.unlink()
                deleted += 1
            except FileNotFoundError:
                continue
        return deleted
