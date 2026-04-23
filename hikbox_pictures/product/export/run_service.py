"""导出运行服务。"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from hikbox_pictures.product.db.connection import connect_sqlite
from hikbox_pictures.product.export.bucket_rules import FaceBucketInput, classify_bucket
from hikbox_pictures.product.export.template_service import ExportTemplateService


class ExportRunNotFoundError(Exception):
    """导出运行不存在。"""


class ExportRunningLockError(Exception):
    """存在运行中的导出任务，禁止人物归属/合并写操作。"""

    error_code = "EXPORT_RUNNING_LOCK"

    def __init__(self, export_run_id: int):
        super().__init__(f"存在运行中的导出任务，export_run_id={export_run_id}")
        self.export_run_id = int(export_run_id)


@dataclass(frozen=True)
class ExportRunRecord:
    export_run_id: int
    template_id: int
    status: str


@dataclass(frozen=True)
class ExportRunResult:
    export_run_id: int
    status: str
    exported_count: int
    skipped_exists_count: int
    failed_count: int


class ExportRunService:
    """导出运行创建与执行。"""

    def __init__(self, library_db_path: Path):
        self._library_db_path = Path(library_db_path)
        self._template_service = ExportTemplateService(self._library_db_path)

    def start_run(self, template_id: int) -> ExportRunRecord:
        template = self._template_service.get_template(int(template_id))
        conn = connect_sqlite(self._library_db_path)
        try:
            cursor = conn.execute(
                """
                INSERT INTO export_run(template_id, status, summary_json, started_at, finished_at)
                VALUES (?, 'running', ?, CURRENT_TIMESTAMP, NULL)
                """,
                (
                    template.id,
                    json.dumps(_empty_summary(), ensure_ascii=False, sort_keys=True),
                ),
            )
            conn.commit()
            return ExportRunRecord(export_run_id=int(cursor.lastrowid), template_id=template.id, status="running")
        finally:
            conn.close()

    def execute_run(self, export_run_id: int) -> ExportRunResult:
        conn = connect_sqlite(self._library_db_path)
        conn.row_factory = sqlite3.Row
        try:
            run_row = conn.execute(
                """
                SELECT id, template_id, status
                FROM export_run
                WHERE id=?
                """,
                (int(export_run_id),),
            ).fetchone()
            if run_row is None:
                raise ExportRunNotFoundError(f"导出运行不存在: {export_run_id}")
            template = self._template_service.get_template(int(run_row["template_id"]))
            summary = _empty_summary()

            asset_rows = conn.execute(
                """
                SELECT
                  p.id,
                  p.primary_path,
                  p.capture_datetime,
                  p.mtime_ns,
                  p.live_mov_path,
                  p.library_source_id,
                  s.root_path
                FROM photo_asset AS p
                INNER JOIN library_source AS s ON s.id = p.library_source_id
                WHERE p.asset_status='active'
                ORDER BY p.id ASC
                """
            ).fetchall()

            for asset_row in asset_rows:
                faces = self._load_asset_faces(conn, photo_asset_id=int(asset_row["id"]), selected_person_ids=template.person_ids)
                matched_person_ids = {face.assigned_person_id for face in faces if face.is_selected_person}
                if matched_person_ids != set(template.person_ids):
                    continue

                bucket = classify_bucket(faces)
                month_key = self._resolve_month_key(
                    capture_datetime=asset_row["capture_datetime"],
                    primary_path=asset_row["primary_path"],
                    source_root=asset_row["root_path"],
                    fallback_mtime_ns=asset_row["mtime_ns"],
                )
                target_dir = Path(template.output_root) / bucket / month_key
                source_photo_path = Path(str(asset_row["root_path"])) / str(asset_row["primary_path"])
                photo_destination = target_dir / Path(str(asset_row["primary_path"])).name
                photo_status = self._deliver_file(
                    conn,
                    export_run_id=int(run_row["id"]),
                    photo_asset_id=int(asset_row["id"]),
                    media_kind="photo",
                    bucket=bucket,
                    month_key=month_key,
                    source_path=source_photo_path,
                    destination_path=photo_destination,
                    skip_missing=False,
                )
                summary = _accumulate_summary(summary, photo_status)
                conn.commit()

                live_mov_raw = asset_row["live_mov_path"]
                if live_mov_raw:
                    mov_source_path = Path(str(asset_row["root_path"])) / str(live_mov_raw)
                    mov_destination = target_dir / Path(str(live_mov_raw)).name
                    mov_status = self._deliver_file(
                        conn,
                        export_run_id=int(run_row["id"]),
                        photo_asset_id=int(asset_row["id"]),
                        media_kind="live_mov",
                        bucket=bucket,
                        month_key=month_key,
                        source_path=mov_source_path,
                        destination_path=mov_destination,
                        skip_missing=True,
                    )
                    if mov_status is not None:
                        summary = _accumulate_summary(summary, mov_status)
                        conn.commit()

            final_status = "completed" if summary["failed_count"] == 0 else "failed"
            conn.execute(
                """
                UPDATE export_run
                SET status=?, summary_json=?, finished_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    final_status,
                    json.dumps(summary, ensure_ascii=False, sort_keys=True),
                    int(run_row["id"]),
                ),
            )
            conn.commit()
            return ExportRunResult(
                export_run_id=int(run_row["id"]),
                status=final_status,
                exported_count=int(summary["exported_count"]),
                skipped_exists_count=int(summary["skipped_exists_count"]),
                failed_count=int(summary["failed_count"]),
            )
        finally:
            conn.close()

    def _load_asset_faces(
        self,
        conn: sqlite3.Connection,
        *,
        photo_asset_id: int,
        selected_person_ids: list[int],
    ) -> list[FaceBucketInput]:
        rows = conn.execute(
            """
            SELECT
              f.id,
              f.bbox_x1,
              f.bbox_y1,
              f.bbox_x2,
              f.bbox_y2,
              a.person_id
            FROM face_observation AS f
            LEFT JOIN person_face_assignment AS a
              ON a.face_observation_id = f.id
             AND a.active = 1
            WHERE f.photo_asset_id = ?
              AND f.active = 1
            ORDER BY f.id ASC
            """,
            (int(photo_asset_id),),
        ).fetchall()
        return [
            FaceBucketInput(
                face_observation_id=int(row["id"]),
                area=_bbox_area(row["bbox_x1"], row["bbox_y1"], row["bbox_x2"], row["bbox_y2"]),
                assigned_person_id=None if row["person_id"] is None else int(row["person_id"]),
                is_selected_person=(row["person_id"] is not None and int(row["person_id"]) in set(selected_person_ids)),
            )
            for row in rows
        ]

    def _resolve_month_key(
        self,
        *,
        capture_datetime: str | None,
        primary_path: str,
        source_root: str,
        fallback_mtime_ns: int | None,
    ) -> str:
        if capture_datetime:
            return datetime.fromisoformat(str(capture_datetime)).strftime("%Y-%m")
        source_path = Path(str(source_root)) / str(primary_path)
        try:
            return datetime.fromtimestamp(source_path.stat().st_mtime).strftime("%Y-%m")
        except OSError:
            if fallback_mtime_ns is None:
                raise
            return datetime.fromtimestamp(int(fallback_mtime_ns) / 1_000_000_000).strftime("%Y-%m")

    def _deliver_file(
        self,
        conn: sqlite3.Connection,
        *,
        export_run_id: int,
        photo_asset_id: int,
        media_kind: str,
        bucket: str,
        month_key: str,
        source_path: Path,
        destination_path: Path,
        skip_missing: bool,
    ) -> str | None:
        if skip_missing and not source_path.exists():
            return None
        if skip_missing and not self._is_source_readable(source_path):
            return None
        if not skip_missing and not self._is_source_readable(source_path):
            self._insert_delivery(
                conn,
                export_run_id=export_run_id,
                photo_asset_id=photo_asset_id,
                media_kind=media_kind,
                bucket=bucket,
                month_key=month_key,
                destination_path=destination_path,
                delivery_status="failed",
                error_message=f"源文件不可读: {source_path}",
            )
            return "failed"
        if destination_path.exists():
            self._insert_delivery(
                conn,
                export_run_id=export_run_id,
                photo_asset_id=photo_asset_id,
                media_kind=media_kind,
                bucket=bucket,
                month_key=month_key,
                destination_path=destination_path,
                delivery_status="skipped_exists",
                error_message=None,
            )
            return "skipped_exists"

        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path)
        self._insert_delivery(
            conn,
            export_run_id=export_run_id,
            photo_asset_id=photo_asset_id,
            media_kind=media_kind,
            bucket=bucket,
            month_key=month_key,
            destination_path=destination_path,
            delivery_status="exported",
            error_message=None,
        )
        return "exported"

    def _is_source_readable(self, path: Path) -> bool:
        return is_path_readable(path)

    def _insert_delivery(
        self,
        conn: sqlite3.Connection,
        *,
        export_run_id: int,
        photo_asset_id: int,
        media_kind: str,
        bucket: str,
        month_key: str,
        destination_path: Path,
        delivery_status: str,
        error_message: str | None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO export_delivery(
              export_run_id, photo_asset_id, media_kind, bucket, month_key, destination_path,
              delivery_status, error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                int(export_run_id),
                int(photo_asset_id),
                str(media_kind),
                str(bucket),
                str(month_key),
                str(destination_path),
                str(delivery_status),
                error_message,
            ),
        )


def assert_people_writes_unlocked(conn: sqlite3.Connection) -> None:
    """导出运行中阻断人物归属/合并写入口。"""

    row = conn.execute(
        """
        SELECT id
        FROM export_run
        WHERE status='running'
        ORDER BY id ASC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return
    export_run_id = row[0] if not isinstance(row, sqlite3.Row) else row["id"]
    raise ExportRunningLockError(export_run_id=int(export_run_id))


def _bbox_area(x1: float, y1: float, x2: float, y2: float) -> float | None:
    width = float(x2) - float(x1)
    height = float(y2) - float(y1)
    if width <= 0 or height <= 0:
        return None
    return width * height


def _empty_summary() -> dict[str, int]:
    return {
        "exported_count": 0,
        "skipped_exists_count": 0,
        "failed_count": 0,
    }


def _accumulate_summary(summary: dict[str, int], delivery_status: str) -> dict[str, int]:
    updated = dict(summary)
    if delivery_status == "exported":
        updated["exported_count"] += 1
    elif delivery_status == "skipped_exists":
        updated["skipped_exists_count"] += 1
    elif delivery_status == "failed":
        updated["failed_count"] += 1
    return updated


def os_access_readable(path: Path) -> bool:
    return is_path_readable(path)


def is_path_readable(path: Path) -> bool:
    return path.exists() and path.is_file() and os.access(path, os.R_OK)
