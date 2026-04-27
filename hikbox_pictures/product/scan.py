from __future__ import annotations

from collections.abc import Callable, Sequence
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading

import numpy as np

from hikbox_pictures.product.online_assignment import ExistingAssetFace
from hikbox_pictures.product.online_assignment import OnlineAssignmentError
from hikbox_pictures.product.online_assignment import RedetectFace
from hikbox_pictures.product.online_assignment import reconcile_asset_redetection
from hikbox_pictures.product.online_assignment import run_online_assignment
from hikbox_pictures.product.scan_shared import SUPPORTED_SCAN_SUFFIXES
from hikbox_pictures.product.scan_shared import compute_capture_month
from hikbox_pictures.product.scan_shared import compute_file_fingerprint
from hikbox_pictures.product.scan_shared import find_live_photo_mov
from hikbox_pictures.product.scan_shared import utc_now_text
from hikbox_pictures.product.sources import WorkspaceContext
from hikbox_pictures.product.sources import WorkspaceAccessError
from hikbox_pictures.product.sources import load_workspace_context
from hikbox_pictures.product.workspace_runtime import acquire_workspace_operation_lock
from hikbox_pictures.product.workspace_runtime import WorkspaceOperationLockError


class ScanStartError(RuntimeError):
    """scan start 执行失败。"""


REQUIRED_LIBRARY_SCAN_TABLES = (
    "assets",
    "scan_sessions",
    "scan_batches",
    "scan_batch_items",
    "face_observations",
    "person",
    "assignment_runs",
    "person_face_assignments",
    "person_face_exclusions",
)
REQUIRED_EMBEDDING_SCAN_TABLES = ("face_embeddings",)
_SCAN_PROGRESS_INTERVAL_SECONDS = 10.0


def start_scan(
    *,
    workspace: Path,
    batch_size: int,
    command_args: list[str],
) -> None:
    workspace_context: WorkspaceContext | None = None
    session_id: int | None = None
    command = " ".join(["hikbox-pictures", *command_args])
    try:
        workspace_context = load_workspace_context(workspace)
        with acquire_workspace_operation_lock(
            workspace_context=workspace_context,
            operation_name="scan",
        ):
            _ensure_scan_schema_ready(workspace_context)
            _reconcile_completed_running_sessions(workspace_context)
            active_sources = _load_active_sources(workspace_context)
            resumable_session = _load_resumable_session(workspace_context)
            effective_batch_size = batch_size
            if resumable_session is not None:
                session = resumable_session
                total_batches = int(session["total_batches"])
                plan_fingerprint = str(session["plan_fingerprint"])
                effective_batch_size = int(session["batch_size"])
            else:
                candidates = _discover_candidates(active_sources)
                if not candidates:
                    raise ScanStartError("没有可扫描照片。")

                total_batches = (len(candidates) + batch_size - 1) // batch_size
                plan_fingerprint = _compute_plan_fingerprint(candidates=candidates, batch_size=batch_size)
                session = _ensure_scan_session(
                    workspace_context=workspace_context,
                    candidates=candidates,
                    batch_size=batch_size,
                    total_batches=total_batches,
                    plan_fingerprint=plan_fingerprint,
                    command=command,
                )
            session_id = int(session["id"])
            progress_state = _load_scan_progress_state(
                workspace_context=workspace_context,
                session_id=session_id,
            )
            _append_scan_log(
                workspace_context=workspace_context,
                payload={
                    "timestamp": utc_now_text(),
                    "event": "scan_started",
                    "session_id": session_id,
                    "plan_fingerprint": plan_fingerprint,
                    "batch_size": effective_batch_size,
                    "total_batches": total_batches,
                    "model_root": str(workspace_context.model_root_path),
                    "command": command,
                },
            )
            pending_batches = _load_pending_batches(workspace_context, session_id=session_id)
            if not pending_batches:
                _refresh_session_summary(
                    workspace_context=workspace_context,
                    session_id=session_id,
                    final_status="running",
                )
                _run_assignment_stage(
                    workspace_context=workspace_context,
                    session_id=session_id,
                    progress_state=progress_state,
                )
                summary = _refresh_session_summary(
                    workspace_context=workspace_context,
                    session_id=session_id,
                    final_status="completed",
                )
                _append_scan_log(
                    workspace_context=workspace_context,
                    payload={
                        "timestamp": utc_now_text(),
                        "event": "scan_skipped",
                        "session_id": session_id,
                        "reason": "无新增待处理批次",
                        "completed_batches": summary["completed_batches"],
                        "total_batches": summary["total_batches"],
                        "failed_assets": summary["failed_assets"],
                        "success_faces": summary["success_faces"],
                        "artifact_files": summary["artifact_files"],
                    },
                )
                return

            for batch in pending_batches:
                _run_batch(
                    workspace_context=workspace_context,
                    batch=batch,
                    session_id=session_id,
                    progress_state=progress_state,
                )

            _run_assignment_stage(
                workspace_context=workspace_context,
                session_id=session_id,
                progress_state=progress_state,
            )
            summary = _refresh_session_summary(
                workspace_context=workspace_context,
                session_id=session_id,
                final_status="completed",
            )
            _append_scan_log(
                workspace_context=workspace_context,
                payload={
                    "timestamp": utc_now_text(),
                    "event": "scan_completed",
                    "session_id": session_id,
                    "total_batches": summary["total_batches"],
                    "completed_batches": summary["completed_batches"],
                    "failed_assets": summary["failed_assets"],
                    "success_faces": summary["success_faces"],
                    "artifact_files": summary["artifact_files"],
                },
            )
    except WorkspaceOperationLockError as exc:
        failure = ScanStartError(str(exc))
        _handle_scan_start_failure(
            workspace_context=workspace_context,
            session_id=session_id,
            command=command,
            reason=str(failure),
        )
        raise failure from exc
    except ScanStartError as exc:
        _handle_scan_start_failure(
            workspace_context=workspace_context,
            session_id=session_id,
            command=command,
            reason=str(exc),
        )
        raise
    except OSError as exc:
        failure = ScanStartError(f"本地文件操作失败：{exc}")
        _handle_scan_start_failure(
            workspace_context=workspace_context,
            session_id=session_id,
            command=command,
            reason=str(failure),
        )
        raise failure from exc


def _ensure_scan_schema_ready(workspace_context: WorkspaceContext) -> None:
    missing_tables: list[str] = []
    missing_tables.extend(
        _find_missing_tables(
            db_path=workspace_context.library_db_path,
            required_tables=REQUIRED_LIBRARY_SCAN_TABLES,
        )
    )
    missing_tables.extend(
        _find_missing_tables(
            db_path=workspace_context.embedding_db_path,
            required_tables=REQUIRED_EMBEDDING_SCAN_TABLES,
        )
    )
    if missing_tables:
        table_names = ", ".join(missing_tables)
        raise ScanStartError(
            "当前工作区缺少扫描表："
            f"{table_names}。该工作区只具备 Slice A schema，当前版本不支持自动升级；"
            "请使用当前版本重新执行 hikbox-pictures init 创建新 workspace，再执行 hikbox-pictures source add 和 hikbox-pictures scan start。"
        )


def _find_missing_tables(*, db_path: Path, required_tables: Sequence[str]) -> list[str]:
    connection = sqlite3.connect(db_path)
    try:
        existing_tables = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                """
            ).fetchall()
        }
    except sqlite3.Error as exc:
        raise ScanStartError(f"工作区 schema 检查失败：{db_path}") from exc
    finally:
        connection.close()
    return [table_name for table_name in required_tables if table_name not in existing_tables]


def _handle_scan_start_failure(
    *,
    workspace_context: WorkspaceContext | None,
    session_id: int | None,
    command: str,
    reason: str,
) -> None:
    if workspace_context is None:
        return
    if session_id is not None:
        _best_effort_refresh_session_failed(
            workspace_context=workspace_context,
            session_id=session_id,
        )
    _best_effort_append_scan_log(
        workspace_context=workspace_context,
        payload={
            "timestamp": utc_now_text(),
            "event": "scan_failed",
            "session_id": session_id,
            "reason": reason,
            "command": command,
        },
    )


def _best_effort_refresh_session_failed(
    *,
    workspace_context: WorkspaceContext,
    session_id: int,
) -> None:
    try:
        if _read_scan_status(
            db_path=workspace_context.library_db_path,
            table_name="scan_sessions",
            row_id=session_id,
        ) == "completed":
            return
        _refresh_session_summary(
            workspace_context=workspace_context,
            session_id=session_id,
            final_status="failed",
        )
    except Exception:  # noqa: BLE001
        return


def _best_effort_mark_batch_failed(
    *,
    workspace_context: WorkspaceContext,
    batch_id: int,
    message: str,
) -> None:
    try:
        if _read_scan_status(
            db_path=workspace_context.library_db_path,
            table_name="scan_batches",
            row_id=batch_id,
        ) == "completed":
            return
        _mark_batch_failed(workspace_context, batch_id=batch_id, message=message)
    except Exception:  # noqa: BLE001
        return


def _best_effort_append_scan_log(
    *,
    workspace_context: WorkspaceContext,
    payload: dict[str, object],
) -> None:
    try:
        _append_scan_log(workspace_context=workspace_context, payload=payload)
    except Exception:  # noqa: BLE001
        return


def _read_scan_status(*, db_path: Path, table_name: str, row_id: int) -> str | None:
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            f"SELECT status FROM {table_name} WHERE id = ?",
            (row_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return str(row[0])


def _load_active_sources(workspace_context: WorkspaceContext) -> list[dict[str, object]]:
    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT id, path, label
            FROM library_sources
            WHERE active = 1
            ORDER BY id ASC
            """
        ).fetchall()
    except sqlite3.Error as exc:
        raise ScanStartError("active source 读取失败。") from exc
    finally:
        connection.close()
    if not rows:
        raise ScanStartError("没有可用 active source。")

    sources: list[dict[str, object]] = []
    for row in rows:
        source_path = Path(str(row["path"]))
        if not source_path.exists():
            raise ScanStartError(f"source 路径不存在：{source_path}")
        if not source_path.is_dir():
            raise ScanStartError(f"source 不是目录：{source_path}")
        if not os.access(source_path, os.R_OK | os.X_OK):
            raise ScanStartError(f"source 不可读：{source_path}")
        sources.append(
            {
                "id": int(row["id"]),
                "path": str(source_path.resolve()),
                "label": str(row["label"]),
            }
        )
    return sources


def _discover_candidates(active_sources: list[dict[str, object]]) -> list[dict[str, object]]:
    discovered: list[dict[str, object]] = []
    for source in active_sources:
        source_id = int(source["id"])
        source_path = Path(str(source["path"]))
        for child in sorted(source_path.iterdir(), key=lambda path: (path.name.casefold(), path.name)):
            if not child.is_file():
                continue
            if child.suffix.lower() not in SUPPORTED_SCAN_SUFFIXES:
                continue
            absolute_path = child.resolve()
            discovered.append(
                {
                    "source_id": source_id,
                    "source_path": str(source_path),
                    "absolute_path": str(absolute_path),
                    "file_name": child.name,
                    "file_extension": child.suffix.lower().lstrip("."),
                    "capture_month": compute_capture_month(absolute_path),
                    "file_fingerprint": compute_file_fingerprint(absolute_path),
                    "live_photo_mov_path": find_live_photo_mov(absolute_path),
                }
            )
    return sorted(
        discovered,
        key=lambda item: (str(item["absolute_path"]).casefold(), str(item["absolute_path"])),
    )


def _compute_plan_fingerprint(*, candidates: list[dict[str, object]], batch_size: int) -> str:
    payload = {
        "batch_size": batch_size,
        "candidates": [
            {
                "source_id": item["source_id"],
                "absolute_path": item["absolute_path"],
                "file_fingerprint": item["file_fingerprint"],
                "capture_month": item["capture_month"],
                "live_photo_mov_path": item["live_photo_mov_path"],
            }
            for item in candidates
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ensure_scan_session(
    *,
    workspace_context: WorkspaceContext,
    candidates: list[dict[str, object]],
    batch_size: int,
    total_batches: int,
    plan_fingerprint: str,
    command: str,
) -> dict[str, object]:
    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT id, status, total_batches
            FROM scan_sessions
            WHERE plan_fingerprint = ?
            """,
            (plan_fingerprint,),
        ).fetchone()
        if row is not None:
            with connection:
                connection.execute(
                    """
                    UPDATE scan_sessions
                    SET status = 'running',
                        completed_at = NULL
                    WHERE id = ?
                    """,
                    (int(row["id"]),),
                )
            return {"id": int(row["id"]), "status": "running", "total_batches": int(row["total_batches"])}

        started_at = utc_now_text()
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO scan_sessions (
                  plan_fingerprint,
                  batch_size,
                  status,
                  command,
                  total_batches,
                  started_at
                )
                VALUES (?, ?, 'running', ?, ?, ?)
                """,
                (plan_fingerprint, batch_size, command, total_batches, started_at),
            )
            session_id = int(cursor.lastrowid)
            for batch_index, batch_candidates in enumerate(_chunk_candidates(candidates, batch_size), start=1):
                batch_cursor = connection.execute(
                    """
                    INSERT INTO scan_batches (session_id, batch_index, status, item_count)
                    VALUES (?, ?, 'pending', ?)
                    """,
                    (session_id, batch_index, len(batch_candidates)),
                )
                batch_id = int(batch_cursor.lastrowid)
                for item_index, candidate in enumerate(batch_candidates, start=1):
                    connection.execute(
                        """
                        INSERT INTO scan_batch_items (
                          batch_id,
                          item_index,
                          source_id,
                          absolute_path,
                          status
                        )
                        VALUES (?, ?, ?, ?, 'pending')
                        """,
                        (batch_id, item_index, int(candidate["source_id"]), str(candidate["absolute_path"])),
                    )
        return {"id": session_id, "status": "running", "total_batches": total_batches}
    except sqlite3.Error as exc:
        raise ScanStartError("scan session 初始化失败。") from exc
    finally:
        connection.close()


def _load_pending_batches(workspace_context: WorkspaceContext, *, session_id: int) -> list[dict[str, object]]:
    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT id, batch_index, status, item_count
            FROM scan_batches
            WHERE session_id = ?
              AND status != 'completed'
            ORDER BY batch_index ASC
            """,
            (session_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ScanStartError("scan batch 读取失败。") from exc
    finally:
        connection.close()
    return [dict(row) for row in rows]


def _reconcile_completed_running_sessions(workspace_context: WorkspaceContext) -> None:
    connection = sqlite3.connect(workspace_context.library_db_path)
    try:
        rows = connection.execute(
            """
            SELECT scan_sessions.id
            FROM scan_sessions
            WHERE scan_sessions.status = 'running'
              AND EXISTS (
                SELECT 1
                FROM scan_batches
                WHERE scan_batches.session_id = scan_sessions.id
              )
              AND NOT EXISTS (
                SELECT 1
                FROM scan_batches
                WHERE scan_batches.session_id = scan_sessions.id
                  AND scan_batches.status != 'completed'
              )
            ORDER BY scan_sessions.id ASC
            """
        ).fetchall()
    except sqlite3.Error as exc:
        raise ScanStartError("scan session 终态收敛检查失败。") from exc
    finally:
        connection.close()

    for row in rows:
        _refresh_session_summary(
            workspace_context=workspace_context,
            session_id=int(row[0]),
            final_status="completed",
        )


def _load_resumable_session(workspace_context: WorkspaceContext) -> dict[str, object] | None:
    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT id, plan_fingerprint, batch_size, total_batches
            FROM scan_sessions
            WHERE status = 'running'
              AND EXISTS (
                SELECT 1
                FROM scan_batches
                WHERE scan_batches.session_id = scan_sessions.id
                  AND scan_batches.status != 'completed'
              )
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    except sqlite3.Error as exc:
        raise ScanStartError("scan session 恢复检查失败。") from exc
    finally:
        connection.close()
    if row is None:
        return None
    return dict(row)


def _load_scan_progress_state(
    *,
    workspace_context: WorkspaceContext,
    session_id: int,
) -> dict[str, int]:
    connection = sqlite3.connect(workspace_context.library_db_path)
    try:
        row = connection.execute(
            """
            SELECT
              COUNT(*) AS total_batches,
              COALESCE(SUM(item_count), 0) AS total_items,
              COALESCE(SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END), 0) AS completed_batches,
              COALESCE(SUM(CASE WHEN status = 'completed' THEN item_count ELSE 0 END), 0) AS completed_items
            FROM scan_batches
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise ScanStartError("scan 进度初始化失败。") from exc
    finally:
        connection.close()
    assert row is not None
    return {
        "total_batches": int(row[0]),
        "total_items": int(row[1]),
        "completed_batches": int(row[2]),
        "completed_items": int(row[3]),
    }


def _print_scan_progress(
    *,
    stage: str,
    total_batches: int,
    completed_batches: int,
    total_items: int,
    completed_items: int,
) -> None:
    print(
        f"scan 进度: 阶段={stage}，批次 {completed_batches}/{total_batches}，照片 {completed_items}/{total_items}",
        file=sys.stderr,
        flush=True,
    )


def _report_batch_progress(
    *,
    progress_state: dict[str, int],
    payload: dict[str, object],
) -> None:
    raw_completed_items = payload.get("completed_items")
    raw_total_items = payload.get("total_items")
    if not isinstance(raw_completed_items, int) or not isinstance(raw_total_items, int) or raw_total_items <= 0:
        return
    completed_items = max(0, min(raw_completed_items, raw_total_items))
    _print_scan_progress(
        stage="批处理",
        total_batches=progress_state["total_batches"],
        completed_batches=progress_state["completed_batches"],
        total_items=progress_state["total_items"],
        completed_items=progress_state["completed_items"] + completed_items,
    )


def _run_batch(
    *,
    workspace_context: WorkspaceContext,
    batch: dict[str, object],
    session_id: int,
    progress_state: dict[str, int],
) -> None:
    batch_id = int(batch["id"])
    batch_index = int(batch["batch_index"])
    items = _load_batch_candidates(workspace_context, batch_id=batch_id)
    _mark_batch_running(workspace_context, batch_id=batch_id)
    staging_dir: Path | None = None
    try:
        _append_scan_log(
            workspace_context=workspace_context,
            payload={
                "timestamp": utc_now_text(),
                "event": "batch_started",
                "session_id": session_id,
                "batch_id": batch_id,
                "batch_index": batch_index,
                "item_count": len(items),
                "model_root": str(workspace_context.model_root_path),
            },
        )

        staging_dir = (
            workspace_context.workspace_path
            / ".hikbox"
            / "scan_staging"
            / f"session_{session_id:04d}"
            / f"batch_{batch_index:04d}"
            / f"attempt_{utc_now_text().replace(':', '').replace('-', '')}_{os.getpid()}"
        )
        staging_dir.mkdir(parents=True, exist_ok=True)
        input_path = staging_dir / "input.json"
        output_path = staging_dir / "output.json"
        input_path.write_text(
            json.dumps(
                {
                    "model_root": str(workspace_context.model_root_path),
                    "staging_dir": str(staging_dir),
                    "progress_interval_seconds": _SCAN_PROGRESS_INTERVAL_SECONDS,
                    "items": items,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        worker_result = _run_scan_worker(
            workspace_context=workspace_context,
            input_path=input_path,
            output_path=output_path,
            progress_callback=lambda payload: _report_batch_progress(
                progress_state=progress_state,
                payload=payload,
            ),
        )

        _commit_batch_results(
            workspace_context=workspace_context,
            batch_id=batch_id,
            batch_index=batch_index,
            session_id=session_id,
            candidates=items,
            worker_result=worker_result,
        )
        progress_state["completed_batches"] += 1
        progress_state["completed_items"] += len(items)
    except OSError as exc:
        failure_message = f"本地文件操作失败：{exc}"
        _best_effort_mark_batch_failed(
            workspace_context=workspace_context,
            batch_id=batch_id,
            message=failure_message,
        )
        _best_effort_refresh_session_failed(
            workspace_context=workspace_context,
            session_id=session_id,
        )
        raise ScanStartError(failure_message) from exc
    except ScanStartError as exc:
        _best_effort_mark_batch_failed(
            workspace_context=workspace_context,
            batch_id=batch_id,
            message=str(exc),
        )
        _best_effort_refresh_session_failed(
            workspace_context=workspace_context,
            session_id=session_id,
        )
        raise
    finally:
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)


def _run_scan_worker(
    *,
    workspace_context: WorkspaceContext,
    input_path: Path,
    output_path: Path,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
) -> dict[str, object]:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hikbox_pictures.product.scan_worker",
            "--input-json",
            str(input_path),
            "--output-json",
            str(output_path),
        ],
        cwd=workspace_context.workspace_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
    )
    stderr_lines: list[str] = []

    stdout_thread = threading.Thread(
        target=_consume_scan_worker_stdout,
        args=(process.stdout, progress_callback),
        name="scan-worker-stdout",
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_consume_scan_worker_stderr,
        args=(process.stderr, stderr_lines),
        name="scan-worker-stderr",
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    return_code = process.wait()
    stdout_thread.join()
    stderr_thread.join()

    if return_code != 0:
        raise ScanStartError("".join(stderr_lines).strip() or "scan worker 异常退出")
    try:
        return json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScanStartError("worker 输出无效。") from exc


def _consume_scan_worker_stdout(
    stream,
    progress_callback: Callable[[dict[str, object]], None] | None,
) -> None:
    if stream is None:
        return
    for raw_line in stream:
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(payload, dict)
            and payload.get("event") == "batch_progress"
            and progress_callback is not None
        ):
            progress_callback(payload)


def _consume_scan_worker_stderr(stream, stderr_lines: list[str]) -> None:
    if stream is None:
        return
    for raw_line in stream:
        stderr_lines.append(raw_line)


def _load_batch_candidates(workspace_context: WorkspaceContext, *, batch_id: int) -> list[dict[str, object]]:
    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT
              scan_batch_items.id AS scan_batch_item_id,
              scan_batch_items.item_index,
              scan_batch_items.source_id,
              scan_batch_items.absolute_path,
              library_sources.path AS source_path
            FROM scan_batch_items
            INNER JOIN library_sources ON library_sources.id = scan_batch_items.source_id
            WHERE scan_batch_items.batch_id = ?
            ORDER BY scan_batch_items.item_index ASC
            """,
            (batch_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ScanStartError("scan batch item 读取失败。") from exc
    finally:
        connection.close()

    candidates: list[dict[str, object]] = []
    for row in rows:
        absolute_path = Path(str(row["absolute_path"]))
        candidates.append(
            {
                "scan_batch_item_id": int(row["scan_batch_item_id"]),
                "item_index": int(row["item_index"]),
                "source_id": int(row["source_id"]),
                "source_path": str(row["source_path"]),
                "absolute_path": str(absolute_path),
                "file_name": absolute_path.name,
                "file_extension": absolute_path.suffix.lower().lstrip("."),
                "capture_month": _recoverable_capture_month(absolute_path),
                "file_fingerprint": _recoverable_file_fingerprint(absolute_path),
                "live_photo_mov_path": _recoverable_live_photo_mov(absolute_path),
            }
        )
    return candidates


def _mark_batch_running(workspace_context: WorkspaceContext, *, batch_id: int) -> None:
    connection = sqlite3.connect(workspace_context.library_db_path)
    try:
        with connection:
            connection.execute(
                """
                UPDATE scan_batches
                SET status = 'running',
                    started_at = ?,
                    failure_message = NULL,
                    worker_pid = ?
                WHERE id = ?
                """,
                (utc_now_text(), os.getpid(), batch_id),
            )
    except sqlite3.Error as exc:
        raise ScanStartError("scan batch 状态更新失败。") from exc
    finally:
        connection.close()


def _mark_batch_failed(workspace_context: WorkspaceContext, *, batch_id: int, message: str) -> None:
    connection = sqlite3.connect(workspace_context.library_db_path)
    try:
        with connection:
            connection.execute(
                """
                UPDATE scan_batches
                SET status = 'failed',
                    completed_at = ?,
                    failure_message = ?,
                    worker_pid = NULL
                WHERE id = ?
                """,
                (utc_now_text(), message, batch_id),
            )
    except sqlite3.Error as exc:
        raise ScanStartError("scan batch 失败状态落盘失败。") from exc
    finally:
        connection.close()


def _commit_batch_results(
    *,
    workspace_context: WorkspaceContext,
    batch_id: int,
    batch_index: int,
    session_id: int,
    candidates: list[dict[str, object]],
    worker_result: dict[str, object],
) -> None:
    result_items = worker_result.get("items")
    if not isinstance(result_items, list):
        _mark_batch_failed(workspace_context, batch_id=batch_id, message="worker 结果缺少 items")
        _refresh_session_summary(
            workspace_context=workspace_context,
            session_id=session_id,
            final_status="failed",
        )
        raise ScanStartError("worker 结果缺少 items。")
    result_by_path = {
        str(item["absolute_path"]): item
        for item in result_items
        if isinstance(item, dict) and "absolute_path" in item
    }
    if set(result_by_path) != {str(item["absolute_path"]) for item in candidates}:
        _mark_batch_failed(workspace_context, batch_id=batch_id, message="worker 返回的图片集合不完整")
        _refresh_session_summary(
            workspace_context=workspace_context,
            session_id=session_id,
            final_status="failed",
        )
        raise ScanStartError("worker 返回的图片集合不完整。")

    connection = sqlite3.connect(workspace_context.library_db_path)
    moved_artifact_paths: list[Path] = []
    old_artifact_paths_to_cleanup: list[Path] = []
    commit_succeeded = False
    try:
        validated_results = _validate_batch_results(
            workspace_context=workspace_context,
            candidates=candidates,
            result_by_path=result_by_path,
            session_id=session_id,
            batch_id=batch_id,
        )
        connection.execute("ATTACH DATABASE ? AS embedding", (str(workspace_context.embedding_db_path),))
        connection.execute("BEGIN")
        for candidate in candidates:
            validated = validated_results[str(candidate["absolute_path"])]
            result = validated["raw_result"]
            asset_id = _upsert_asset(connection, candidate=candidate, result=result)
            existing_face_ids, existing_faces = _load_existing_asset_face_state(
                connection,
                asset_id=asset_id,
            )
            if str(result["status"]) == "failed":
                old_artifact_paths_to_cleanup.extend(
                    _list_face_artifact_paths(
                        connection,
                        face_ids=existing_face_ids,
                    )
                )
                _delete_invalidated_face_rows(
                    connection,
                    face_ids=existing_face_ids,
                )
                connection.execute(
                    """
                    UPDATE scan_batch_items
                    SET asset_id = ?, status = 'failed', failure_reason = ?, face_count = 0
                    WHERE id = ?
                    """,
                    (
                        asset_id,
                        str(result.get("failure_reason", "图片处理失败")),
                        int(candidate["scan_batch_item_id"]),
                    ),
                )
                _append_scan_log(
                    workspace_context=workspace_context,
                    payload={
                        "timestamp": utc_now_text(),
                        "event": "asset_failed",
                        "session_id": session_id,
                        "batch_id": batch_id,
                        "batch_index": batch_index,
                        "asset_path": str(candidate["absolute_path"]),
                        "reason": str(result.get("failure_reason", "图片处理失败")),
                    },
                )
                continue

            planned_faces = validated["faces"]
            reconcile_result = reconcile_asset_redetection(
                existing_faces=existing_faces,
                redetected_faces=[
                    RedetectFace(
                        bbox=tuple(float(value) for value in planned_face["bbox"]),
                        image_width=int(validated["image_width"]),
                        image_height=int(validated["image_height"]),
                        embedding=np.asarray(planned_face["embedding"], dtype=np.float32),
                    )
                    for planned_face in planned_faces
                ],
            )
            reused_face_ids = {int(face_id) for face_id in reconcile_result.reused_face_ids}
            invalidated_face_ids = [
                face_id for face_id in existing_face_ids if face_id not in reused_face_ids
            ]
            old_artifact_paths_to_cleanup.extend(
                _list_face_artifact_paths(
                    connection,
                    face_ids=invalidated_face_ids,
                )
            )
            _delete_invalidated_face_rows(
                connection,
                face_ids=invalidated_face_ids,
            )
            next_face_index = _next_face_index_for_asset(connection, asset_id=asset_id)
            for planned_face in planned_faces:
                reused_face_id = reconcile_result.reused_face_id_by_detection_index.get(int(planned_face["face_index"]))
                if reused_face_id is not None:
                    old_artifact_paths_to_cleanup.extend(
                        _list_face_artifact_paths(
                            connection,
                            face_ids=[int(reused_face_id)],
                        )
                    )
                    final_crop_path, final_context_path = _materialize_artifacts(
                        planned_face=planned_face,
                    )
                    moved_artifact_paths.extend([final_crop_path, final_context_path])
                    _update_reused_face_observation(
                        connection,
                        face_observation_id=int(reused_face_id),
                        planned_face=planned_face,
                        image_width=int(validated["image_width"]),
                        image_height=int(validated["image_height"]),
                        crop_path=final_crop_path,
                        context_path=final_context_path,
                    )
                    continue
                final_crop_path, final_context_path = _materialize_artifacts(
                    planned_face=planned_face,
                )
                moved_artifact_paths.extend([final_crop_path, final_context_path])
                face_cursor = connection.execute(
                    """
                    INSERT INTO face_observations (
                      asset_id,
                      face_index,
                      bbox_x1,
                      bbox_y1,
                      bbox_x2,
                      bbox_y2,
                      image_width,
                      image_height,
                      score,
                      crop_path,
                      context_path,
                      created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        asset_id,
                        next_face_index,
                        float(planned_face["bbox"][0]),
                        float(planned_face["bbox"][1]),
                        float(planned_face["bbox"][2]),
                        float(planned_face["bbox"][3]),
                        int(validated["image_width"]),
                        int(validated["image_height"]),
                        float(planned_face["score"]),
                        str(final_crop_path),
                        str(final_context_path),
                        utc_now_text(),
                    ),
                )
                next_face_index += 1
                face_observation_id = int(face_cursor.lastrowid)
                vector = planned_face["embedding"]
                connection.execute(
                    """
                    INSERT INTO embedding.face_embeddings (
                      face_observation_id,
                      variant,
                      dimension,
                      l2_norm,
                      vector_blob,
                      created_at
                    )
                    VALUES (?, 'main', ?, ?, ?, ?)
                    """,
                    (
                        face_observation_id,
                        int(vector.shape[0]),
                        float(np.linalg.norm(vector)),
                        vector.astype(np.float32).tobytes(),
                        utc_now_text(),
                    ),
                )
            connection.execute(
                """
                UPDATE scan_batch_items
                SET asset_id = ?, status = 'succeeded', failure_reason = NULL, face_count = ?
                WHERE id = ?
                """,
                (
                    asset_id,
                    len(planned_faces),
                    int(candidate["scan_batch_item_id"]),
                ),
            )
        connection.execute(
            """
            UPDATE scan_batches
            SET status = 'completed',
                completed_at = ?,
                failure_message = NULL,
                worker_pid = NULL
            WHERE id = ?
            """,
            (utc_now_text(), batch_id),
        )
        connection.commit()
        commit_succeeded = True
    except Exception as exc:  # noqa: BLE001
        if not commit_succeeded:
            connection.rollback()
            _cleanup_final_artifacts(moved_artifact_paths)
        failure_message = str(exc) if str(exc) else "批次结果提交失败"
        _mark_batch_failed(workspace_context, batch_id=batch_id, message=failure_message)
        _refresh_session_summary(
            workspace_context=workspace_context,
            session_id=session_id,
            final_status="failed",
        )
        if isinstance(exc, ScanStartError):
            raise
        raise ScanStartError(failure_message) from exc
    finally:
        connection.close()

    cleanup_warning = _cleanup_old_artifacts_after_commit(
        workspace_context=workspace_context,
        session_id=session_id,
        batch_id=batch_id,
        batch_index=batch_index,
        old_artifact_paths=old_artifact_paths_to_cleanup,
    )
    summary = _refresh_session_summary(
        workspace_context=workspace_context,
        session_id=session_id,
        final_status="running",
    )
    _append_scan_log(
        workspace_context=workspace_context,
        payload={
            "timestamp": utc_now_text(),
            "event": "batch_completed",
            "session_id": session_id,
            "batch_id": batch_id,
            "batch_index": batch_index,
            "completed_batches": summary["completed_batches"],
            "failed_assets": summary["failed_assets"],
            "success_faces": summary["success_faces"],
            "artifact_files": summary["artifact_files"],
        },
    )
    if cleanup_warning is not None:
        _best_effort_append_scan_log(
            workspace_context=workspace_context,
            payload={
                "timestamp": utc_now_text(),
                "event": "artifact_cleanup_warning",
                "session_id": session_id,
                "batch_id": batch_id,
                "batch_index": batch_index,
                "reason": cleanup_warning,
                "path_count": len(old_artifact_paths_to_cleanup),
            },
        )


def _run_assignment_stage(
    *,
    workspace_context: WorkspaceContext,
    session_id: int,
    progress_state: dict[str, int],
) -> None:
    try:
        run_online_assignment(
            workspace_context=workspace_context,
            scan_session_id=session_id,
            append_log=lambda payload: _append_scan_log(
                workspace_context=workspace_context,
                payload=payload,
            ),
            progress_callback=lambda _event: _print_scan_progress(
                stage="在线归属",
                total_batches=progress_state["total_batches"],
                completed_batches=progress_state["completed_batches"],
                total_items=progress_state["total_items"],
                completed_items=progress_state["completed_items"],
            ),
        )
    except OnlineAssignmentError as exc:
        raise ScanStartError(str(exc)) from exc


def _upsert_asset(connection: sqlite3.Connection, *, candidate: dict[str, object], result: dict[str, object]) -> int:
    now = utc_now_text()
    connection.execute(
        """
        INSERT INTO assets (
          source_id,
          absolute_path,
          file_name,
          file_extension,
          capture_month,
          file_fingerprint,
          live_photo_mov_path,
          processing_status,
          failure_reason,
          created_at,
          updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(absolute_path) DO UPDATE SET
          source_id = excluded.source_id,
          file_name = excluded.file_name,
          file_extension = excluded.file_extension,
          capture_month = excluded.capture_month,
          file_fingerprint = excluded.file_fingerprint,
          live_photo_mov_path = excluded.live_photo_mov_path,
          processing_status = excluded.processing_status,
          failure_reason = excluded.failure_reason,
          updated_at = excluded.updated_at
        """,
        (
            int(candidate["source_id"]),
            str(candidate["absolute_path"]),
            str(candidate["file_name"]),
            str(candidate["file_extension"]),
            str(candidate["capture_month"]),
            str(candidate["file_fingerprint"]),
            candidate["live_photo_mov_path"],
            "failed" if str(result["status"]) == "failed" else "succeeded",
            str(result.get("failure_reason")) if str(result["status"]) == "failed" else None,
            now,
            now,
        ),
    )
    row = connection.execute(
        "SELECT id FROM assets WHERE absolute_path = ?",
        (str(candidate["absolute_path"]),),
    ).fetchone()
    if row is None:
        raise ScanStartError(f"asset 写入失败：{candidate['absolute_path']}")
    return int(row[0])


def _clear_existing_face_rows(connection: sqlite3.Connection, *, asset_id: int) -> None:
    rows = connection.execute(
        "SELECT id FROM face_observations WHERE asset_id = ?",
        (asset_id,),
    ).fetchall()
    if rows:
        face_ids = [int(row[0]) for row in rows]
        placeholders = ", ".join("?" for _ in face_ids)
        connection.execute(
            f"DELETE FROM embedding.face_embeddings WHERE face_observation_id IN ({placeholders})",
            face_ids,
        )
        connection.execute(
            "DELETE FROM face_observations WHERE asset_id = ?",
            (asset_id,),
        )


def _load_existing_asset_face_state(
    connection: sqlite3.Connection,
    *,
    asset_id: int,
) -> tuple[list[int], list[ExistingAssetFace]]:
    rows = connection.execute(
        """
        SELECT
          face_observations.id,
          face_observations.bbox_x1,
          face_observations.bbox_y1,
          face_observations.bbox_x2,
          face_observations.bbox_y2,
          face_observations.image_width,
          face_observations.image_height,
          embedding.face_embeddings.dimension,
          embedding.face_embeddings.vector_blob,
          person_face_assignments.person_id
        FROM face_observations
        LEFT JOIN embedding.face_embeddings
          ON embedding.face_embeddings.face_observation_id = face_observations.id
         AND embedding.face_embeddings.variant = 'main'
        LEFT JOIN person_face_assignments
          ON person_face_assignments.face_observation_id = face_observations.id
         AND person_face_assignments.active = 1
        WHERE face_observations.asset_id = ?
        ORDER BY face_observations.id ASC
        """,
        (asset_id,),
    ).fetchall()
    all_face_ids: list[int] = []
    existing_faces: list[ExistingAssetFace] = []
    for row in rows:
        face_id = int(row[0])
        all_face_ids.append(face_id)
        vector_blob = row[8]
        vector: np.ndarray | None = None
        if row[7] is not None and int(row[7]) == 512 and isinstance(vector_blob, (bytes, bytearray, memoryview)):
            try:
                decoded_vector = np.frombuffer(bytes(vector_blob), dtype=np.float32)
            except ValueError:
                decoded_vector = None
            if decoded_vector is not None and decoded_vector.shape == (512,):
                vector = decoded_vector.copy()
        existing_faces.append(
            ExistingAssetFace(
                face_id=str(face_id),
                bbox=(float(row[1]), float(row[2]), float(row[3]), float(row[4])),
                image_width=int(row[5]),
                image_height=int(row[6]),
                person_id=str(row[9]) if row[9] is not None else None,
                embedding=vector,
            )
        )
    return all_face_ids, existing_faces


def _list_face_artifact_paths(connection: sqlite3.Connection, *, face_ids: list[int]) -> list[Path]:
    if not face_ids:
        return []
    placeholders = ", ".join("?" for _ in face_ids)
    rows = connection.execute(
        f"""
        SELECT crop_path, context_path
        FROM face_observations
        WHERE id IN ({placeholders})
        ORDER BY id ASC
        """,
        face_ids,
    ).fetchall()
    paths: list[Path] = []
    for crop_path, context_path in rows:
        paths.append(Path(str(crop_path)))
        paths.append(Path(str(context_path)))
    return paths


def _update_reused_face_observation(
    connection: sqlite3.Connection,
    *,
    face_observation_id: int,
    planned_face: dict[str, object],
    image_width: int,
    image_height: int,
    crop_path: Path,
    context_path: Path,
) -> None:
    connection.execute(
        """
        UPDATE face_observations
        SET bbox_x1 = ?,
            bbox_y1 = ?,
            bbox_x2 = ?,
            bbox_y2 = ?,
            image_width = ?,
            image_height = ?,
            score = ?,
            crop_path = ?,
            context_path = ?
        WHERE id = ?
        """,
        (
            float(planned_face["bbox"][0]),
            float(planned_face["bbox"][1]),
            float(planned_face["bbox"][2]),
            float(planned_face["bbox"][3]),
            image_width,
            image_height,
            float(planned_face["score"]),
            str(crop_path),
            str(context_path),
            face_observation_id,
        ),
    )


def _delete_invalidated_face_rows(connection: sqlite3.Connection, *, face_ids: list[int]) -> None:
    if not face_ids:
        return
    placeholders = ", ".join("?" for _ in face_ids)
    person_rows = connection.execute(
        f"""
        SELECT DISTINCT person_id
        FROM person_face_assignments
        WHERE face_observation_id IN ({placeholders})
          AND person_id IS NOT NULL
        """,
        face_ids,
    ).fetchall()
    person_ids = [str(row[0]) for row in person_rows if row[0] is not None]
    if person_ids:
        person_placeholders = ", ".join("?" for _ in person_ids)
        now = utc_now_text()
        connection.execute(
            f"""
            UPDATE person
            SET write_revision = write_revision + 1,
                updated_at = ?
            WHERE id IN ({person_placeholders})
            """,
            (now, *person_ids),
        )
    connection.execute(
        f"DELETE FROM person_face_assignments WHERE face_observation_id IN ({placeholders})",
        face_ids,
    )
    connection.execute(
        f"DELETE FROM embedding.face_embeddings WHERE face_observation_id IN ({placeholders})",
        face_ids,
    )
    connection.execute(
        f"DELETE FROM face_observations WHERE id IN ({placeholders})",
        face_ids,
    )
    if not person_ids:
        return
    person_placeholders = ", ".join("?" for _ in person_ids)
    connection.execute(
        f"""
        DELETE FROM person
        WHERE id IN ({person_placeholders})
          AND NOT EXISTS (
            SELECT 1
            FROM person_face_assignments
            WHERE person_face_assignments.person_id = person.id
              AND person_face_assignments.active = 1
          )
        """,
        person_ids,
    )


def _next_face_index_for_asset(connection: sqlite3.Connection, *, asset_id: int) -> int:
    row = connection.execute(
        """
        SELECT COALESCE(MAX(face_index), -1) + 1
        FROM face_observations
        WHERE asset_id = ?
        """,
        (asset_id,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _list_existing_face_artifact_paths(connection: sqlite3.Connection, *, asset_id: int) -> list[Path]:
    rows = connection.execute(
        """
        SELECT crop_path, context_path
        FROM face_observations
        WHERE asset_id = ?
        ORDER BY id ASC
        """,
        (asset_id,),
    ).fetchall()
    paths: list[Path] = []
    for crop_path, context_path in rows:
        paths.append(Path(str(crop_path)))
        paths.append(Path(str(context_path)))
    return paths


def _materialize_artifacts(
    *,
    planned_face: dict[str, object],
) -> tuple[Path, Path]:
    crop_source = Path(str(planned_face["crop_source"]))
    context_source = Path(str(planned_face["context_source"]))
    crop_target = Path(str(planned_face["crop_target"]))
    context_target = Path(str(planned_face["context_target"]))
    crop_target.parent.mkdir(parents=True, exist_ok=True)
    context_target.parent.mkdir(parents=True, exist_ok=True)
    if crop_target.exists():
        crop_target.unlink()
    if context_target.exists():
        context_target.unlink()
    moved_targets: list[Path] = []
    try:
        shutil.move(str(crop_source), str(crop_target))
        moved_targets.append(crop_target)
        shutil.move(str(context_source), str(context_target))
        moved_targets.append(context_target)
        return crop_target, context_target
    except Exception:  # noqa: BLE001
        _cleanup_final_artifacts(moved_targets)
        raise


def _validate_batch_results(
    *,
    workspace_context: WorkspaceContext,
    candidates: list[dict[str, object]],
    result_by_path: dict[str, dict[str, object]],
    session_id: int,
    batch_id: int,
) -> dict[str, dict[str, object]]:
    validated_results: dict[str, dict[str, object]] = {}
    for candidate in candidates:
        absolute_path = str(candidate["absolute_path"])
        result = result_by_path[absolute_path]
        if str(result["status"]) == "failed":
            validated_results[absolute_path] = {"raw_result": result, "faces": []}
            continue
        detections = result.get("detections")
        artifacts = result.get("artifacts")
        if not isinstance(detections, list) or not isinstance(artifacts, list):
            raise ScanStartError(f"worker 结果格式错误：{absolute_path}")
        if len(detections) != len(artifacts):
            raise ScanStartError(f"worker 产物数量不匹配：{absolute_path}")
        image_width = result.get("image_width")
        image_height = result.get("image_height")
        if not isinstance(image_width, int) or not isinstance(image_height, int):
            raise ScanStartError(f"worker 图像尺寸格式错误：{absolute_path}")
        planned_faces: list[dict[str, object]] = []
        for face_index, (detection, artifact) in enumerate(zip(detections, artifacts, strict=False)):
            if not isinstance(detection, dict) or not isinstance(artifact, dict):
                raise ScanStartError(f"worker 结果格式错误：{absolute_path}")
            bbox = detection.get("bbox")
            if not isinstance(bbox, list) or len(bbox) != 4:
                raise ScanStartError(f"worker bbox 格式错误：{absolute_path}")
            vector = np.asarray(detection.get("embedding"), dtype=np.float32)
            if vector.shape != (512,):
                raise ScanStartError(f"embedding 维度错误：{absolute_path} -> {vector.shape}")
            crop_source = Path(str(artifact.get("crop_path")))
            context_source = Path(str(artifact.get("context_path")))
            if not crop_source.is_file() or not context_source.is_file():
                raise ScanStartError(f"worker 产物文件不存在：{absolute_path}")
            artifact_token = _artifact_token_for_candidate(candidate=candidate)
            planned_faces.append(
                {
                    "face_index": face_index,
                    "bbox": [float(value) for value in bbox],
                    "score": float(detection["score"]),
                    "embedding": vector,
                    "crop_source": str(crop_source),
                    "context_source": str(context_source),
                    "crop_target": str(
                        (
                            workspace_context.external_root_path
                            / "artifacts"
                            / "crops"
                            / f"s{session_id:04d}_b{batch_id:04d}_{artifact_token}_face_{face_index:02d}.jpg"
                        ).resolve()
                    ),
                    "context_target": str(
                        (
                            workspace_context.external_root_path
                            / "artifacts"
                            / "context"
                            / f"s{session_id:04d}_b{batch_id:04d}_{artifact_token}_face_{face_index:02d}.jpg"
                        ).resolve()
                    ),
                }
            )
        validated_results[absolute_path] = {
            "raw_result": result,
            "faces": planned_faces,
            "image_width": image_width,
            "image_height": image_height,
        }
    return validated_results


def _cleanup_final_artifacts(paths: list[Path]) -> None:
    for path in reversed(paths):
        if path.exists():
            path.unlink(missing_ok=True)


def _cleanup_old_artifacts_after_commit(
    *,
    workspace_context: WorkspaceContext,
    session_id: int,
    batch_id: int,
    batch_index: int,
    old_artifact_paths: list[Path],
) -> str | None:
    if not old_artifact_paths:
        return None
    try:
        _cleanup_final_artifacts(old_artifact_paths)
    except Exception as exc:  # noqa: BLE001
        return str(exc) or "旧 artifact 清理失败"
    return None


def _artifact_token_for_candidate(*, candidate: dict[str, object]) -> str:
    scan_batch_item_id = candidate.get("scan_batch_item_id")
    item_index = candidate.get("item_index")
    if isinstance(scan_batch_item_id, int):
        unique_token = f"item{scan_batch_item_id:06d}"
    elif isinstance(item_index, int):
        unique_token = f"index{item_index:04d}"
    else:
        unique_token = "item000000"
    return f"{unique_token}_{candidate['file_fingerprint']}"


def _recoverable_capture_month(absolute_path: Path) -> str:
    try:
        return compute_capture_month(absolute_path)
    except OSError:
        return utc_now_text()[0:7]


def _recoverable_file_fingerprint(absolute_path: Path) -> str:
    try:
        return compute_file_fingerprint(absolute_path)
    except OSError:
        return hashlib.sha256(str(absolute_path.resolve()).encode("utf-8")).hexdigest()


def _recoverable_live_photo_mov(absolute_path: Path) -> str | None:
    try:
        return find_live_photo_mov(absolute_path)
    except OSError:
        return None


def _refresh_session_summary(
    *,
    workspace_context: WorkspaceContext,
    session_id: int,
    final_status: str,
) -> dict[str, int]:
    connection = sqlite3.connect(workspace_context.library_db_path)
    try:
        total_batches = int(
            connection.execute(
                "SELECT COUNT(*) FROM scan_batches WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
        )
        completed_batches = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM scan_batches
                WHERE session_id = ? AND status = 'completed'
                """,
                (session_id,),
            ).fetchone()[0]
        )
        failed_assets = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM scan_batch_items
                WHERE batch_id IN (
                  SELECT id FROM scan_batches WHERE session_id = ?
                ) AND status = 'failed'
                """,
                (session_id,),
            ).fetchone()[0]
        )
        success_faces = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM face_observations
                WHERE asset_id IN (
                  SELECT asset_id
                  FROM scan_batch_items
                  WHERE batch_id IN (
                    SELECT id FROM scan_batches WHERE session_id = ?
                  ) AND status = 'succeeded'
                )
                """,
                (session_id,),
            ).fetchone()[0]
        )
        artifact_files = success_faces * 2
        status = final_status
        completed_at = utc_now_text() if final_status == "completed" else None
        with connection:
            connection.execute(
                """
                UPDATE scan_sessions
                SET status = ?,
                    total_batches = ?,
                    completed_batches = ?,
                    failed_assets = ?,
                    success_faces = ?,
                    artifact_files = ?,
                    completed_at = COALESCE(?, completed_at)
                WHERE id = ?
                """,
                (
                    status,
                    total_batches,
                    completed_batches,
                    failed_assets,
                    success_faces,
                    artifact_files,
                    completed_at,
                    session_id,
                ),
            )
        return {
            "total_batches": total_batches,
            "completed_batches": completed_batches,
            "failed_assets": failed_assets,
            "success_faces": success_faces,
            "artifact_files": artifact_files,
        }
    except sqlite3.Error as exc:
        raise ScanStartError("scan summary 更新失败。") from exc
    finally:
        connection.close()


def _append_scan_log(
    *,
    workspace_context: WorkspaceContext,
    payload: dict[str, object],
) -> None:
    logs_dir = workspace_context.external_root_path / "logs"
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        with (logs_dir / "scan.log.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as exc:
        raise ScanStartError(f"scan 日志写入失败：{logs_dir}: {exc}") from exc


def _chunk_candidates(candidates: Sequence[dict[str, object]], batch_size: int) -> list[list[dict[str, object]]]:
    chunks: list[list[dict[str, object]]] = []
    for index in range(0, len(candidates), batch_size):
        chunks.append(list(candidates[index : index + batch_size]))
    return chunks
