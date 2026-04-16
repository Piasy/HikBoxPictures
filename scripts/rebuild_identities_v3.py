from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import threading
import time

from hikbox_pictures.services.identity_rebuild_service import IdentityRebuildService

_PROGRESS_HEARTBEAT_SECONDS = 10.0


class _ProgressHeartbeatPrinter:
    def __init__(self, *, interval_seconds: float, stream=None) -> None:
        self._interval_seconds = float(interval_seconds)
        self._stream = stream if stream is not None else sys.stderr
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        now = time.monotonic()
        self._started_at = now
        self._phase_started_at = now
        self._payload: dict[str, object] = {
            "phase": "initialize",
            "status": "running",
        }

    def start(self) -> None:
        if self._interval_seconds <= 0:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="identity-v3-progress-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self._interval_seconds * 2))

    def update(self, payload: dict[str, object]) -> None:
        now = time.monotonic()
        with self._lock:
            phase = payload.get("phase")
            previous_phase_text = str(self._payload.get("phase") or "")
            next_phase_text = str(phase) if phase is not None else previous_phase_text
            current_subphase = self._payload.get("subphase")
            next_subphase = payload.get("subphase", current_subphase)
            phase_changed = next_phase_text != previous_phase_text
            should_reset = (
                phase_changed
                or next_subphase != current_subphase
            )
            if should_reset:
                status_value = str(payload.get("status") or self._payload.get("status") or "running")
                self._payload = {
                    "phase": next_phase_text,
                    "status": status_value,
                }
            if phase is not None:
                phase_text = str(phase)
                if phase_changed:
                    self._phase_started_at = now
                self._payload["phase"] = phase_text
            status = payload.get("status")
            if status is not None:
                self._payload["status"] = str(status)
            for key, value in payload.items():
                if key in {"phase", "status"} or value is None:
                    continue
                self._payload[str(key)] = value

    def _snapshot(self) -> dict[str, object]:
        now = time.monotonic()
        with self._lock:
            payload = dict(self._payload)
            phase_started_at = float(self._phase_started_at)
        payload["elapsed_seconds"] = round(now - self._started_at, 1)
        payload["phase_elapsed_seconds"] = round(now - phase_started_at, 1)
        return payload

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            print(
                "identity v3 进度: " + json.dumps(self._snapshot(), ensure_ascii=False),
                file=self._stream,
                flush=True,
            )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="执行 identity v3 全量重建")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--backup-db", action="store_true")
    parser.add_argument("--skip-ann-rebuild", action="store_true")
    parser.add_argument("--threshold-profile", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    service: IdentityRebuildService | None = None
    progress_printer = _ProgressHeartbeatPrinter(interval_seconds=_PROGRESS_HEARTBEAT_SECONDS)
    progress_printer.start()
    try:
        service = IdentityRebuildService(Path(args.workspace))
        summary = service.run_rebuild(
            dry_run=bool(args.dry_run),
            backup_db=bool(args.backup_db),
            skip_ann_rebuild=bool(args.skip_ann_rebuild),
            threshold_profile_path=Path(args.threshold_profile) if args.threshold_profile is not None else None,
            progress_reporter=progress_printer.update,
        )
    except Exception as exc:
        print(f"identity v3 重建失败: {exc}", file=sys.stderr)
        return 1
    finally:
        progress_printer.stop()
        if service is not None:
            service.close()

    mode = "dry-run" if args.dry_run else "execute"
    print(
        "identity v3 重建完成: "
        + json.dumps(
            {
                "mode": mode,
                "threshold_profile_id": summary.get("threshold_profile_id"),
                "profile_mode": summary.get("profile_mode"),
                "materialized_cluster_count": summary.get("materialized_cluster_count"),
                "review_pending_cluster_count": summary.get("review_pending_cluster_count"),
                "discarded_cluster_count": summary.get("discarded_cluster_count"),
                "summary_path": str(
                    Path(args.workspace).expanduser().resolve()
                    / ".tmp"
                    / "rebuild-identities-v3"
                    / "last-summary.json"
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
