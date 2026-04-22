"""扫描断点服务。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from hikbox_pictures.product.db.connection import connect_sqlite


@dataclass(frozen=True)
class ScanCheckpointRecord:
    scan_session_id: int
    stage: str
    cursor: dict[str, object]
    processed_count: int
    updated_at: str


class ScanCheckpointService:
    """读写 scan_checkpoint 表。"""

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)

    def upsert_checkpoint(
        self,
        *,
        scan_session_id: int,
        stage: str,
        cursor: dict[str, object],
        processed_count: int,
    ) -> None:
        conn = connect_sqlite(self._db_path)
        try:
            conn.execute(
                """
                INSERT INTO scan_checkpoint(
                    scan_session_id,
                    stage,
                    cursor_json,
                    processed_count,
                    updated_at
                ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(scan_session_id, stage) DO UPDATE SET
                    cursor_json = excluded.cursor_json,
                    processed_count = excluded.processed_count,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (scan_session_id, stage, json.dumps(cursor, ensure_ascii=False), processed_count),
            )
            conn.commit()
        finally:
            conn.close()

    def get_checkpoint(self, *, scan_session_id: int, stage: str) -> ScanCheckpointRecord | None:
        conn = connect_sqlite(self._db_path)
        try:
            row = conn.execute(
                """
                SELECT scan_session_id, stage, cursor_json, processed_count, updated_at
                FROM scan_checkpoint
                WHERE scan_session_id = ? AND stage = ?
                """,
                (scan_session_id, stage),
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            return None
        return ScanCheckpointRecord(
            scan_session_id=int(row[0]),
            stage=str(row[1]),
            cursor=json.loads(str(row[2])),
            processed_count=int(row[3]),
            updated_at=str(row[4]),
        )
