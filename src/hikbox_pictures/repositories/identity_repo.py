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

    def list_high_quality_observations(
        self,
        *,
        model_key: str,
        min_quality: float,
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT fo.id AS observation_id,
                   fo.photo_asset_id,
                   COALESCE(fo.quality_score, 0.0) AS quality_score,
                   fe.vector_blob
            FROM face_observation AS fo
            JOIN face_embedding AS fe
              ON fe.face_observation_id = fo.id
             AND fe.feature_type = 'face'
             AND fe.model_key = ?
             AND fe.normalized = 1
            WHERE fo.active = 1
              AND COALESCE(fo.quality_score, 0.0) >= ?
            ORDER BY fo.id ASC
            """,
            (str(model_key), float(min_quality)),
        ).fetchall()
        return [dict(row) for row in rows]

    def create_bootstrap_batch(
        self,
        *,
        model_key: str,
        threshold_profile_id: int,
        algorithm_version: str,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO auto_cluster_batch(
                model_key,
                algorithm_version,
                batch_type,
                threshold_profile_id,
                scan_session_id
            )
            VALUES (?, ?, 'bootstrap', ?, NULL)
            """,
            (str(model_key), str(algorithm_version), int(threshold_profile_id)),
        )
        return int(cursor.lastrowid)

    def create_cluster(
        self,
        *,
        batch_id: int,
        representative_observation_id: int,
        cluster_status: str,
        resolved_person_id: int | None,
        diagnostic_json: str,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO auto_cluster(
                batch_id,
                representative_observation_id,
                cluster_status,
                resolved_person_id,
                diagnostic_json
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                int(batch_id),
                int(representative_observation_id),
                str(cluster_status),
                int(resolved_person_id) if resolved_person_id is not None else None,
                str(diagnostic_json),
            ),
        )
        return int(cursor.lastrowid)

    def add_cluster_member(
        self,
        *,
        cluster_id: int,
        face_observation_id: int,
        membership_score: float | None,
        quality_score_snapshot: float,
        is_seed_candidate: bool,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO auto_cluster_member(
                cluster_id,
                face_observation_id,
                membership_score,
                quality_score_snapshot,
                is_seed_candidate
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                int(cluster_id),
                int(face_observation_id),
                float(membership_score) if membership_score is not None else None,
                float(quality_score_snapshot),
                1 if is_seed_candidate else 0,
            ),
        )
        return int(cursor.lastrowid)

    def update_cluster_resolution(
        self,
        *,
        cluster_id: int,
        cluster_status: str,
        resolved_person_id: int | None,
        diagnostic_json: str,
    ) -> int:
        cursor = self.conn.execute(
            """
            UPDATE auto_cluster
            SET cluster_status = ?,
                resolved_person_id = ?,
                diagnostic_json = ?
            WHERE id = ?
            """,
            (
                str(cluster_status),
                int(resolved_person_id) if resolved_person_id is not None else None,
                str(diagnostic_json),
                int(cluster_id),
            ),
        )
        return int(cursor.rowcount)

    def get_cluster_status(self, cluster_id: int) -> str | None:
        row = self.conn.execute(
            """
            SELECT cluster_status
            FROM auto_cluster
            WHERE id = ?
            """,
            (int(cluster_id),),
        ).fetchone()
        if row is None:
            return None
        return str(row["cluster_status"])
