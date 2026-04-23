"""产品服务装配与最小只读查询。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hikbox_pictures.product.audit.service import AuditSamplingService
from hikbox_pictures.product.config import WorkspaceLayout
from hikbox_pictures.product.export.bucket_rules import FaceBucketInput, classify_bucket
from hikbox_pictures.product.export.run_service import ExportRunService
from hikbox_pictures.product.export.template_service import ExportTemplateService
from hikbox_pictures.product.ops_event import OpsEventService
from hikbox_pictures.product.people.repository import PeopleRepository
from hikbox_pictures.product.people.service import PeopleService
from hikbox_pictures.product.scan.execution_service import ScanExecutionService
from hikbox_pictures.product.scan.session_service import ScanSessionRepository, ScanSessionService
from hikbox_pictures.product.source.repository import SourceRepository
from hikbox_pictures.product.source.service import SourceService


class WebReadModel:
    """Web 页面使用的最小只读聚合查询。"""

    def __init__(self, library_db: Path) -> None:
        self._library_db = Path(library_db)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._library_db)
        conn.row_factory = sqlite3.Row
        return conn

    def list_named_people(self) -> list[dict[str, Any]]:
        return self._list_people(where_sql="is_named=1")

    def list_anonymous_people(self) -> list[dict[str, Any]]:
        return self._list_people(where_sql="is_named=0")

    def get_person_detail(self, person_id: int) -> dict[str, Any]:
        conn = self.connect()
        try:
            person = conn.execute(
                """
                SELECT id, person_uuid, display_name, is_named, status, created_at, updated_at
                FROM person
                WHERE id=?
                """,
                (int(person_id),),
            ).fetchone()
            samples = conn.execute(
                """
                SELECT
                  f.id AS face_observation_id,
                  f.crop_relpath,
                  f.context_relpath,
                  f.photo_asset_id,
                  COALESCE(p.is_live_photo, 0) AS is_live_photo,
                  p.live_mov_path,
                  f.quality_score,
                  f.magface_quality,
                  a.assignment_source,
                  a.confidence,
                  a.margin
                FROM face_observation AS f
                LEFT JOIN person_face_assignment AS a
                  ON a.face_observation_id=f.id
                 AND a.active=1
                LEFT JOIN photo_asset AS p
                  ON p.id=f.photo_asset_id
                WHERE a.person_id=?
                ORDER BY f.id ASC
                """,
                (int(person_id),),
            ).fetchall()
        finally:
            conn.close()
        return {
            "person": None if person is None else dict(person),
            "samples": [dict(row) for row in samples],
        }

    def list_sources(self) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                """
                SELECT id, root_path, label, enabled, removed_at, created_at, updated_at
                FROM library_source
                WHERE removed_at IS NULL
                ORDER BY id ASC
                """
            ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    def get_scan_audit_page(self, session_id: int) -> dict[str, Any]:
        conn = self.connect()
        try:
            session = conn.execute(
                """
                SELECT id, run_kind, status, triggered_by, created_at, updated_at, last_error
                FROM scan_session
                WHERE id=?
                """,
                (int(session_id),),
            ).fetchone()
            source_rows = conn.execute(
                """
                SELECT
                  s.library_source_id AS source_id,
                  s.processed_assets,
                  s.failed_assets,
                  s.stage_status_json,
                  src.label
                FROM scan_session_source AS s
                INNER JOIN library_source AS src ON src.id=s.library_source_id
                WHERE s.scan_session_id=?
                ORDER BY s.library_source_id ASC
                """,
                (int(session_id),),
            ).fetchall()
            assignment_run = conn.execute(
                """
                SELECT id, param_snapshot_json
                FROM assignment_run
                WHERE scan_session_id=?
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(session_id),),
            ).fetchone()
            failed_count_row = conn.execute(
                """
                SELECT COUNT(*)
                FROM scan_audit_item
                WHERE scan_session_id=?
                """,
                (int(session_id),),
            ).fetchone()
        finally:
            conn.close()
        return {
            "session": None if session is None else dict(session),
            "sources": [self._map_scan_source_row(row) for row in source_rows],
            "assignment_run_id": None if assignment_run is None else int(assignment_run["id"]),
            "scan_params": self._load_scan_params(assignment_run),
            "failed_count": 0 if failed_count_row is None else int(failed_count_row[0]),
        }

    def list_export_templates(self) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                """
                SELECT id, name, output_root, enabled, created_at, updated_at
                FROM export_template
                ORDER BY id ASC
                """
            ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    def get_export_page(self) -> dict[str, Any]:
        conn = self.connect()
        try:
            templates = conn.execute(
                """
                SELECT id, name, output_root, enabled, created_at, updated_at
                FROM export_template
                ORDER BY id ASC
                """
            ).fetchall()
            runs = conn.execute(
                """
                SELECT id, template_id, status, summary_json, started_at, finished_at
                FROM export_run
                ORDER BY id DESC
                LIMIT 20
                """
            ).fetchall()
            running = conn.execute(
                """
                SELECT id
                FROM export_run
                WHERE status='running'
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            preview = self._build_export_preview(conn)
        finally:
            conn.close()
        return {
            "templates": [dict(row) for row in templates],
            "runs": [self._map_export_run_row(row) for row in runs],
            "running_export_run_id": None if running is None else int(running["id"]),
            "people_write_locked": running is not None,
            "preview": preview,
        }

    def get_export_detail(self, export_id: int) -> dict[str, Any]:
        conn = self.connect()
        try:
            row = conn.execute(
                """
                SELECT id, template_id, status, summary_json, started_at, finished_at
                FROM export_run
                WHERE id=?
                """,
                (int(export_id),),
            ).fetchone()
        finally:
            conn.close()
        return {"run": None if row is None else self._map_export_run_row(row)}

    def query_logs(
        self,
        *,
        scan_session_id: int | None,
        export_run_id: int | None,
        severity: str | None,
    ) -> list[dict[str, Any]]:
        filters: list[str] = ["event_type != 'audit.freeze'"]
        params: list[object] = []
        if scan_session_id is not None:
            filters.append("scan_session_id=?")
            params.append(int(scan_session_id))
        if export_run_id is not None:
            filters.append("export_run_id=?")
            params.append(int(export_run_id))
        if severity:
            filters.append("severity=?")
            params.append(str(severity))
        where_sql = " AND ".join(filters)
        conn = self.connect()
        try:
            rows = conn.execute(
                f"""
                SELECT id, event_type, severity, scan_session_id, export_run_id, payload_json, created_at
                FROM ops_event
                WHERE {where_sql}
                ORDER BY id DESC
                LIMIT 100
                """,
                tuple(params),
            ).fetchall()
        finally:
            conn.close()
        result: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            result.append(
                {
                    "id": int(row["id"]),
                    "event_type": str(row["event_type"]),
                    "severity": str(row["severity"]),
                    "scan_session_id": None if row["scan_session_id"] is None else int(row["scan_session_id"]),
                    "export_run_id": None if row["export_run_id"] is None else int(row["export_run_id"]),
                    "payload": payload,
                    "created_at": str(row["created_at"]),
                }
            )
        return result

    def list_audit_items(self, *, scan_session_id: int) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                """
                SELECT id, scan_session_id, assignment_run_id, audit_type, face_observation_id, person_id, evidence_json, created_at
                FROM scan_audit_item
                WHERE scan_session_id=?
                ORDER BY id ASC
                """,
                (int(scan_session_id),),
            ).fetchall()
        finally:
            conn.close()
        items: list[dict[str, Any]] = []
        for row in rows:
            evidence = json.loads(str(row["evidence_json"]))
            items.append(
                {
                    "id": int(row["id"]),
                    "scan_session_id": int(row["scan_session_id"]),
                    "assignment_run_id": int(row["assignment_run_id"]),
                    "audit_type": str(row["audit_type"]),
                    "face_observation_id": int(row["face_observation_id"]),
                    "person_id": None if row["person_id"] is None else int(row["person_id"]),
                    "evidence": evidence,
                    "created_at": str(row["created_at"]),
                }
            )
        return items

    def _list_people(self, *, where_sql: str) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                f"""
                SELECT
                  p.id,
                  p.person_uuid,
                  p.display_name,
                  p.is_named,
                  p.status,
                  COUNT(a.id) AS sample_count
                FROM person AS p
                LEFT JOIN person_face_assignment AS a
                  ON a.person_id=p.id
                 AND a.active=1
                WHERE p.status='active' AND {where_sql}
                GROUP BY p.id
                ORDER BY p.id ASC
                """
            ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    def _map_scan_source_row(self, row: sqlite3.Row) -> dict[str, Any]:
        status_map = json.loads(str(row["stage_status_json"]))
        processed = int(row["processed_assets"])
        failed = int(row["failed_assets"])
        total = processed + failed
        return {
            "source_id": int(row["source_id"]),
            "label": str(row["label"]),
            "processed_assets": processed,
            "failed_assets": failed,
            "total_assets": total,
            "stage_status": status_map,
        }

    def _map_export_run_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "template_id": int(row["template_id"]),
            "status": str(row["status"]),
            "summary": json.loads(str(row["summary_json"])),
            "started_at": str(row["started_at"]),
            "finished_at": None if row["finished_at"] is None else str(row["finished_at"]),
        }

    def _load_scan_params(self, assignment_run: sqlite3.Row | None) -> dict[str, int]:
        if assignment_run is None or assignment_run["param_snapshot_json"] is None:
            return {"det_size": 640, "workers": 4, "batch_size": 300}
        payload = json.loads(str(assignment_run["param_snapshot_json"]))
        return {
            "det_size": int(payload.get("det_size", 640)),
            "workers": int(payload.get("workers", 4)),
            "batch_size": int(payload.get("batch_size", 300)),
        }

    def _build_export_preview(self, conn: sqlite3.Connection) -> dict[str, Any]:
        template = conn.execute(
            """
            SELECT id
            FROM export_template
            WHERE enabled=1
            ORDER BY id ASC
            LIMIT 1
            """
        ).fetchone()
        if template is None:
            return {"only_count": 0, "group_count": 0, "items": []}

        person_rows = conn.execute(
            """
            SELECT person_id
            FROM export_template_person
            WHERE template_id=?
            ORDER BY person_id ASC
            """,
            (int(template["id"]),),
        ).fetchall()
        selected_person_ids = {int(row["person_id"]) for row in person_rows}
        asset_rows = conn.execute(
            """
            SELECT
              p.id,
              p.primary_path,
              p.capture_datetime,
              p.is_live_photo
            FROM photo_asset AS p
            WHERE p.asset_status='active'
            ORDER BY p.id ASC
            """
        ).fetchall()
        items: list[dict[str, Any]] = []
        only_count = 0
        group_count = 0
        for asset_row in asset_rows:
            face_rows = conn.execute(
                """
                SELECT
                  f.id AS face_observation_id,
                  f.bbox_x1,
                  f.bbox_y1,
                  f.bbox_x2,
                  f.bbox_y2,
                  a.person_id
                FROM face_observation AS f
                LEFT JOIN person_face_assignment AS a
                  ON a.face_observation_id=f.id
                 AND a.active=1
                WHERE f.photo_asset_id=?
                  AND f.active=1
                ORDER BY f.id ASC
                """,
                (int(asset_row["id"]),),
            ).fetchall()
            faces = [
                FaceBucketInput(
                    face_observation_id=int(row["face_observation_id"]),
                    area=(float(row["bbox_x2"]) - float(row["bbox_x1"])) * (float(row["bbox_y2"]) - float(row["bbox_y1"])),
                    assigned_person_id=None if row["person_id"] is None else int(row["person_id"]),
                    is_selected_person=(row["person_id"] is not None and int(row["person_id"]) in selected_person_ids),
                )
                for row in face_rows
            ]
            matched_person_ids = {
                int(face.assigned_person_id)
                for face in faces
                if face.assigned_person_id is not None and face.is_selected_person
            }
            if matched_person_ids != selected_person_ids:
                continue
            bucket = classify_bucket(faces)
            if bucket == "only":
                only_count += 1
            else:
                group_count += 1
            items.append(
                {
                    "photo_asset_id": int(asset_row["id"]),
                    "primary_path": str(asset_row["primary_path"]),
                    "bucket": bucket,
                    "is_live_photo": bool(int(asset_row["is_live_photo"])),
                }
            )
        return {"only_count": only_count, "group_count": group_count, "items": items}


@dataclass(frozen=True)
class ServiceContainer:
    """跨入口共享的服务容器。"""

    layout: WorkspaceLayout
    people: PeopleService
    scan_sessions: ScanSessionService
    scan_session_repo: ScanSessionRepository
    scan_execution: ScanExecutionService
    sources: SourceService
    export_templates: ExportTemplateService
    export_runs: ExportRunService
    audit: AuditSamplingService
    ops_events: OpsEventService
    read_model: WebReadModel


def build_service_container(layout: WorkspaceLayout) -> ServiceContainer:
    """基于工作区装配产品服务。"""

    return ServiceContainer(
        layout=layout,
        people=PeopleService(PeopleRepository(layout.library_db)),
        scan_sessions=ScanSessionService(ScanSessionRepository(layout.library_db)),
        scan_session_repo=ScanSessionRepository(layout.library_db),
        scan_execution=ScanExecutionService(
            db_path=layout.library_db,
            output_root=layout.workspace_root,
        ),
        sources=SourceService(SourceRepository(layout.library_db)),
        export_templates=ExportTemplateService(layout.library_db),
        export_runs=ExportRunService(layout.library_db),
        audit=AuditSamplingService(layout.library_db),
        ops_events=OpsEventService(layout.library_db),
        read_model=WebReadModel(layout.library_db),
    )
