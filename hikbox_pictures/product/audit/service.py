from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from hikbox_pictures.product.db.connection import connect_sqlite


AUDIT_TYPES = {
    "low_margin_auto_assign",
    "reassign_after_exclusion",
    "new_anonymous_person",
}
AUTO_ASSIGNMENT_SOURCES = {"hdbscan", "person_consensus", "recall"}


@dataclass(frozen=True)
class AssignmentAuditInput:
    face_observation_id: int
    person_id: int | None
    assignment_source: str
    margin: float | None = None
    reassign_after_exclusion: bool = False
    new_anonymous_person: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScanAuditItem:
    id: int
    scan_session_id: int
    assignment_run_id: int
    audit_type: str
    face_observation_id: int
    person_id: int | None
    evidence_json: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class _AuditItemDraft:
    audit_type: str
    face_observation_id: int
    person_id: int | None
    evidence_json: dict[str, Any]


def build_audit_items(
    run_id: int,
    assignments: Sequence[AssignmentAuditInput],
    *,
    low_margin_threshold: float = 0.04,
) -> list[_AuditItemDraft]:
    drafts: list[_AuditItemDraft] = []
    seen: set[tuple[str, int, int | None]] = set()
    for assignment in assignments:
        if (
            assignment.assignment_source in AUTO_ASSIGNMENT_SOURCES
            and assignment.margin is not None
            and float(assignment.margin) <= float(low_margin_threshold)
        ):
            _append_audit_draft(
                drafts=drafts,
                seen=seen,
                audit_type="low_margin_auto_assign",
                assignment=assignment,
                evidence_json=_merge_evidence_with_system_fields(
                    assignment.evidence,
                    assignment_run_id=int(run_id),
                    assignment_source=assignment.assignment_source,
                    margin=float(assignment.margin),
                    threshold=float(low_margin_threshold),
                ),
            )
        if assignment.reassign_after_exclusion:
            _append_audit_draft(
                drafts=drafts,
                seen=seen,
                audit_type="reassign_after_exclusion",
                assignment=assignment,
                evidence_json=_merge_evidence_with_system_fields(
                    assignment.evidence,
                    assignment_run_id=int(run_id),
                    assignment_source=assignment.assignment_source,
                ),
            )
        if assignment.new_anonymous_person:
            _append_audit_draft(
                drafts=drafts,
                seen=seen,
                audit_type="new_anonymous_person",
                assignment=assignment,
                evidence_json=_merge_evidence_with_system_fields(
                    assignment.evidence,
                    assignment_run_id=int(run_id),
                    assignment_source=assignment.assignment_source,
                ),
            )
    return drafts


class AuditSamplingService:
    def __init__(self, library_db_path: Path) -> None:
        self._library_db_path = library_db_path

    def sample_assignment_run(
        self,
        *,
        scan_session_id: int,
        assignment_run_id: int,
        assignments: Sequence[AssignmentAuditInput],
        low_margin_threshold: float = 0.04,
    ) -> list[ScanAuditItem]:
        with connect_sqlite(self._library_db_path) as conn:
            _ensure_scan_audit_schema(conn)
            _validate_assignment_run_session(
                conn=conn,
                scan_session_id=scan_session_id,
                assignment_run_id=assignment_run_id,
            )
            drafts = build_audit_items(
                run_id=assignment_run_id,
                assignments=assignments,
                low_margin_threshold=low_margin_threshold,
            )
            if not drafts:
                return []

            created_at = _utc_now()
            inserted: list[ScanAuditItem] = []
            for draft in drafts:
                cursor = conn.execute(
                    """
                    INSERT INTO scan_audit_item(
                        scan_session_id,
                        assignment_run_id,
                        audit_type,
                        face_observation_id,
                        person_id,
                        evidence_json,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(scan_session_id),
                        int(assignment_run_id),
                        draft.audit_type,
                        draft.face_observation_id,
                        draft.person_id,
                        json.dumps(draft.evidence_json, ensure_ascii=False),
                        created_at,
                    ),
                )
                item_id = int(cursor.lastrowid)
                row = conn.execute(
                    """
                    SELECT id, scan_session_id, assignment_run_id, audit_type, face_observation_id, person_id, evidence_json, created_at
                    FROM scan_audit_item
                    WHERE id=?
                    """,
                    (item_id,),
                ).fetchone()
                assert row is not None
                inserted.append(_row_to_scan_audit_item(row))
            conn.commit()
        return inserted

    def list_audit_items(
        self,
        *,
        scan_session_id: int,
        audit_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ScanAuditItem]:
        if limit <= 0:
            raise ValueError(f"limit 必须 > 0: {limit}")
        if offset < 0:
            raise ValueError(f"offset 必须 >= 0: {offset}")
        if audit_type is not None and audit_type not in AUDIT_TYPES:
            raise ValueError(f"不支持的 audit_type: {audit_type}")

        conditions = ["scan_session_id=?"]
        params: list[object] = [int(scan_session_id)]
        if audit_type is not None:
            conditions.append("audit_type=?")
            params.append(audit_type)
        sql = f"""
            SELECT id, scan_session_id, assignment_run_id, audit_type, face_observation_id, person_id, evidence_json, created_at
            FROM scan_audit_item
            WHERE {' AND '.join(conditions)}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
        """
        params.extend([int(limit), int(offset)])
        with connect_sqlite(self._library_db_path) as conn:
            _ensure_scan_audit_schema(conn)
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [_row_to_scan_audit_item(row) for row in rows]


def _append_audit_draft(
    *,
    drafts: list[_AuditItemDraft],
    seen: set[tuple[str, int, int | None]],
    audit_type: str,
    assignment: AssignmentAuditInput,
    evidence_json: dict[str, Any],
) -> None:
    if audit_type not in AUDIT_TYPES:
        raise ValueError(f"不支持的 audit_type: {audit_type}")
    key = (audit_type, int(assignment.face_observation_id), assignment.person_id)
    if key in seen:
        return
    seen.add(key)
    drafts.append(
        _AuditItemDraft(
            audit_type=audit_type,
            face_observation_id=int(assignment.face_observation_id),
            person_id=int(assignment.person_id) if assignment.person_id is not None else None,
            evidence_json=evidence_json,
        ),
    )


def _merge_evidence_with_system_fields(
    evidence: dict[str, Any],
    **system_fields: object,
) -> dict[str, Any]:
    merged: dict[str, Any] = dict(evidence or {})
    for key, value in system_fields.items():
        merged[key] = value
    return merged


def _validate_assignment_run_session(
    *,
    conn: sqlite3.Connection,
    scan_session_id: int,
    assignment_run_id: int,
) -> None:
    row = conn.execute(
        """
        SELECT scan_session_id
        FROM assignment_run
        WHERE id=?
        """,
        (int(assignment_run_id),),
    ).fetchone()
    if row is None:
        raise ValueError(f"assignment_run 不存在: id={assignment_run_id}")
    actual_scan_session_id = int(row[0])
    expected_scan_session_id = int(scan_session_id)
    if actual_scan_session_id != expected_scan_session_id:
        raise ValueError(
            "assignment_run 与 scan_session_id 不匹配: "
            f"assignment_run_id={assignment_run_id}, expected={expected_scan_session_id}, actual={actual_scan_session_id}"
        )


def _ensure_scan_audit_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS scan_audit_item (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          scan_session_id INTEGER NOT NULL REFERENCES scan_session(id),
          assignment_run_id INTEGER NOT NULL REFERENCES assignment_run(id),
          audit_type TEXT NOT NULL CHECK (audit_type IN ('low_margin_auto_assign', 'reassign_after_exclusion', 'new_anonymous_person')),
          face_observation_id INTEGER NOT NULL REFERENCES face_observation(id),
          person_id INTEGER REFERENCES person(id),
          evidence_json TEXT NOT NULL,
          created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_scan_audit_session
        ON scan_audit_item(scan_session_id, audit_type);
        """
    )


def _row_to_scan_audit_item(row: sqlite3.Row | tuple[object, ...]) -> ScanAuditItem:
    return ScanAuditItem(
        id=int(row[0]),
        scan_session_id=int(row[1]),
        assignment_run_id=int(row[2]),
        audit_type=str(row[3]),
        face_observation_id=int(row[4]),
        person_id=int(row[5]) if row[5] is not None else None,
        evidence_json=_parse_json_dict(row[6]),
        created_at=str(row[7]),
    )


def _parse_json_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
