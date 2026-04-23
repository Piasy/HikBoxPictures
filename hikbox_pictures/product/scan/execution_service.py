"""扫描执行服务（discover -> metadata -> detect -> assignment）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path

from hikbox_pictures.product.scan.assignment_stage import AssignmentAbortedError, AssignmentStageService
from hikbox_pictures.product.scan.detect_stage import DetectStageRepository
from hikbox_pictures.product.scan.detect_worker import run_detect_worker
from hikbox_pictures.product.scan.discover_stage import DiscoverStageService
from hikbox_pictures.product.scan.metadata_stage import MetadataStageService
from hikbox_pictures.product.scan.session_service import ScanSessionRepository


@dataclass(frozen=True)
class ScanRuntimeDefaults:
    det_size: int
    batch_size: int
    workers: int
    preview_max_side: int


def build_scan_runtime_defaults(*, cpu_count: int | None = None) -> ScanRuntimeDefaults:
    safe_cpu_count = max(1, int(cpu_count or os.cpu_count() or 1))
    workers = max(1, min(4, safe_cpu_count))
    return ScanRuntimeDefaults(
        det_size=640,
        batch_size=300,
        workers=workers,
        preview_max_side=1280,
    )


def split_batch(*, total: int, workers: int) -> list[int]:
    safe_workers = max(1, int(workers))
    safe_total = max(0, int(total))
    base = safe_total // safe_workers
    remainder = safe_total % safe_workers
    result = [base for _ in range(safe_workers)]
    for idx in range(remainder):
        result[idx] += 1
    return result


@dataclass(frozen=True)
class DetectStageRunResult:
    claimed_batches: int
    acked_batches: int
    interrupted: bool


@dataclass(frozen=True)
class ScanSessionRunResult:
    scan_session_id: int
    detect_result: DetectStageRunResult
    assignment_run_id: int
    new_face_count: int | None = None
    anchor_candidate_face_count: int | None = None
    anchor_attached_face_count: int | None = None
    anchor_missed_face_count: int | None = None
    anchor_missed_by_person: dict[int, int] | None = None
    local_rebuild_count: int | None = None
    fallback_reason: str | None = None


class ScanExecutionService:
    """执行扫描主链路并落地冻结 assignment。"""

    def __init__(self, *, db_path: Path, output_root: Path):
        self._db_path = Path(db_path)
        self._output_root = Path(output_root)
        self._detect_repo = DetectStageRepository(self._db_path)
        self._session_repo = ScanSessionRepository(self._db_path)
        self._discover_service = DiscoverStageService(self._db_path)
        self._metadata_service = MetadataStageService(self._db_path)

    def run_detect_stage(
        self,
        *,
        scan_session_id: int,
        runtime_defaults: ScanRuntimeDefaults | None = None,
        detector=None,
    ) -> DetectStageRunResult:
        defaults = runtime_defaults or build_scan_runtime_defaults()
        session = self._session_repo.get_session(scan_session_id)
        if session.status == "aborting":
            self._detect_repo.rollback_unacked_batches(scan_session_id=scan_session_id, reason="session aborting")
            self._session_repo.update_status(scan_session_id, status="interrupted")
            return DetectStageRunResult(claimed_batches=0, acked_batches=0, interrupted=True)

        claimed_batches = 0
        acked_batches = 0
        while True:
            self._detect_repo.prepare_detect_batches(
                scan_session_id=scan_session_id,
                batch_size=defaults.batch_size,
                workers=defaults.workers,
            )

            round_claimed = 0
            for worker_slot in range(defaults.workers):
                while True:
                    claimed = self._detect_repo.claim_detect_batch(
                        scan_session_id=scan_session_id,
                        worker_slot=worker_slot,
                    )
                    if claimed is None:
                        break
                    claimed_batches += 1
                    round_claimed += 1

                    worker_items: list[dict[str, object]] = []
                    for item in claimed.items:
                        source_root = Path(str(item["source_root"]))
                        primary_path = str(item["primary_path"])
                        photo_asset_id = int(item["photo_asset_id"])
                        worker_items.append(
                            {
                                "photo_asset_id": photo_asset_id,
                                "image_path": str(source_root / primary_path),
                                "photo_key": f"a{photo_asset_id}",
                            }
                        )

                    request = {
                        "items": worker_items,
                        "output_root": str(self._output_root),
                        "det_size": defaults.det_size,
                        "preview_max_side": defaults.preview_max_side,
                    }
                    try:
                        if detector is None:
                            payload = _run_detect_worker_subprocess(request, workdir=self._output_root)
                        else:
                            payload = run_detect_worker(request, detector=detector)

                        self._detect_repo.ack_detect_batch(
                            batch_id=claimed.batch_id,
                            claim_token=claimed.claim_token,
                            worker_payload=payload,
                        )
                        acked_batches += 1
                    except Exception as exc:
                        error_message = f"detect worker/ack 失败: {exc}"
                        self._detect_repo.rollback_unacked_batches(
                            scan_session_id=scan_session_id,
                            reason=error_message,
                            item_status="failed",
                        )
                        self._session_repo.update_status(
                            scan_session_id,
                            status="failed",
                            finished_at=datetime.now().isoformat(timespec="seconds"),
                            last_error=error_message,
                        )
                        raise

                    latest = self._session_repo.get_session(scan_session_id)
                    if latest.status == "aborting":
                        self._detect_repo.rollback_unacked_batches(scan_session_id=scan_session_id, reason="session aborting")
                        self._session_repo.update_status(scan_session_id, status="interrupted")
                        return DetectStageRunResult(
                            claimed_batches=claimed_batches,
                            acked_batches=acked_batches,
                            interrupted=True,
                        )

            has_remaining = self._detect_repo.has_remaining_detect_work(scan_session_id=scan_session_id)
            if not has_remaining:
                self._detect_repo.mark_detect_stage_done(scan_session_id=scan_session_id)
                break
            if round_claimed == 0:
                # 防止异常状态下空转；保留现场供上层重试。
                break

        return DetectStageRunResult(claimed_batches=claimed_batches, acked_batches=acked_batches, interrupted=False)

    def run_session(
        self,
        *,
        scan_session_id: int,
        runtime_defaults: ScanRuntimeDefaults | None = None,
        detector=None,
        embedding_calculator=None,
    ) -> ScanSessionRunResult:
        session = self._session_repo.get_session(scan_session_id)
        if session.status not in {"running", "aborting"}:
            raise ValueError(f"scan_session 状态不允许执行主链路: {session.status}")

        effective_defaults = runtime_defaults or build_scan_runtime_defaults()
        effective_defaults = ScanRuntimeDefaults(
            det_size=effective_defaults.det_size,
            batch_size=effective_defaults.batch_size,
            workers=effective_defaults.workers,
            preview_max_side=480,
        )

        detect_result = DetectStageRunResult(claimed_batches=0, acked_batches=0, interrupted=False)
        try:
            self._discover_service.run(scan_session_id=scan_session_id)
            self._metadata_service.run(scan_session_id=scan_session_id)
            detect_result = self.run_detect_stage(
                scan_session_id=scan_session_id,
                runtime_defaults=effective_defaults,
                detector=detector,
            )
            if detect_result.interrupted:
                return ScanSessionRunResult(
                    scan_session_id=scan_session_id,
                    detect_result=detect_result,
                    assignment_run_id=0,
                )
            latest = self._session_repo.get_session(scan_session_id)
            if latest.status == "aborting":
                self._session_repo.update_status(
                    scan_session_id,
                    status="interrupted",
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                )
                return ScanSessionRunResult(
                    scan_session_id=scan_session_id,
                    detect_result=detect_result,
                    assignment_run_id=0,
                )

            assignment_service = AssignmentStageService(
                library_db_path=self._db_path,
                embedding_db_path=self._db_path.parent / "embedding.db",
                output_root=self._output_root,
            )
            assignment_result = assignment_service.run_frozen_v5_assignment(
                scan_session_id=scan_session_id,
                run_kind=session.run_kind,
                embedding_calculator=embedding_calculator,
            )
            self._session_repo.update_status(
                scan_session_id,
                status="completed",
                finished_at=datetime.now().isoformat(timespec="seconds"),
            )
            return ScanSessionRunResult(
                scan_session_id=scan_session_id,
                detect_result=detect_result,
                assignment_run_id=assignment_result.assignment_run_id,
                new_face_count=assignment_result.new_face_count,
                anchor_candidate_face_count=assignment_result.anchor_candidate_face_count,
                anchor_attached_face_count=assignment_result.anchor_attached_face_count,
                anchor_missed_face_count=assignment_result.anchor_missed_face_count,
                anchor_missed_by_person=assignment_result.anchor_missed_by_person,
                local_rebuild_count=assignment_result.local_rebuild_count,
                fallback_reason=assignment_result.fallback_reason,
            )
        except AssignmentAbortedError:
            self._session_repo.update_status(
                scan_session_id,
                status="interrupted",
                finished_at=datetime.now().isoformat(timespec="seconds"),
                last_error="assignment aborted by user",
            )
            return ScanSessionRunResult(
                scan_session_id=scan_session_id,
                detect_result=detect_result,
                assignment_run_id=0,
            )
        except Exception as exc:
            self._session_repo.update_status(
                scan_session_id,
                status="failed",
                finished_at=datetime.now().isoformat(timespec="seconds"),
                last_error=f"scan session failed: {exc}",
            )
            raise

    def detect_stage_progress(self, *, scan_session_id: int) -> dict[str, object]:
        conn = self._detect_repo.connect()
        try:
            row = conn.execute(
                """
                SELECT
                  SUM(CASE WHEN status='acked' THEN 1 ELSE 0 END) AS acked_batches,
                  SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running_batches,
                  SUM(CASE WHEN status='claimed' THEN 1 ELSE 0 END) AS claimed_batches,
                  SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_batches
                FROM scan_batch
                WHERE scan_session_id=? AND stage='detect'
                """,
                (scan_session_id,),
            ).fetchone()
            session_sources = conn.execute(
                """
                SELECT library_source_id, stage_status_json
                FROM scan_session_source
                WHERE scan_session_id=?
                ORDER BY library_source_id ASC
                """,
                (scan_session_id,),
            ).fetchall()
        finally:
            conn.close()

        stage_status = {}
        for source_row in session_sources:
            status_map = json.loads(str(source_row[1]))
            stage_status[int(source_row[0])] = status_map.get("detect", "pending")

        return {
            "acked_batches": int(row[0] or 0),
            "running_batches": int(row[1] or 0),
            "claimed_batches": int(row[2] or 0),
            "failed_batches": int(row[3] or 0),
            "source_detect_status": stage_status,
        }


def _run_detect_worker_subprocess(request: dict[str, object], *, workdir: Path) -> dict[str, object]:
    workdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="detect-ipc-", dir=str(workdir)) as ipc_dir:
        ipc_root = Path(ipc_dir)
        request_json = ipc_root / "request.json"
        response_json = ipc_root / "response.json"
        request_json.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "hikbox_pictures.product.scan.detect_worker",
                "--request-json",
                str(request_json),
                "--response-json",
                str(response_json),
            ],
            check=True,
        )
        return json.loads(response_json.read_text(encoding="utf-8"))
