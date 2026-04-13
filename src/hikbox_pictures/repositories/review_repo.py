from __future__ import annotations

import json
from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]


class ReviewRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create_review_item(
        self,
        review_type: str,
        payload_json: str,
        priority: int = 0,
        status: str = "open",
        primary_person_id: int | None = None,
        secondary_person_id: int | None = None,
        face_observation_id: int | None = None,
    ) -> int:
        try:
            json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise ValueError("payload_json 必须是合法 JSON 字符串") from exc

        cursor = self.conn.execute(
            """
            INSERT INTO review_item(
                review_type,
                primary_person_id,
                secondary_person_id,
                face_observation_id,
                payload_json,
                priority,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                review_type,
                primary_person_id,
                secondary_person_id,
                face_observation_id,
                payload_json,
                int(priority),
                status,
            ),
        )
        return int(cursor.lastrowid)

    def list_open_items(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT id, review_type, payload_json, priority, status,
                   primary_person_id, secondary_person_id, face_observation_id,
                   created_at, resolved_at
            FROM review_item
            WHERE status = 'open'
            ORDER BY priority DESC, id ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def get_item(self, review_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT id, review_type, payload_json, priority, status,
                   primary_person_id, secondary_person_id, face_observation_id,
                   created_at, resolved_at
            FROM review_item
            WHERE id = ?
            """,
            (int(review_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def dismiss_item(self, review_id: int) -> int:
        cursor = self.conn.execute(
            """
            UPDATE review_item
            SET status = 'dismissed',
                resolved_at = COALESCE(resolved_at, CURRENT_TIMESTAMP)
            WHERE id = ?
            """,
            (int(review_id),),
        )
        return int(cursor.rowcount)

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM review_item").fetchone()
        return int(row["c"])
