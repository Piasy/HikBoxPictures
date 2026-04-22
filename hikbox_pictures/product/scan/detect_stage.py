"""detect 阶段 claim/ack 数据访问。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from hikbox_pictures.product.db.connection import connect_sqlite


@dataclass(frozen=True)
class ClaimedDetectBatch:
    batch_id: int
    claim_token: str
    worker_slot: int
    items: list[dict[str, object]]


class DetectStageRepository:
    """detect 阶段数据库仓储。"""

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        conn = connect_sqlite(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def prepare_detect_batches(self, *, scan_session_id: int, batch_size: int, workers: int) -> int:
        """按当前待检测资产生成 detect 批次。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            pending_assets = conn.execute(
                """
                SELECT p.id
                FROM photo_asset AS p
                WHERE p.asset_status='active'
                  AND NOT EXISTS (
                    SELECT 1
                    FROM scan_batch_item AS i
                    JOIN scan_batch AS b
                      ON b.id = i.scan_batch_id
                    WHERE b.scan_session_id = ?
                      AND b.stage = 'detect'
                      AND i.photo_asset_id = p.id
                      AND i.status IN ('done', 'failed')
                  )
                ORDER BY p.id ASC
                LIMIT ?
                """,
                (scan_session_id, max(1, int(batch_size))),
            ).fetchall()
            if not pending_assets:
                conn.commit()
                return 0

            worker_count = max(1, int(workers))
            groups = _split_items_evenly([int(row[0]) for row in pending_assets], worker_count)
            created = 0
            for worker_slot, group in enumerate(groups):
                if not group:
                    continue
                claim_token = uuid.uuid4().hex
                batch_cursor = conn.execute(
                    """
                    INSERT INTO scan_batch(
                      scan_session_id, stage, worker_slot, claim_token, status, retry_count, claimed_at
                    ) VALUES (?, 'detect', ?, ?, 'claimed', 0, CURRENT_TIMESTAMP)
                    """,
                    (scan_session_id, worker_slot, claim_token),
                )
                batch_id = int(batch_cursor.lastrowid)
                for item_order, photo_asset_id in enumerate(group):
                    conn.execute(
                        """
                        INSERT INTO scan_batch_item(
                          scan_batch_id, photo_asset_id, item_order, status, updated_at
                        ) VALUES (?, ?, ?, 'pending', CURRENT_TIMESTAMP)
                        """,
                        (batch_id, photo_asset_id, item_order),
                    )
                    created += 1

            conn.commit()
            return created
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def has_remaining_detect_work(self, *, scan_session_id: int) -> bool:
        """判断当前会话是否仍有未进入终态（done/failed）的 detect 资产。"""
        conn = self.connect()
        try:
            row = conn.execute(
                """
                SELECT EXISTS(
                  SELECT 1
                  FROM photo_asset AS p
                  WHERE p.asset_status='active'
                    AND NOT EXISTS (
                      SELECT 1
                      FROM scan_batch_item AS i
                      JOIN scan_batch AS b
                        ON b.id = i.scan_batch_id
                      WHERE b.scan_session_id = ?
                        AND b.stage = 'detect'
                        AND i.photo_asset_id = p.id
                        AND i.status IN ('done', 'failed')
                    )
                )
                """,
                (scan_session_id,),
            ).fetchone()
            return bool(int(row[0])) if row is not None else False
        finally:
            conn.close()

    def claim_detect_batch(self, *, scan_session_id: int, worker_slot: int) -> ClaimedDetectBatch | None:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            batch = conn.execute(
                """
                SELECT id, claim_token, worker_slot
                FROM scan_batch
                WHERE scan_session_id=? AND stage='detect' AND worker_slot=? AND status='claimed'
                ORDER BY id ASC
                LIMIT 1
                """,
                (scan_session_id, worker_slot),
            ).fetchone()
            if batch is None:
                conn.commit()
                return None

            batch_id = int(batch[0])
            claim_token = str(batch[1])
            conn.execute(
                """
                UPDATE scan_batch
                SET status='running',
                    started_at=CURRENT_TIMESTAMP
                WHERE id=? AND claim_token=?
                """,
                (batch_id, claim_token),
            )
            conn.execute(
                """
                UPDATE scan_batch_item
                SET status='running',
                    updated_at=CURRENT_TIMESTAMP
                WHERE scan_batch_id=? AND status='pending'
                """,
                (batch_id,),
            )
            rows = conn.execute(
                """
                SELECT
                  i.photo_asset_id,
                  s.root_path,
                  p.primary_path,
                  i.item_order
                FROM scan_batch_item AS i
                JOIN photo_asset AS p ON p.id = i.photo_asset_id
                JOIN library_source AS s ON s.id = p.library_source_id
                WHERE i.scan_batch_id = ?
                ORDER BY i.item_order ASC
                """,
                (batch_id,),
            ).fetchall()
            conn.commit()
            return ClaimedDetectBatch(
                batch_id=batch_id,
                claim_token=claim_token,
                worker_slot=int(batch[2]),
                items=[
                    {
                        "photo_asset_id": int(row[0]),
                        "source_root": str(row[1]),
                        "primary_path": str(row[2]),
                        "item_order": int(row[3]),
                    }
                    for row in rows
                ],
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ack_detect_batch(
        self,
        *,
        batch_id: int,
        claim_token: str,
        worker_payload: dict[str, object],
    ) -> None:
        results = worker_payload.get("results")
        if not isinstance(results, list) or not results:
            raise ValueError("worker_payload.results 不能为空")

        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            batch = conn.execute(
                """
                SELECT id
                FROM scan_batch
                WHERE id=? AND claim_token=? AND status='running'
                """,
                (batch_id, claim_token),
            ).fetchone()
            if batch is None:
                raise ValueError("批次不存在或 claim_token 不匹配")

            expected_rows = conn.execute(
                """
                SELECT photo_asset_id
                FROM scan_batch_item
                WHERE scan_batch_id=?
                ORDER BY item_order ASC
                """,
                (batch_id,),
            ).fetchall()
            expected_ids = [int(row[0]) for row in expected_rows]
            if not expected_ids:
                raise ValueError("批次不存在条目")

            seen_ids: set[int] = set()
            payload_ids: list[int] = []
            for result in results:
                if not isinstance(result, dict):
                    raise ValueError("worker_payload.results[] 必须为对象")
                photo_asset_id = int(result["photo_asset_id"])
                if photo_asset_id in seen_ids:
                    raise ValueError(f"worker_payload 存在重复 photo_asset_id: {photo_asset_id}")
                seen_ids.add(photo_asset_id)
                payload_ids.append(photo_asset_id)
            expected_set = set(expected_ids)
            payload_set = set(payload_ids)
            if payload_set != expected_set:
                missing_ids = sorted(expected_set - payload_set)
                extra_ids = sorted(payload_set - expected_set)
                raise ValueError(
                    f"worker_payload 与 batch 条目不一致: missing={missing_ids}, extra={extra_ids}"
                )
            if len(payload_ids) != len(expected_ids):
                raise ValueError("worker_payload 条目数量与 batch 不一致")

            for result in results:
                photo_asset_id = int(result["photo_asset_id"])
                status = str(result.get("status", "failed"))
                error_message = None if result.get("error") is None else str(result.get("error"))

                updated = conn.execute(
                    """
                    UPDATE scan_batch_item
                    SET status = ?,
                        error_message = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE scan_batch_id = ? AND photo_asset_id = ?
                    """,
                    ("done" if status == "done" else "failed", error_message, batch_id, photo_asset_id),
                )
                if int(updated.rowcount) != 1:
                    raise ValueError(f"更新 scan_batch_item 失败: photo_asset_id={photo_asset_id}")

                if status != "done":
                    continue
                faces = result.get("faces")
                if not isinstance(faces, list):
                    raise ValueError("done 结果必须携带 faces 列表")
                self._replace_face_observations(conn, photo_asset_id=photo_asset_id, faces=faces)

            terminal = conn.execute(
                """
                SELECT
                  COUNT(*) AS total,
                  SUM(CASE WHEN status IN ('done','failed') THEN 1 ELSE 0 END) AS terminal_count
                FROM scan_batch_item
                WHERE scan_batch_id=?
                """,
                (batch_id,),
            ).fetchone()
            if terminal is None or int(terminal[0]) <= 0 or int(terminal[1] or 0) != int(terminal[0]):
                raise ValueError("batch item 未全部进入终态，禁止 ack")

            conn.execute(
                """
                UPDATE scan_batch
                SET status='acked',
                    acked_at=CURRENT_TIMESTAMP,
                    error_message=NULL
                WHERE id=? AND claim_token=?
                """,
                (batch_id, claim_token),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def rollback_unacked_batches(
        self,
        *,
        scan_session_id: int,
        reason: str = "aborted",
        item_status: str = "pending",
    ) -> int:
        if item_status not in {"pending", "failed"}:
            raise ValueError(f"非法 item_status: {item_status}")
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT id
                FROM scan_batch
                WHERE scan_session_id=? AND stage='detect' AND status IN ('claimed','running')
                """,
                (scan_session_id,),
            ).fetchall()
            if not rows:
                conn.commit()
                return 0
            batch_ids = [int(row[0]) for row in rows]
            placeholders = ", ".join("?" for _ in batch_ids)
            conn.execute(
                f"""
                UPDATE scan_batch
                SET status='failed',
                    error_message=?,
                    acked_at=CURRENT_TIMESTAMP
                WHERE id IN ({placeholders})
                """,
                (reason, *batch_ids),
            )
            conn.execute(
                f"""
                UPDATE scan_batch_item
                SET status=?,
                    error_message=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE scan_batch_id IN ({placeholders}) AND status!='done'
                """,
                (item_status, reason, *batch_ids),
            )
            conn.commit()
            return len(batch_ids)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_detect_stage_done(self, *, scan_session_id: int) -> None:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT id, stage_status_json
                FROM scan_session_source
                WHERE scan_session_id=?
                """,
                (scan_session_id,),
            ).fetchall()
            for row in rows:
                stage_status = json.loads(str(row[1]))
                stage_status["detect"] = "done"
                conn.execute(
                    """
                    UPDATE scan_session_source
                    SET stage_status_json=?,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (json.dumps(stage_status, ensure_ascii=False, sort_keys=True), int(row[0])),
                )
            conn.execute(
                """
                INSERT INTO scan_checkpoint(scan_session_id, stage, cursor_json, processed_count, updated_at)
                VALUES (?, 'detect', '{}', 0, CURRENT_TIMESTAMP)
                ON CONFLICT(scan_session_id, stage)
                DO UPDATE SET
                  cursor_json=excluded.cursor_json,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (scan_session_id,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _replace_face_observations(self, conn: sqlite3.Connection, *, photo_asset_id: int, faces: list[object]) -> None:
        conn.execute(
            """
            UPDATE face_observation
            SET active=0,
                inactive_reason='re_detect_replaced',
                pending_reassign=1,
                updated_at=CURRENT_TIMESTAMP
            WHERE photo_asset_id=? AND active=1
            """,
            (photo_asset_id,),
        )

        for face_index, face_obj in enumerate(faces):
            if not isinstance(face_obj, dict):
                raise ValueError("faces[] 必须为对象")
            bbox_obj = face_obj.get("bbox")
            if not isinstance(bbox_obj, list) or len(bbox_obj) != 4:
                raise ValueError("faces[].bbox 格式非法")
            x1, y1, x2, y2 = [float(v) for v in bbox_obj]
            det_conf = float(face_obj["detector_confidence"])
            area_ratio = float(face_obj["face_area_ratio"])
            magface_quality = float(face_obj.get("magface_quality", 1.0 + area_ratio + det_conf))
            quality_score = float(face_obj.get("quality_score", magface_quality * max(0.05, det_conf)))

            conn.execute(
                """
                INSERT INTO face_observation(
                  photo_asset_id, face_index, crop_relpath, aligned_relpath, context_relpath,
                  bbox_x1, bbox_y1, bbox_x2, bbox_y2,
                  detector_confidence, face_area_ratio, magface_quality, quality_score,
                  active, inactive_reason, pending_reassign, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(photo_asset_id, face_index)
                DO UPDATE SET
                  crop_relpath=excluded.crop_relpath,
                  aligned_relpath=excluded.aligned_relpath,
                  context_relpath=excluded.context_relpath,
                  bbox_x1=excluded.bbox_x1,
                  bbox_y1=excluded.bbox_y1,
                  bbox_x2=excluded.bbox_x2,
                  bbox_y2=excluded.bbox_y2,
                  detector_confidence=excluded.detector_confidence,
                  face_area_ratio=excluded.face_area_ratio,
                  magface_quality=excluded.magface_quality,
                  quality_score=excluded.quality_score,
                  active=1,
                  inactive_reason=NULL,
                  pending_reassign=0,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (
                    photo_asset_id,
                    face_index,
                    str(face_obj["crop_relpath"]),
                    str(face_obj["aligned_relpath"]),
                    str(face_obj["context_relpath"]),
                    x1,
                    y1,
                    x2,
                    y2,
                    det_conf,
                    area_ratio,
                    magface_quality,
                    quality_score,
                ),
            )


def _split_items_evenly(items: list[int], workers: int) -> list[list[int]]:
    safe_workers = max(1, int(workers))
    result: list[list[int]] = [[] for _ in range(safe_workers)]
    for idx, item in enumerate(items):
        result[idx % safe_workers].append(item)
    return result
