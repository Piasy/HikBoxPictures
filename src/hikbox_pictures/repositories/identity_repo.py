from __future__ import annotations

from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]


class IdentityRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get_profile(self, profile_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM identity_threshold_profile
            WHERE id = ?
            """,
            (int(profile_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_profile_required(self, profile_id: int) -> dict[str, Any]:
        profile = self.get_profile(profile_id)
        if profile is None:
            raise ValueError(f"threshold profile 不存在: {int(profile_id)}")
        return profile

    def get_active_profile(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM identity_threshold_profile
            WHERE active = 1
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row is not None else None

    def list_profiles(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT *
            FROM identity_threshold_profile
            ORDER BY id ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def list_profile_columns(self) -> list[str]:
        rows = self.conn.execute("PRAGMA table_info(identity_threshold_profile)").fetchall()
        return [str(row["name"]) for row in rows]

    def insert_profile(self, values: dict[str, Any]) -> int:
        if not values:
            raise ValueError("identity_threshold_profile 写入不能为空")
        columns = sorted(values.keys())
        placeholders = ", ".join("?" for _ in columns)
        sql = f"""
            INSERT INTO identity_threshold_profile({", ".join(columns)})
            VALUES ({placeholders})
        """
        params = tuple(values[col] for col in columns)
        cursor = self.conn.execute(sql, params)
        return int(cursor.lastrowid)

    def activate_profile_transactional(self, profile_id: int) -> int:
        profile_exists = self.conn.execute(
            """
            SELECT 1
            FROM identity_threshold_profile
            WHERE id = ?
            LIMIT 1
            """,
            (int(profile_id),),
        ).fetchone()
        if profile_exists is None:
            raise ValueError(f"目标 profile 不存在，无法激活：{int(profile_id)}")

        self.conn.execute(
            """
            UPDATE identity_threshold_profile
            SET active = 0,
                updated_at = CURRENT_TIMESTAMP
            WHERE active = 1
            """
        )
        cursor = self.conn.execute(
            """
            UPDATE identity_threshold_profile
            SET active = 1,
                activated_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (int(profile_id),),
        )
        return int(cursor.rowcount)

    def detect_workspace_embedding_binding(self) -> dict[str, str] | None:
        rows = self.conn.execute(
            """
            SELECT DISTINCT fe.feature_type, fe.model_key
            FROM face_embedding AS fe
            WHERE fe.feature_type IS NOT NULL
              AND fe.model_key IS NOT NULL
            ORDER BY fe.feature_type ASC, fe.model_key ASC
            """
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise ValueError("embedding 绑定不唯一，当前 workspace 存在多组 feature/model 组合。")
        row = rows[0]
        return {
            "embedding_feature_type": str(row["feature_type"]),
            "embedding_model_key": str(row["model_key"]),
            "embedding_distance_metric": "cosine",
            "embedding_schema_version": "face_embedding.v1",
        }

    def update_profile_quality_quantiles(
        self,
        *,
        profile_id: int,
        area_log_p10: float,
        area_log_p90: float,
        sharpness_log_p10: float,
        sharpness_log_p90: float,
    ) -> int:
        cursor = self.conn.execute(
            """
            UPDATE identity_threshold_profile
            SET area_log_p10 = ?,
                area_log_p90 = ?,
                sharpness_log_p10 = ?,
                sharpness_log_p90 = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                float(area_log_p10),
                float(area_log_p90),
                float(sharpness_log_p10),
                float(sharpness_log_p90),
                int(profile_id),
            ),
        )
        return int(cursor.rowcount)
