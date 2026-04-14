from __future__ import annotations

from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]


class AssetRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add_photo_asset(
        self,
        library_source_id: int,
        primary_path: str,
        processing_status: str = "discovered",
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO photo_asset(library_source_id, primary_path, processing_status)
            VALUES (?, ?, ?)
            """,
            (int(library_source_id), primary_path, processing_status),
        )
        return int(cursor.lastrowid)

    def upsert_photo_asset_from_scan(
        self,
        *,
        library_source_id: int,
        primary_path: str,
        is_heic: bool,
        live_mov_path: str | None,
    ) -> tuple[int, bool]:
        insert_cursor = self.conn.execute(
            """
            INSERT OR IGNORE INTO photo_asset(
                library_source_id,
                primary_path,
                is_heic,
                live_mov_path,
                processing_status
            )
            VALUES (?, ?, ?, ?, 'discovered')
            """,
            (
                int(library_source_id),
                primary_path,
                1 if is_heic else 0,
                live_mov_path,
            ),
        )
        created = int(insert_cursor.rowcount) > 0
        if not created:
            self.conn.execute(
                """
                UPDATE photo_asset
                SET is_heic = ?,
                    live_mov_path = COALESCE(?, live_mov_path),
                    updated_at = CURRENT_TIMESTAMP
                WHERE library_source_id = ?
                  AND primary_path = ?
                """,
                (
                    1 if is_heic else 0,
                    live_mov_path,
                    int(library_source_id),
                    primary_path,
                ),
            )

        row = self.conn.execute(
            """
            SELECT id
            FROM photo_asset
            WHERE library_source_id = ?
              AND primary_path = ?
            LIMIT 1
            """,
            (int(library_source_id), primary_path),
        ).fetchone()
        if row is None:
            raise RuntimeError("photo_asset upsert 失败，未找到对应记录")
        return int(row["id"]), created

    def get_asset(self, asset_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT id, library_source_id, primary_path, processing_status,
                   capture_datetime, capture_month, created_at, updated_at
            FROM photo_asset
            WHERE id = ?
            """,
            (int(asset_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_photo_media(self, photo_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT id, library_source_id, primary_path, primary_fingerprint, live_mov_path, live_mov_fingerprint,
                   is_heic, processing_status, capture_datetime, capture_month, created_at, updated_at
            FROM photo_asset
            WHERE id = ?
            """,
            (int(photo_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_observation_media(self, observation_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT fo.id,
                   fo.photo_asset_id,
                   fo.crop_path,
                   fo.active,
                   pa.library_source_id,
                   pa.primary_path
            FROM face_observation AS fo
            JOIN photo_asset AS pa
              ON pa.id = fo.photo_asset_id
            WHERE fo.id = ?
            """,
            (int(observation_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_observation_with_source(self, observation_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT fo.id,
                   fo.photo_asset_id,
                   fo.bbox_top,
                   fo.bbox_right,
                   fo.bbox_bottom,
                   fo.bbox_left,
                   fo.crop_path,
                   fo.active,
                   pa.library_source_id,
                   pa.primary_path
            FROM face_observation AS fo
            JOIN photo_asset AS pa
              ON pa.id = fo.photo_asset_id
            WHERE fo.id = ?
            """,
            (int(observation_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def update_observation_crop_path(self, observation_id: int, crop_path: str) -> int:
        cursor = self.conn.execute(
            """
            UPDATE face_observation
            SET crop_path = ?
            WHERE id = ?
            """,
            (crop_path, int(observation_id)),
        )
        return int(cursor.rowcount)

    def list_assets_for_source(self, library_source_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT id, library_source_id, primary_path, processing_status,
                   capture_datetime, capture_month, created_at, updated_at
            FROM photo_asset
            WHERE library_source_id = ?
            ORDER BY id ASC
            """,
            (int(library_source_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_assets_for_source_with_status(self, library_source_id: int, processing_status: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT id, library_source_id, primary_path, processing_status,
                   capture_datetime, capture_month, created_at, updated_at
            FROM photo_asset
            WHERE library_source_id = ?
              AND processing_status = ?
            ORDER BY id ASC
            """,
            (int(library_source_id), processing_status),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_metadata_done_if_current(
        self,
        asset_id: int,
        *,
        expected_status: str = "discovered",
        capture_datetime: str | None,
        capture_month: str | None,
        last_processed_session_id: int | None,
    ) -> int:
        cursor = self.conn.execute(
            """
            UPDATE photo_asset
            SET capture_datetime = ?,
                capture_month = ?,
                processing_status = 'metadata_done',
                last_processed_session_id = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND processing_status = ?
            """,
            (
                capture_datetime,
                capture_month,
                last_processed_session_id,
                int(asset_id),
                expected_status,
            ),
        )
        return int(cursor.rowcount)

    def mark_stage_done_if_current(
        self,
        asset_id: int,
        *,
        from_status: str,
        to_status: str,
        last_processed_session_id: int | None,
    ) -> int:
        cursor = self.conn.execute(
            """
            UPDATE photo_asset
            SET processing_status = ?,
                last_processed_session_id = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND processing_status = ?
            """,
            (
                to_status,
                last_processed_session_id,
                int(asset_id),
                from_status,
            ),
        )
        return int(cursor.rowcount)

    def count_assets_for_source(self, library_source_id: int) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM photo_asset
            WHERE library_source_id = ?
            """,
            (int(library_source_id),),
        ).fetchone()
        return int(row["c"])

    def count_assets_for_source_with_statuses(self, library_source_id: int, statuses: tuple[str, ...]) -> int:
        if not statuses:
            return 0
        placeholders = ", ".join("?" for _ in statuses)
        params: tuple[Any, ...] = (int(library_source_id),) + tuple(statuses)
        row = self.conn.execute(
            f"""
            SELECT COUNT(*) AS c
            FROM photo_asset
            WHERE library_source_id = ?
              AND processing_status IN ({placeholders})
            """,
            params,
        ).fetchone()
        return int(row["c"])

    def list_active_face_observation_ids(self, asset_id: int) -> list[int]:
        rows = self.conn.execute(
            """
            SELECT id
            FROM face_observation
            WHERE photo_asset_id = ?
              AND active = 1
            ORDER BY id ASC
            """,
            (int(asset_id),),
        ).fetchall()
        return [int(row["id"]) for row in rows]

    def list_active_face_observations(self, asset_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT id,
                   photo_asset_id,
                   bbox_top,
                   bbox_right,
                   bbox_bottom,
                   bbox_left,
                   face_area_ratio,
                   crop_path,
                   detector_key,
                   detector_version,
                   active
            FROM face_observation
            WHERE photo_asset_id = ?
              AND active = 1
            ORDER BY id ASC
            """,
            (int(asset_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    def ensure_face_observation(
        self,
        asset_id: int,
        *,
        bbox_top: float = 0.0,
        bbox_right: float = 1.0,
        bbox_bottom: float = 1.0,
        bbox_left: float = 0.0,
    ) -> int:
        row = self.conn.execute(
            """
            SELECT id
            FROM face_observation
            WHERE photo_asset_id = ?
              AND active = 1
            ORDER BY id ASC
            LIMIT 1
            """,
            (int(asset_id),),
        ).fetchone()
        if row is not None:
            return int(row["id"])

        cursor = self.conn.execute(
            """
            INSERT INTO face_observation(photo_asset_id, bbox_top, bbox_right, bbox_bottom, bbox_left, active)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (int(asset_id), float(bbox_top), float(bbox_right), float(bbox_bottom), float(bbox_left)),
        )
        return int(cursor.lastrowid)

    def replace_face_observations(
        self,
        asset_id: int,
        *,
        observations: list[dict[str, Any]],
        detector_key: str,
        detector_version: str,
    ) -> list[int]:
        self.conn.execute(
            """
            UPDATE face_observation
            SET active = 0
            WHERE photo_asset_id = ?
              AND active = 1
            """,
            (int(asset_id),),
        )

        created_ids: list[int] = []
        for observation in observations:
            cursor = self.conn.execute(
                """
                INSERT INTO face_observation(
                    photo_asset_id,
                    bbox_top,
                    bbox_right,
                    bbox_bottom,
                    bbox_left,
                    face_area_ratio,
                    crop_path,
                    detector_key,
                    detector_version,
                    active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    int(asset_id),
                    float(observation["bbox_top"]),
                    float(observation["bbox_right"]),
                    float(observation["bbox_bottom"]),
                    float(observation["bbox_left"]),
                    observation.get("face_area_ratio"),
                    observation.get("crop_path"),
                    detector_key,
                    detector_version,
                ),
            )
            created_ids.append(int(cursor.lastrowid))
        return created_ids

    def ensure_face_embedding(
        self,
        face_observation_id: int,
        *,
        vector_blob: bytes,
        model_key: str = "pipeline-stub-v1",
        feature_type: str = "face",
        normalized: int = 1,
        dimension: int = 4,
    ) -> int:
        self.conn.execute(
            """
            INSERT INTO face_embedding(
                face_observation_id,
                feature_type,
                model_key,
                dimension,
                vector_blob,
                normalized
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(face_observation_id, feature_type)
            DO UPDATE SET
                model_key = excluded.model_key,
                dimension = excluded.dimension,
                vector_blob = excluded.vector_blob,
                normalized = excluded.normalized,
                generated_at = CURRENT_TIMESTAMP
            """,
            (
                int(face_observation_id),
                feature_type,
                model_key,
                int(dimension),
                vector_blob,
                int(normalized),
            ),
        )
        row = self.conn.execute(
            """
            SELECT id
            FROM face_embedding
            WHERE face_observation_id = ?
              AND feature_type = ?
            LIMIT 1
            """,
            (int(face_observation_id), feature_type),
        ).fetchone()
        if row is None:
            raise RuntimeError("face_embedding 写入失败，未找到对应记录")
        return int(row["id"])

    def get_face_embedding(
        self,
        face_observation_id: int,
        *,
        feature_type: str = "face",
        model_key: str | None = None,
    ) -> dict[str, Any] | None:
        sql = """
            SELECT id,
                   face_observation_id,
                   feature_type,
                   model_key,
                   dimension,
                   vector_blob,
                   normalized,
                   generated_at
            FROM face_embedding
            WHERE face_observation_id = ?
              AND feature_type = ?
        """
        params: list[Any] = [int(face_observation_id), feature_type]
        if model_key is not None:
            sql += " AND model_key = ?"
            params.append(str(model_key))
        sql += " ORDER BY id ASC LIMIT 1"
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row is not None else None

    def get_assignment(self, assignment_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT id, person_id, face_observation_id, assignment_source, confidence,
                   locked, confirmed_at, active, created_at, updated_at
            FROM person_face_assignment
            WHERE id = ?
            """,
            (int(assignment_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_active_assignment_for_observation(self, face_observation_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT id, person_id, face_observation_id, assignment_source, confidence,
                   locked, confirmed_at, active, created_at, updated_at
            FROM person_face_assignment
            WHERE face_observation_id = ?
              AND active = 1
            LIMIT 1
            """,
            (int(face_observation_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def create_assignment(
        self,
        *,
        person_id: int,
        face_observation_id: int,
        assignment_source: str,
        confidence: float | None,
        locked: bool = False,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO person_face_assignment(
                person_id,
                face_observation_id,
                assignment_source,
                confidence,
                locked,
                active
            )
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (
                int(person_id),
                int(face_observation_id),
                assignment_source,
                confidence,
                1 if locked else 0,
            ),
        )
        return int(cursor.lastrowid)

    def update_assignment(
        self,
        assignment_id: int,
        *,
        person_id: int,
        assignment_source: str,
        confidence: float | None,
    ) -> int:
        cursor = self.conn.execute(
            """
            UPDATE person_face_assignment
            SET person_id = ?,
                assignment_source = ?,
                confidence = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND active = 1
              AND locked = 0
            """,
            (
                int(person_id),
                assignment_source,
                confidence,
                int(assignment_id),
            ),
        )
        return int(cursor.rowcount)

    def move_assignment(
        self,
        assignment_id: int,
        *,
        from_person_id: int,
        to_person_id: int,
        assignment_source: str,
    ) -> int:
        cursor = self.conn.execute(
            """
            UPDATE person_face_assignment
            SET person_id = ?,
                assignment_source = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND person_id = ?
              AND active = 1
            """,
            (
                int(to_person_id),
                assignment_source,
                int(assignment_id),
                int(from_person_id),
            ),
        )
        return int(cursor.rowcount)

    def move_active_assignments_for_person(
        self,
        *,
        from_person_id: int,
        to_person_id: int,
        assignment_source: str,
    ) -> int:
        cursor = self.conn.execute(
            """
            UPDATE person_face_assignment
            SET person_id = ?,
                assignment_source = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE person_id = ?
              AND active = 1
            """,
            (
                int(to_person_id),
                assignment_source,
                int(from_person_id),
            ),
        )
        return int(cursor.rowcount)

    def lock_assignment(self, assignment_id: int, *, person_id: int) -> int:
        cursor = self.conn.execute(
            """
            UPDATE person_face_assignment
            SET locked = 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND person_id = ?
              AND active = 1
            """,
            (
                int(assignment_id),
                int(person_id),
            ),
        )
        return int(cursor.rowcount)

    def reassign_if_unlocked(self, assignment_id: int, *, candidate_person_id: int) -> int:
        cursor = self.conn.execute(
            """
            UPDATE person_face_assignment
            SET person_id = ?,
                assignment_source = 'auto',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND active = 1
              AND locked = 0
            """,
            (
                int(candidate_person_id),
                int(assignment_id),
            ),
        )
        return int(cursor.rowcount)

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM photo_asset").fetchone()
        return int(row["c"])
