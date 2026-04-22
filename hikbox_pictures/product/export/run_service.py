from __future__ import annotations

import json
import re
import sqlite3
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Sequence

from hikbox_pictures.product.db.connection import connect_sqlite

from . import ExportRunLockError, ExportValidationError, ensure_export_schema
from .bucket_rules import ExportFaceSample, bucket_for_photo

CAPTURE_MONTH_RE = re.compile(r"^\d{4}-\d{2}")


@dataclass(frozen=True)
class ExportRunRecord:
    id: int
    template_id: int
    status: str
    summary_json: dict[str, int]
    started_at: str
    finished_at: str | None


class ExportRunService:
    def __init__(self, library_db_path: Path) -> None:
        self._library_db_path = library_db_path

    def start_export_run(self, *, template_id: int) -> ExportRunRecord:
        started_at = _utc_now()
        with connect_sqlite(self._library_db_path) as conn:
            ensure_export_schema(conn)
            template_row = conn.execute(
                "SELECT id FROM export_template WHERE id=? AND enabled=1",
                (int(template_id),),
            ).fetchone()
            if template_row is None:
                raise ExportValidationError(f"模板不存在或未启用: template_id={template_id}")
            cursor = conn.execute(
                """
                INSERT INTO export_run(template_id, status, summary_json, started_at, finished_at)
                VALUES (?, 'running', '{}', ?, NULL)
                """,
                (int(template_id), started_at),
            )
            conn.commit()
            return ExportRunRecord(
                id=int(cursor.lastrowid),
                template_id=int(template_id),
                status="running",
                summary_json={},
                started_at=started_at,
                finished_at=None,
            )

    def finish_export_run(self, *, run_id: int, status: str, summary: dict[str, int]) -> ExportRunRecord:
        if status not in {"completed", "failed", "aborted"}:
            raise ValueError(f"非法导出状态: {status}")
        finished_at = _utc_now()
        normalized_summary = _normalize_summary(summary)
        with connect_sqlite(self._library_db_path) as conn:
            ensure_export_schema(conn)
            conn.execute(
                """
                UPDATE export_run
                SET status=?, summary_json=?, finished_at=?
                WHERE id=?
                """,
                (status, json.dumps(normalized_summary, ensure_ascii=False), finished_at, int(run_id)),
            )
            conn.commit()
            return self._load_run(conn, int(run_id))

    def execute_export(self, *, template_id: int) -> ExportRunRecord:
        run = self.start_export_run(template_id=template_id)
        return self.execute_existing_run(run_id=run.id, template_id=template_id)

    def execute_existing_run(self, *, run_id: int, template_id: int) -> ExportRunRecord:
        summary = _empty_summary()
        try:
            self._execute_delivery(run_id=run_id, template_id=template_id, counters=summary)
            return self.finish_export_run(run_id=run_id, status="completed", summary=summary)
        except BaseException as exc:
            terminal_status = "aborted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed"
            try:
                self.finish_export_run(run_id=run_id, status=terminal_status, summary=summary)
            except BaseException:
                # 收尾失败不应覆盖原始业务异常，仍以上游原异常为准。
                pass
            raise

    def _execute_delivery(self, *, run_id: int, template_id: int, counters: dict[str, int]) -> None:
        with connect_sqlite(self._library_db_path) as conn:
            ensure_export_schema(conn)
            template = self._load_template(conn, template_id)
            person_ids = self._load_template_person_ids(conn, template_id)
            if not person_ids:
                raise ExportValidationError(f"模板未选择人物: template_id={template_id}")

            photos = conn.execute(
                """
                SELECT
                    pa.id,
                    pa.primary_path,
                    pa.capture_datetime,
                    pa.mtime_ns,
                    pa.live_mov_path,
                    ls.root_path
                FROM photo_asset pa
                JOIN library_source ls ON ls.id = pa.library_source_id
                WHERE pa.asset_status='active'
                ORDER BY pa.id
                """
            ).fetchall()

            for photo in photos:
                photo_asset_id = int(photo[0])
                primary_path = str(photo[1])
                capture_datetime = photo[2]
                mtime_ns = photo[3]
                live_mov_path = photo[4]
                source_root = str(photo[5])

                faces = self._load_photo_faces(conn, photo_asset_id=photo_asset_id)
                if not faces:
                    continue
                if not _photo_hits_template(selected_person_ids=set(person_ids), faces=faces):
                    continue

                bucket_decision = bucket_for_photo(selected_person_ids=set(person_ids), faces=faces)
                month_key = _month_key(capture_datetime=capture_datetime, mtime_ns=mtime_ns)
                output_root = Path(template["output_root"])
                photo_destination_path = output_root / bucket_decision.bucket / month_key / Path(primary_path).name
                photo_destination = str(photo_destination_path.resolve())

                if self._has_delivery_record(
                    conn=conn,
                    run_id=run_id,
                    media_kind="photo",
                    destination_path=photo_destination,
                ):
                    photo_status, photo_error = "skipped_exists", None
                else:
                    photo_status, photo_error, _ = _deliver_file(
                        source_path=_resolve_source_path(root_path=source_root, relative_or_abs_path=primary_path),
                        destination_path=photo_destination_path,
                    )
                photo_status = self._insert_delivery(
                    conn=conn,
                    run_id=run_id,
                    photo_asset_id=photo_asset_id,
                    media_kind="photo",
                    bucket=bucket_decision.bucket,
                    month_key=month_key,
                    destination_path=photo_destination,
                    delivery_status=photo_status,
                    error_message=photo_error,
                )
                counters[photo_status] += 1

                if live_mov_path:
                    live_source = _resolve_source_path(root_path=source_root, relative_or_abs_path=str(live_mov_path))
                    if live_source.exists():
                        live_destination_path = (
                            output_root / bucket_decision.bucket / month_key / Path(str(live_mov_path)).name
                        )
                        live_destination = str(live_destination_path.resolve())
                        if self._has_delivery_record(
                            conn=conn,
                            run_id=run_id,
                            media_kind="live_mov",
                            destination_path=live_destination,
                        ):
                            live_status, live_error = "skipped_exists", None
                        else:
                            live_status, live_error, _ = _deliver_file(
                                source_path=live_source,
                                destination_path=live_destination_path,
                            )
                        live_status = self._insert_delivery(
                            conn=conn,
                            run_id=run_id,
                            photo_asset_id=photo_asset_id,
                            media_kind="live_mov",
                            bucket=bucket_decision.bucket,
                            month_key=month_key,
                            destination_path=live_destination,
                            delivery_status=live_status,
                            error_message=live_error,
                        )
                        counters[live_status] += 1
                conn.commit()

    def _load_template(self, conn, template_id: int):
        row = conn.execute(
            """
            SELECT id, name, output_root, enabled
            FROM export_template
            WHERE id=?
            """,
            (int(template_id),),
        ).fetchone()
        if row is None:
            raise ExportValidationError(f"模板不存在: template_id={template_id}")
        if int(row[3]) != 1:
            raise ExportValidationError(f"模板未启用: template_id={template_id}")
        return {
            "id": int(row[0]),
            "name": str(row[1]),
            "output_root": str(row[2]),
        }

    def _load_template_person_ids(self, conn, template_id: int) -> list[int]:
        rows = conn.execute(
            """
            SELECT person_id
            FROM export_template_person
            WHERE template_id=?
            ORDER BY person_id
            """,
            (int(template_id),),
        ).fetchall()
        return [int(row[0]) for row in rows]

    def _load_photo_faces(self, conn, *, photo_asset_id: int) -> Sequence[ExportFaceSample]:
        rows = conn.execute(
            """
            SELECT
                fo.id,
                pfa.person_id,
                fo.bbox_x1,
                fo.bbox_y1,
                fo.bbox_x2,
                fo.bbox_y2
            FROM face_observation fo
            LEFT JOIN person_face_assignment pfa
              ON pfa.face_observation_id = fo.id
             AND pfa.active = 1
            WHERE fo.photo_asset_id=?
              AND fo.active=1
            ORDER BY fo.id
            """,
            (int(photo_asset_id),),
        ).fetchall()
        result: list[ExportFaceSample] = []
        for row in rows:
            area = _face_area(row[2], row[3], row[4], row[5])
            person_id = None if row[1] is None else int(row[1])
            result.append(
                ExportFaceSample(
                    face_observation_id=int(row[0]),
                    person_id=person_id,
                    area=area,
                )
            )
        return result

    def _has_delivery_record(
        self,
        *,
        conn,
        run_id: int,
        media_kind: str,
        destination_path: str,
    ) -> bool:
        row = conn.execute(
            """
            SELECT 1
            FROM export_delivery
            WHERE export_run_id=?
              AND media_kind=?
              AND destination_path=?
            LIMIT 1
            """,
            (int(run_id), media_kind, destination_path),
        ).fetchone()
        return row is not None

    def _insert_delivery(
        self,
        *,
        conn,
        run_id: int,
        photo_asset_id: int,
        media_kind: str,
        bucket: str,
        month_key: str,
        destination_path: str,
        delivery_status: str,
        error_message: str | None,
    ) -> str:
        existing = conn.execute(
            """
            SELECT id
            FROM export_delivery
            WHERE export_run_id=?
              AND media_kind=?
              AND destination_path=?
            LIMIT 1
            """,
            (int(run_id), media_kind, destination_path),
        ).fetchone()
        if existing is not None:
            return "skipped_exists"

        try:
            conn.execute(
                """
                INSERT INTO export_delivery(
                    export_run_id,
                    photo_asset_id,
                    media_kind,
                    bucket,
                    month_key,
                    destination_path,
                    delivery_status,
                    error_message,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(run_id),
                    int(photo_asset_id),
                    media_kind,
                    bucket,
                    month_key,
                    destination_path,
                    delivery_status,
                    error_message,
                    _utc_now(),
                ),
            )
            return delivery_status
        except sqlite3.IntegrityError as exc:
            if _is_delivery_unique_conflict(exc):
                # 并发或竞态导致的重复投递，统一降级为 skipped_exists。
                return "skipped_exists"
            raise

    def _load_run(self, conn, run_id: int) -> ExportRunRecord:
        row = conn.execute(
            """
            SELECT id, template_id, status, summary_json, started_at, finished_at
            FROM export_run
            WHERE id=?
            """,
            (int(run_id),),
        ).fetchone()
        if row is None:
            raise ExportValidationError(f"导出运行不存在: run_id={run_id}")
        summary_raw = str(row[3]) if row[3] is not None else "{}"
        parsed = json.loads(summary_raw)
        if not isinstance(parsed, dict):
            parsed = {}
        summary_json = _normalize_summary(parsed)
        return ExportRunRecord(
            id=int(row[0]),
            template_id=int(row[1]),
            status=str(row[2]),
            summary_json=summary_json,
            started_at=str(row[4]),
            finished_at=None if row[5] is None else str(row[5]),
        )


def assert_people_writes_allowed(library_db_path: Path) -> None:
    with connect_sqlite(library_db_path) as conn:
        try:
            row = conn.execute(
                """
                SELECT id
                FROM export_run
                WHERE status='running'
                ORDER BY started_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table: export_run" in str(exc):
                return
            raise
    if row is not None:
        raise ExportRunLockError(export_run_id=int(row[0]))


def _photo_hits_template(*, selected_person_ids: set[int], faces: Sequence[ExportFaceSample]) -> bool:
    selected_present = {int(face.person_id) for face in faces if face.person_id in selected_person_ids}
    return selected_present == selected_person_ids


def _face_area(
    bbox_x1: float | int | None,
    bbox_y1: float | int | None,
    bbox_x2: float | int | None,
    bbox_y2: float | int | None,
) -> float | None:
    if bbox_x1 is None or bbox_y1 is None or bbox_x2 is None or bbox_y2 is None:
        return None
    width = float(bbox_x2) - float(bbox_x1)
    height = float(bbox_y2) - float(bbox_y1)
    if width <= 0 or height <= 0:
        return None
    return width * height


def _month_key(*, capture_datetime, mtime_ns) -> str:
    if isinstance(capture_datetime, str):
        capture_datetime = capture_datetime.strip()
        if CAPTURE_MONTH_RE.match(capture_datetime):
            return capture_datetime[:7]
    timestamp_seconds = float(int(mtime_ns)) / 1_000_000_000
    return datetime.fromtimestamp(timestamp_seconds, tz=UTC).strftime("%Y-%m")


def _resolve_source_path(*, root_path: str, relative_or_abs_path: str) -> Path:
    src = Path(relative_or_abs_path)
    if src.is_absolute():
        return src
    return Path(root_path) / src


def _deliver_file(*, source_path: Path, destination_path: Path) -> tuple[str, str | None, str]:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination = str(destination_path.resolve())
    if destination_path.exists():
        return "skipped_exists", None, destination
    try:
        shutil.copy2(source_path, destination_path)
        return "exported", None, destination
    except OSError as exc:
        return "failed", str(exc), destination


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _empty_summary() -> dict[str, int]:
    return {
        "exported": 0,
        "skipped_exists": 0,
        "failed": 0,
    }


def _normalize_summary(summary: Mapping[str, object]) -> dict[str, int]:
    normalized = _empty_summary()
    for key in ("exported", "skipped_exists", "failed"):
        raw_value = summary.get(key, 0)
        try:
            normalized[key] = int(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"summary.{key} 必须是整数: {raw_value!r}") from exc
    return normalized


def _is_delivery_unique_conflict(exc: sqlite3.IntegrityError) -> bool:
    message = str(exc)
    return (
        "UNIQUE constraint failed" in message
        and "export_delivery.export_run_id" in message
        and "export_delivery.media_kind" in message
        and "export_delivery.destination_path" in message
    )


__all__ = [
    "ExportRunLockError",
    "ExportRunRecord",
    "ExportRunService",
    "assert_people_writes_allowed",
]
