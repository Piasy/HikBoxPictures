from __future__ import annotations

import json
from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]


class IdentityClusterRunRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get_active_cluster_profile(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM identity_cluster_profile
            WHERE active = 1
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row is not None else None

    def insert_run(
        self,
        *,
        observation_snapshot_id: int,
        cluster_profile_id: int,
        algorithm_version: str,
        run_status: str,
        supersedes_run_id: int | None,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO identity_cluster_run(
                observation_snapshot_id,
                cluster_profile_id,
                algorithm_version,
                run_status,
                summary_json,
                failure_json,
                supersedes_run_id
            )
            VALUES (?, ?, ?, ?, '{}', '{}', ?)
            """,
            (
                int(observation_snapshot_id),
                int(cluster_profile_id),
                str(algorithm_version),
                str(run_status),
                int(supersedes_run_id) if supersedes_run_id is not None else None,
            ),
        )
        return int(cursor.lastrowid)

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM identity_cluster_run
            WHERE id = ?
            """,
            (int(run_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_run_required(self, run_id: int) -> dict[str, Any]:
        row = self.get_run(run_id)
        if row is None:
            raise ValueError(f"cluster run 不存在: {int(run_id)}")
        return row

    def update_run_status(
        self,
        *,
        run_id: int,
        run_status: str,
        summary_json: dict[str, Any] | None,
        failure_json: dict[str, Any] | None,
        expected_statuses: tuple[str, ...] | None = None,
    ) -> bool:
        params: list[Any] = [
            str(run_status),
            json.dumps(summary_json or {}, ensure_ascii=False, sort_keys=True),
            json.dumps(failure_json or {}, ensure_ascii=False, sort_keys=True),
            str(run_status),
            str(run_status),
            int(run_id),
        ]
        sql = """
            UPDATE identity_cluster_run
            SET run_status = ?,
                summary_json = ?,
                failure_json = ?,
                started_at = CASE
                    WHEN ? = 'running' AND started_at IS NULL THEN CURRENT_TIMESTAMP
                    ELSE started_at
                END,
                finished_at = CASE
                    WHEN ? IN ('succeeded', 'failed', 'cancelled') THEN CURRENT_TIMESTAMP
                    ELSE finished_at
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """
        if expected_statuses:
            placeholders = ", ".join("?" for _ in expected_statuses)
            sql += f" AND run_status IN ({placeholders})"
            params.extend(str(status) for status in expected_statuses)
        cursor = self.conn.execute(sql, tuple(params))
        return int(cursor.rowcount) == 1

    def exists_review_target(self) -> bool:
        row = self.conn.execute(
            """
            SELECT 1
            FROM identity_cluster_run
            WHERE is_review_target = 1
            LIMIT 1
            """
        ).fetchone()
        return row is not None

    def clear_review_target(self) -> None:
        self.conn.execute(
            """
            UPDATE identity_cluster_run
            SET is_review_target = 0,
                review_selected_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE is_review_target = 1
            """
        )

    def set_review_target(
        self,
        *,
        run_id: int,
        review_selected_at: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE identity_cluster_run
            SET is_review_target = 1,
                review_selected_at = COALESCE(?, CURRENT_TIMESTAMP),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (str(review_selected_at) if review_selected_at is not None else None, int(run_id)),
        )
