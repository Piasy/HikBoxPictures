from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from hikbox_pictures.product.db.connection import connect_sqlite


@dataclass(frozen=True)
class ScanRuntimeDefaults:
    det_size: int
    batch_size: int
    workers: int


@dataclass(frozen=True)
class DetectBatchClaim:
    batch_id: int
    claim_token: str
    worker_slot: int
    items: list[dict[str, int]]


def build_scan_runtime_defaults(*, cpu_count: int | None = None) -> ScanRuntimeDefaults:
    detected_cpu = 1 if cpu_count is None else cpu_count
    workers = max(1, detected_cpu // 2)
    return ScanRuntimeDefaults(det_size=640, batch_size=300, workers=workers)


def split_batch(*, total: int, workers: int) -> list[int]:
    if workers <= 0:
        raise ValueError("workers 必须大于 0")
    if total < 0:
        raise ValueError("total 不能为负数")
    base = total // workers
    remainder = total % workers
    return [base + (1 if index < remainder else 0) for index in range(workers)]


class DetectStageRepository:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def seed_detect_batches(
        self,
        *,
        scan_session_id: int,
        photo_asset_ids: list[int],
        workers: int,
        batch_size: int,
    ) -> list[int]:
        if workers <= 0:
            raise ValueError("workers 必须大于 0")
        if batch_size <= 0:
            raise ValueError("batch_size 必须大于 0")

        created_batch_ids: list[int] = []
        now = _utc_now()
        with connect_sqlite(self._db_path) as conn:
            for batch_index, batch_assets in enumerate(_chunk_assets(photo_asset_ids, batch_size)):
                claim_token = _new_claim_token()
                worker_slot = batch_index % workers
                cursor = conn.execute(
                    """
                    INSERT INTO scan_batch(
                      scan_session_id,
                      stage,
                      worker_slot,
                      claim_token,
                      status,
                      retry_count,
                      claimed_at,
                      started_at,
                      acked_at,
                      error_message
                    )
                    VALUES (?, 'detect', ?, ?, 'claimed', 0, ?, NULL, NULL, NULL)
                    """,
                    (scan_session_id, worker_slot, claim_token, now),
                )
                batch_id = int(cursor.lastrowid)
                created_batch_ids.append(batch_id)

                for item_order, photo_asset_id in enumerate(batch_assets):
                    conn.execute(
                        """
                        INSERT INTO scan_batch_item(
                          scan_batch_id,
                          photo_asset_id,
                          item_order,
                          status,
                          error_message,
                          updated_at
                        )
                        VALUES (?, ?, ?, 'pending', NULL, ?)
                        """,
                        (batch_id, photo_asset_id, item_order, now),
                    )
            conn.commit()

        return created_batch_ids

    def claim_detect_batch(self, *, scan_session_id: int, worker_slot: int) -> DetectBatchClaim | None:
        with connect_sqlite(self._db_path) as conn:
            while True:
                row = conn.execute(
                    """
                    SELECT id, claim_token, worker_slot
                    FROM scan_batch
                    WHERE scan_session_id=?
                      AND stage='detect'
                      AND worker_slot=?
                      AND status='claimed'
                    ORDER BY id
                    LIMIT 1
                    """,
                    (scan_session_id, worker_slot),
                ).fetchone()
                if row is None:
                    return None
                batch_id = int(row[0])
                claimed = conn.execute(
                    """
                    UPDATE scan_batch
                    SET worker_slot=-(worker_slot + 1),
                        claimed_at=?
                    WHERE id=?
                      AND status='claimed'
                      AND worker_slot=?
                    """,
                    (_utc_now(), batch_id, worker_slot),
                )
                if claimed.rowcount == 1:
                    item_rows = conn.execute(
                        """
                        SELECT id, photo_asset_id
                        FROM scan_batch_item
                        WHERE scan_batch_id=?
                        ORDER BY item_order
                        """,
                        (batch_id,),
                    ).fetchall()
                    conn.commit()
                    return DetectBatchClaim(
                        batch_id=batch_id,
                        claim_token=str(row[1]),
                        worker_slot=int(row[2]),
                        items=[
                            {
                                "scan_batch_item_id": int(item_row[0]),
                                "photo_asset_id": int(item_row[1]),
                            }
                            for item_row in item_rows
                        ],
                    )

    def dispatch_batch(self, claim_token: str) -> None:
        now = _utc_now()
        with connect_sqlite(self._db_path) as conn:
            row = conn.execute(
                "SELECT id, status, worker_slot FROM scan_batch WHERE claim_token=?",
                (claim_token,),
            ).fetchone()
            if row is None:
                raise ValueError(f"未找到 claim_token={claim_token}")
            batch_id = int(row[0])
            batch_status = str(row[1])
            reserved_slot = int(row[2])

            if batch_status == "running":
                conn.commit()
                return
            if batch_status != "claimed":
                raise ValueError(f"批次状态={batch_status}，不能 dispatch")

            updated = conn.execute(
                """
                UPDATE scan_batch
                SET status='running',
                    worker_slot=?,
                    started_at=?,
                    error_message=NULL
                WHERE id=?
                  AND status='claimed'
                """,
                (_normalize_worker_slot(reserved_slot), now, batch_id),
            )
            if updated.rowcount != 1:
                raise ValueError(f"批次状态变化，不能 dispatch claim_token={claim_token}")
            conn.execute(
                """
                UPDATE scan_batch_item
                SET status='running',
                    error_message=NULL,
                    updated_at=?
                WHERE scan_batch_id=?
                """,
                (now, batch_id),
            )
            conn.commit()

    def ack_detect_batch(self, claim_token: str, *, worker_payload_path: Path | None = None) -> None:
        now = _utc_now()
        results_by_item_id: dict[int, tuple[str, str | None]] | None = None
        payload_claim_token: str | None = None
        payload_batch_id: int | None = None
        if worker_payload_path is not None:
            payload = json.loads(worker_payload_path.read_text(encoding="utf-8"))
            payload_claim_token = str(payload.get("claim_token", ""))
            if "scan_batch_id" in payload:
                payload_batch_id = int(payload["scan_batch_id"])
            elif "batch_id" in payload:
                payload_batch_id = int(payload["batch_id"])
            else:
                raise ValueError("worker payload 必须包含 batch 字段(scan_batch_id 或 batch_id)")
            results_by_item_id = {}
            for item in payload.get("results", []):
                results_by_item_id[int(item["scan_batch_item_id"])] = (
                    str(item.get("status", "done")),
                    str(item.get("error_message")) if item.get("error_message") else None,
                )

        with connect_sqlite(self._db_path) as conn:
            row = conn.execute(
                "SELECT id, status FROM scan_batch WHERE claim_token=?",
                (claim_token,),
            ).fetchone()
            if row is None:
                raise ValueError(f"未找到 claim_token={claim_token}")
            batch_id = int(row[0])
            batch_status = str(row[1])
            if batch_status != "running":
                raise ValueError(f"批次状态={batch_status}，仅 running 可 ack")

            if results_by_item_id is None:
                conn.execute(
                    """
                    UPDATE scan_batch_item
                    SET status='done',
                        error_message=NULL,
                        updated_at=?
                    WHERE scan_batch_id=?
                    """,
                    (now, batch_id),
                )
                next_status = "acked"
                error_message = None
            else:
                if payload_claim_token != claim_token:
                    raise ValueError("worker payload claim_token 与 ack 参数不一致")
                if payload_batch_id != batch_id:
                    raise ValueError("worker payload scan_batch_id 与当前批次不一致")
                failed_count = 0
                item_rows = conn.execute(
                    "SELECT id FROM scan_batch_item WHERE scan_batch_id=?",
                    (batch_id,),
                ).fetchall()
                item_ids = {int(item_row[0]) for item_row in item_rows}
                payload_ids = set(results_by_item_id.keys())
                if payload_ids != item_ids:
                    missing_ids = sorted(item_ids - payload_ids)
                    extra_ids = sorted(payload_ids - item_ids)
                    raise ValueError(f"worker payload 未覆盖全部 batch item: missing={missing_ids}, extra={extra_ids}")
                for item_row in item_rows:
                    item_id = int(item_row[0])
                    item_status, item_error = results_by_item_id[item_id]
                    mapped_status = "done" if item_status == "done" else "failed"
                    if mapped_status == "failed":
                        failed_count += 1
                    conn.execute(
                        """
                        UPDATE scan_batch_item
                        SET status=?,
                            error_message=?,
                            updated_at=?
                        WHERE id=?
                        """,
                        (mapped_status, item_error, now, item_id),
                    )
                if failed_count > 0:
                    next_status = "failed"
                    error_message = f"detect worker 返回 {failed_count} 条失败"
                else:
                    next_status = "acked"
                    error_message = None

            conn.execute(
                """
                UPDATE scan_batch
                SET status=?,
                    acked_at=?,
                    error_message=?
                WHERE id=?
                """,
                (next_status, now, error_message, batch_id),
            )
            conn.commit()

    def rollback_unacked_batches(self, *, scan_session_id: int) -> int:
        now = _utc_now()
        with connect_sqlite(self._db_path) as conn:
            rolled_back = self._rollback_unacked_batches_in_conn(
                conn,
                scan_session_id=scan_session_id,
                now=now,
            )
            conn.commit()
            return rolled_back

    def rollback_unacked_batches_and_interrupt(self, *, scan_session_id: int, last_error: str) -> int:
        now = _utc_now()
        with connect_sqlite(self._db_path) as conn:
            rolled_back = self._rollback_unacked_batches_in_conn(
                conn,
                scan_session_id=scan_session_id,
                now=now,
            )
            updated = conn.execute(
                """
                UPDATE scan_session
                SET status='interrupted',
                    finished_at=?,
                    last_error=?,
                    updated_at=?
                WHERE id=?
                  AND status IN ('running', 'aborting')
                """,
                (now, last_error, now, scan_session_id),
            )
            if updated.rowcount != 1:
                raise ValueError(f"session_id={scan_session_id} 不是 running/aborting，无法迁移 interrupted")
            conn.commit()
            return rolled_back

    def _rollback_unacked_batches_in_conn(self, conn: object, *, scan_session_id: int, now: str) -> int:
        rows = conn.execute(
                """
                SELECT id
                FROM scan_batch
                WHERE scan_session_id=?
                  AND stage='detect'
                  AND status IN ('claimed', 'running')
                """,
                (scan_session_id,),
            ).fetchall()

        for row in rows:
            batch_id = int(row[0])
            conn.execute(
                """
                UPDATE scan_batch
                SET status='claimed',
                    worker_slot=CASE
                        WHEN worker_slot < 0 THEN (-worker_slot) - 1
                        ELSE worker_slot
                    END,
                    retry_count=retry_count + 1,
                    claim_token=?,
                    claimed_at=?,
                    started_at=NULL,
                    error_message=NULL
                WHERE id=?
                """,
                (_new_claim_token(), now, batch_id),
            )
            conn.execute(
                """
                UPDATE scan_batch_item
                SET status='pending',
                    error_message=NULL,
                    updated_at=?
                WHERE scan_batch_id=?
                  AND status IN ('pending', 'running')
                """,
                (now, batch_id),
            )
        return len(rows)


def rollback_unacked_batches_and_interrupt(
    *,
    detect_repo: DetectStageRepository,
    session_id: int,
    last_error: str,
) -> None:
    detect_repo.rollback_unacked_batches_and_interrupt(scan_session_id=session_id, last_error=last_error)


def _chunk_assets(photo_asset_ids: list[int], batch_size: int) -> list[list[int]]:
    return [photo_asset_ids[index : index + batch_size] for index in range(0, len(photo_asset_ids), batch_size)]


def _new_claim_token() -> str:
    return uuid.uuid4().hex


def _normalize_worker_slot(worker_slot: int) -> int:
    if worker_slot < 0:
        return (-worker_slot) - 1
    return worker_slot


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
