"""轻量审计采样服务。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hikbox_pictures.product.db.connection import connect_sqlite

LOW_MARGIN_AUDIT_TYPE = "low_margin_auto_assign"
REASSIGN_AFTER_EXCLUSION_AUDIT_TYPE = "reassign_after_exclusion"
NEW_ANONYMOUS_PERSON_AUDIT_TYPE = "new_anonymous_person"
AUDIT_FROZEN_EVENT_TYPE = "audit.freeze"


@dataclass(frozen=True)
class AuditItem:
    id: int
    scan_session_id: int
    assignment_run_id: int
    audit_type: str
    face_observation_id: int
    person_id: int | None
    evidence: dict[str, Any]
    created_at: str


class AuditSamplingService:
    """基于 assignment_run 的轻量人工复核样本生成器。"""

    def __init__(
        self,
        db_path: Path,
        *,
        low_margin_threshold: float = 0.05,
    ) -> None:
        self._db_path = Path(db_path)
        self._low_margin_threshold = float(low_margin_threshold)

    def sample_assignment_run(self, assignment_run_id: int) -> list[AuditItem]:
        conn = connect_sqlite(self._db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            items = self.persist_assignment_run(assignment_run_id, conn=conn)
            conn.commit()
            return items
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def persist_assignment_run(
        self,
        assignment_run_id: int,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> list[AuditItem]:
        managed_conn = conn is None
        db = conn or connect_sqlite(self._db_path)
        try:
            persisted_rows = self._fetch_persisted_rows(assignment_run_id=assignment_run_id, conn=db)
            if persisted_rows:
                if not self._has_frozen_marker(assignment_run_id=assignment_run_id, conn=db):
                    self._write_frozen_marker(assignment_run_id=assignment_run_id, conn=db)
                    if managed_conn:
                        db.commit()
                return persisted_rows
            if self._has_frozen_marker(assignment_run_id=assignment_run_id, conn=db):
                return []
            items = self.build_audit_items(assignment_run_id, conn=db)
            for item in items:
                db.execute(
                    """
                    INSERT INTO scan_audit_item(
                      scan_session_id, assignment_run_id, audit_type, face_observation_id, person_id, evidence_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(item.scan_session_id),
                        int(item.assignment_run_id),
                        str(item.audit_type),
                        int(item.face_observation_id),
                        None if item.person_id is None else int(item.person_id),
                        json.dumps(item.evidence, ensure_ascii=False, sort_keys=True),
                    ),
                )
            self._write_frozen_marker(assignment_run_id=assignment_run_id, conn=db)
            if managed_conn:
                db.commit()
            return self._fetch_persisted_rows(assignment_run_id=assignment_run_id, conn=db)
        except Exception:
            if managed_conn:
                db.rollback()
            raise
        finally:
            if managed_conn:
                db.close()

    def build_audit_items(
        self,
        assignment_run_id: int,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> list[AuditItem]:
        managed_conn = conn is None
        db = conn or connect_sqlite(self._db_path)
        try:
            scan_session_id = self._load_scan_session_id(assignment_run_id=assignment_run_id, conn=db)
            items = self._build_low_margin_items(
                assignment_run_id=assignment_run_id,
                scan_session_id=scan_session_id,
                conn=db,
            )
            items.extend(
                self._build_reassign_after_exclusion_items(
                    assignment_run_id=assignment_run_id,
                    scan_session_id=scan_session_id,
                    conn=db,
                )
            )
            items.extend(
                self._build_new_anonymous_person_items(
                    assignment_run_id=assignment_run_id,
                    scan_session_id=scan_session_id,
                    conn=db,
                )
            )
            return self._dedupe_items(items)
        finally:
            if managed_conn:
                db.close()

    def list_items(
        self,
        *,
        assignment_run_id: int | None = None,
        scan_session_id: int | None = None,
        audit_type: str | None = None,
        limit: int | None = 100,
        conn: sqlite3.Connection | None = None,
    ) -> list[AuditItem]:
        filters: list[str] = []
        params: list[object] = []
        if assignment_run_id is not None:
            filters.append("assignment_run_id=?")
            params.append(int(assignment_run_id))
        if scan_session_id is not None:
            filters.append("scan_session_id=?")
            params.append(int(scan_session_id))
        if audit_type is not None:
            filters.append("audit_type=?")
            params.append(str(audit_type))
        where_sql = ""
        if filters:
            where_sql = "WHERE " + " AND ".join(filters)

        managed_conn = conn is None
        db = conn or connect_sqlite(self._db_path)
        sql = f"""
            SELECT
              id,
              scan_session_id,
              assignment_run_id,
              audit_type,
              face_observation_id,
              person_id,
              evidence_json,
              created_at
            FROM scan_audit_item
            {where_sql}
            ORDER BY id ASC
        """
        query_params: tuple[object, ...]
        if limit is None:
            query_params = tuple(params)
        else:
            sql += "\nLIMIT ?"
            query_params = (*params, int(limit))
        try:
            rows = db.execute(sql, query_params).fetchall()
            return [self._row_to_item(row) for row in rows]
        finally:
            if managed_conn:
                db.close()

    def _fetch_persisted_rows(
        self,
        *,
        assignment_run_id: int,
        conn: sqlite3.Connection,
    ) -> list[AuditItem]:
        rows = conn.execute(
            """
            SELECT
              id,
              scan_session_id,
              assignment_run_id,
              audit_type,
              face_observation_id,
              person_id,
              evidence_json,
              created_at
            FROM scan_audit_item
            WHERE assignment_run_id=?
            ORDER BY id ASC
            """,
            (int(assignment_run_id),),
        ).fetchall()
        return [self._row_to_item(row) for row in rows]

    def _has_frozen_marker(
        self,
        *,
        assignment_run_id: int,
        conn: sqlite3.Connection,
    ) -> bool:
        row = conn.execute(
            """
            SELECT 1
            FROM ops_event
            WHERE event_type=?
              AND payload_json=?
            LIMIT 1
            """,
            (
                AUDIT_FROZEN_EVENT_TYPE,
                json.dumps({"assignment_run_id": int(assignment_run_id)}, ensure_ascii=False, sort_keys=True),
            ),
        ).fetchone()
        return row is not None

    def _write_frozen_marker(
        self,
        *,
        assignment_run_id: int,
        conn: sqlite3.Connection,
    ) -> None:
        scan_session_id = self._load_scan_session_id(assignment_run_id=assignment_run_id, conn=conn)
        conn.execute(
            """
            INSERT INTO ops_event(
              event_type, severity, scan_session_id, export_run_id, payload_json
            ) VALUES (?, 'info', ?, NULL, ?)
            """,
            (
                AUDIT_FROZEN_EVENT_TYPE,
                int(scan_session_id),
                json.dumps({"assignment_run_id": int(assignment_run_id)}, ensure_ascii=False, sort_keys=True),
            ),
        )

    def _build_low_margin_items(
        self,
        *,
        assignment_run_id: int,
        scan_session_id: int,
        conn: sqlite3.Connection,
    ) -> list[AuditItem]:
        rows = conn.execute(
            """
            SELECT
              a.face_observation_id,
              a.person_id,
              a.assignment_source,
              a.confidence,
              a.margin,
              f.pending_reassign
            FROM person_face_assignment AS a
            INNER JOIN face_observation AS f ON f.id = a.face_observation_id
            WHERE a.assignment_run_id=?
              AND a.active=1
              AND a.assignment_source IN ('hdbscan', 'person_consensus', 'merge')
              AND a.margin IS NOT NULL
              AND a.margin <= ?
            ORDER BY a.face_observation_id ASC
            """,
            (int(assignment_run_id), float(self._low_margin_threshold)),
        ).fetchall()
        return [
            self._make_item(
                scan_session_id=scan_session_id,
                assignment_run_id=assignment_run_id,
                audit_type=LOW_MARGIN_AUDIT_TYPE,
                face_observation_id=int(row[0]),
                person_id=int(row[1]),
                evidence={
                    "assignment_source": str(row[2]),
                    "confidence": None if row[3] is None else float(row[3]),
                    "margin": None if row[4] is None else float(row[4]),
                    "pending_reassign": bool(row[5]),
                },
            )
            for row in rows
        ]

    def _build_reassign_after_exclusion_items(
        self,
        *,
        assignment_run_id: int,
        scan_session_id: int,
        conn: sqlite3.Connection,
    ) -> list[AuditItem]:
        rows = conn.execute(
            """
            SELECT
              a.face_observation_id,
              a.person_id,
              a.assignment_source,
              e.person_id,
              e.id,
              e.updated_at
            FROM person_face_assignment AS a
            INNER JOIN person_face_exclusion AS e
              ON e.face_observation_id = a.face_observation_id
             AND e.active = 1
            WHERE a.assignment_run_id=?
              AND a.active=1
              AND a.person_id <> e.person_id
            ORDER BY a.face_observation_id ASC
            """,
            (int(assignment_run_id),),
        ).fetchall()
        return [
            self._make_item(
                scan_session_id=scan_session_id,
                assignment_run_id=assignment_run_id,
                audit_type=REASSIGN_AFTER_EXCLUSION_AUDIT_TYPE,
                face_observation_id=int(row[0]),
                person_id=int(row[1]),
                evidence={
                    "assignment_source": str(row[2]),
                    "excluded_person_id": int(row[3]),
                    "exclusion_id": int(row[4]),
                    "exclusion_updated_at": str(row[5]),
                },
            )
            for row in rows
        ]

    def _build_new_anonymous_person_items(
        self,
        *,
        assignment_run_id: int,
        scan_session_id: int,
        conn: sqlite3.Connection,
    ) -> list[AuditItem]:
        rows = conn.execute(
            """
            SELECT
              p.id,
              COALESCE(rep.face_observation_id, member.face_observation_id) AS sample_face_observation_id,
              (
                SELECT COUNT(*)
                FROM person_face_assignment AS a2
                WHERE a2.person_id = p.id
                  AND a2.active = 1
              ) AS active_face_count
            FROM person AS p
            INNER JOIN face_cluster AS c
              ON c.person_id = p.id
             AND c.status = 'active'
             AND c.created_assignment_run_id = ?
            LEFT JOIN face_cluster_rep_face AS rep
              ON rep.face_cluster_id = c.id
             AND rep.rep_rank = (
               SELECT MIN(rep_rank)
               FROM face_cluster_rep_face
               WHERE face_cluster_id = c.id
             )
            LEFT JOIN face_cluster_member AS member
              ON member.face_cluster_id = c.id
             AND member.face_observation_id = (
               SELECT MIN(face_observation_id)
               FROM face_cluster_member
               WHERE face_cluster_id = c.id
             )
            WHERE p.status='active'
              AND p.is_named=0
              AND NOT EXISTS (
                SELECT 1
                FROM person_face_assignment AS prev_assignment
                WHERE prev_assignment.person_id = p.id
                  AND prev_assignment.assignment_run_id <> ?
              )
            ORDER BY p.id ASC, sample_face_observation_id ASC
            """,
            (int(assignment_run_id), int(assignment_run_id)),
        ).fetchall()
        chosen_rows: dict[int, tuple[int, int]] = {}
        for row in rows:
            person_id = int(row[0])
            face_observation_id = int(row[1] or 0)
            if face_observation_id <= 0:
                continue
            current = chosen_rows.get(person_id)
            if current is None or face_observation_id < current[0]:
                chosen_rows[person_id] = (face_observation_id, int(row[2] or 0))
        return [
            self._make_item(
                scan_session_id=scan_session_id,
                assignment_run_id=assignment_run_id,
                audit_type=NEW_ANONYMOUS_PERSON_AUDIT_TYPE,
                face_observation_id=face_observation_id,
                person_id=person_id,
                evidence={
                    "person_id": person_id,
                    "active_face_count": active_face_count,
                },
            )
            for person_id, (face_observation_id, active_face_count) in sorted(chosen_rows.items())
        ]

    def _load_scan_session_id(self, *, assignment_run_id: int, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT scan_session_id FROM assignment_run WHERE id=?",
            (int(assignment_run_id),),
        ).fetchone()
        if row is None:
            raise ValueError(f"assignment_run 不存在: {assignment_run_id}")
        return int(row[0])

    def _dedupe_items(self, items: list[AuditItem]) -> list[AuditItem]:
        deduped: dict[tuple[str, int, int | None], AuditItem] = {}
        for item in items:
            deduped[(item.audit_type, item.face_observation_id, item.person_id)] = item
        return sorted(
            deduped.values(),
            key=lambda item: (item.audit_type, item.face_observation_id, item.person_id or 0),
        )

    def _make_item(
        self,
        *,
        scan_session_id: int,
        assignment_run_id: int,
        audit_type: str,
        face_observation_id: int,
        person_id: int | None,
        evidence: dict[str, Any],
    ) -> AuditItem:
        return AuditItem(
            id=0,
            scan_session_id=int(scan_session_id),
            assignment_run_id=int(assignment_run_id),
            audit_type=str(audit_type),
            face_observation_id=int(face_observation_id),
            person_id=None if person_id is None else int(person_id),
            evidence=dict(evidence),
            created_at="",
        )

    def _row_to_item(self, row: sqlite3.Row | tuple[Any, ...]) -> AuditItem:
        return AuditItem(
            id=int(row[0]),
            scan_session_id=int(row[1]),
            assignment_run_id=int(row[2]),
            audit_type=str(row[3]),
            face_observation_id=int(row[4]),
            person_id=None if row[5] is None else int(row[5]),
            evidence=json.loads(str(row[6])),
            created_at=str(row[7]),
        )


def build_audit_items(db_path: Path, assignment_run_id: int) -> list[AuditItem]:
    """为外部调用方提供无状态构建入口。"""

    return AuditSamplingService(db_path).build_audit_items(assignment_run_id)
