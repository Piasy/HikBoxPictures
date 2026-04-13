from __future__ import annotations

from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]


class ExportRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def create_template(
        self,
        name: str,
        output_root: str,
        include_group: bool = True,
        export_live_mov: bool = False,
        enabled: bool = True,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO export_template(name, output_root, include_group, export_live_mov, enabled)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                name,
                output_root,
                1 if include_group else 0,
                1 if export_live_mov else 0,
                1 if enabled else 0,
            ),
        )
        return int(cursor.lastrowid)

    def add_template_person(self, template_id: int, person_id: int, position: int) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO export_template_person(template_id, person_id, position)
            VALUES (?, ?, ?)
            """,
            (int(template_id), int(person_id), int(position)),
        )
        return int(cursor.lastrowid)

    def list_templates(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT id, name, output_root, include_group, export_live_mov,
                   start_datetime, end_datetime, enabled, created_at, updated_at
            FROM export_template
            ORDER BY id ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def count_templates(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM export_template").fetchone()
        return int(row["c"])

    def get_template(self, template_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT id, name, output_root, include_group, export_live_mov,
                   start_datetime, end_datetime, enabled, created_at, updated_at
            FROM export_template
            WHERE id = ?
            """,
            (int(template_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_template_person_ids(self, template_id: int) -> list[int]:
        rows = self.conn.execute(
            """
            SELECT person_id
            FROM export_template_person
            WHERE template_id = ?
            ORDER BY position ASC, id ASC
            """,
            (int(template_id),),
        ).fetchall()
        return [int(row["person_id"]) for row in rows]

    def update_template_include_group(self, template_id: int, include_group: bool) -> int:
        cursor = self.conn.execute(
            """
            UPDATE export_template
            SET include_group = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (1 if include_group else 0, int(template_id)),
        )
        return int(cursor.rowcount)

    def list_assets_with_faces(
        self,
        *,
        start_datetime: str | None = None,
        end_datetime: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT pa.id AS photo_asset_id,
                   pa.primary_path,
                   pa.primary_fingerprint,
                   pa.live_mov_path,
                   pa.live_mov_fingerprint,
                   pa.capture_datetime,
                   pa.capture_month,
                   fo.id AS face_observation_id,
                   fo.face_area_ratio,
                   pfa.person_id
            FROM photo_asset AS pa
            LEFT JOIN face_observation AS fo
              ON fo.photo_asset_id = pa.id
             AND fo.active = 1
            LEFT JOIN person_face_assignment AS pfa
              ON pfa.face_observation_id = fo.id
             AND pfa.active = 1
            WHERE (? IS NULL OR pa.capture_datetime >= ?)
              AND (? IS NULL OR pa.capture_datetime <= ?)
            ORDER BY pa.id ASC, fo.id ASC, pfa.id ASC
            """,
            (start_datetime, start_datetime, end_datetime, end_datetime),
        ).fetchall()
        return [dict(row) for row in rows]

    def create_export_run(self, template_id: int, spec_hash: str, *, status: str = "running") -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO export_run(template_id, spec_hash, status, started_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (int(template_id), spec_hash, status),
        )
        return int(cursor.lastrowid)

    def finish_export_run(
        self,
        export_run_id: int,
        *,
        status: str,
        matched_only_count: int,
        matched_group_count: int,
        exported_count: int,
        skipped_count: int,
        failed_count: int,
    ) -> int:
        cursor = self.conn.execute(
            """
            UPDATE export_run
            SET status = ?,
                matched_only_count = ?,
                matched_group_count = ?,
                exported_count = ?,
                skipped_count = ?,
                failed_count = ?,
                finished_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                status,
                int(matched_only_count),
                int(matched_group_count),
                int(exported_count),
                int(skipped_count),
                int(failed_count),
                int(export_run_id),
            ),
        )
        return int(cursor.rowcount)

    def mark_other_spec_deliveries_stale(self, *, template_id: int, spec_hash: str) -> int:
        cursor = self.conn.execute(
            """
            UPDATE export_delivery
            SET status = 'stale',
                last_verified_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE template_id = ?
              AND spec_hash <> ?
              AND status <> 'stale'
            """,
            (int(template_id), spec_hash),
        )
        return int(cursor.rowcount)

    def get_delivery(
        self,
        *,
        template_id: int,
        spec_hash: str,
        photo_asset_id: int,
        asset_variant: str,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT id, template_id, spec_hash, photo_asset_id, asset_variant, bucket, target_path,
                   source_fingerprint, status, last_exported_at, last_verified_at, created_at, updated_at
            FROM export_delivery
            WHERE template_id = ?
              AND spec_hash = ?
              AND photo_asset_id = ?
              AND asset_variant = ?
            LIMIT 1
            """,
            (
                int(template_id),
                spec_hash,
                int(photo_asset_id),
                asset_variant,
            ),
        ).fetchone()
        return dict(row) if row is not None else None

    def upsert_delivery(
        self,
        *,
        template_id: int,
        spec_hash: str,
        photo_asset_id: int,
        asset_variant: str,
        bucket: str,
        target_path: str,
        source_fingerprint: str | None,
        status: str,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO export_delivery(
                template_id,
                spec_hash,
                photo_asset_id,
                asset_variant,
                bucket,
                target_path,
                source_fingerprint,
                status,
                last_exported_at,
                last_verified_at,
                updated_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?,
                CASE WHEN ? = 'ok' THEN CURRENT_TIMESTAMP ELSE NULL END,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            )
            ON CONFLICT(template_id, spec_hash, photo_asset_id, asset_variant)
            DO UPDATE SET
                bucket = excluded.bucket,
                target_path = excluded.target_path,
                source_fingerprint = excluded.source_fingerprint,
                status = excluded.status,
                last_exported_at = CASE
                    WHEN excluded.status = 'ok' THEN CURRENT_TIMESTAMP
                    ELSE export_delivery.last_exported_at
                END,
                last_verified_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                int(template_id),
                spec_hash,
                int(photo_asset_id),
                asset_variant,
                bucket,
                target_path,
                source_fingerprint,
                status,
                status,
            ),
        )
        return int(cursor.rowcount)

    def list_deliveries_for_spec(self, *, template_id: int, spec_hash: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT id, photo_asset_id, asset_variant, bucket, target_path, source_fingerprint, status
            FROM export_delivery
            WHERE template_id = ?
              AND spec_hash = ?
            ORDER BY id ASC
            """,
            (int(template_id), spec_hash),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_delivery_status(self, *, delivery_id: int, status: str) -> int:
        cursor = self.conn.execute(
            """
            UPDATE export_delivery
            SET status = ?,
                last_verified_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, int(delivery_id)),
        )
        return int(cursor.rowcount)

    def count_stale_deliveries(self, template_id: int) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM export_delivery
            WHERE template_id = ?
              AND status = 'stale'
            """,
            (int(template_id),),
        ).fetchone()
        return int(row["c"])
