from __future__ import annotations

import sqlite3
from pathlib import Path

from hikbox_pictures.product.audit.service import AssignmentAuditInput, AuditSamplingService
from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.ops_event import OpsEventService
from hikbox_pictures.product.service_registry import build_service_registry
import pytest


def _insert_scan_session(db_path: Path, started_at: str, *, status: str = "running") -> int:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO scan_session(
                run_kind,
                status,
                triggered_by,
                resume_from_session_id,
                started_at,
                finished_at,
                last_error,
                created_at,
                updated_at
            )
            VALUES ('scan_full', ?, 'manual_cli', NULL, ?, NULL, NULL, ?, ?)
            """,
            (status, started_at, started_at, started_at),
        )
        conn.commit()
        return int(cursor.lastrowid)


def _insert_assignment_run(db_path: Path, scan_session_id: int, started_at: str) -> int:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO assignment_run(
                scan_session_id,
                algorithm_version,
                param_snapshot_json,
                run_kind,
                started_at,
                finished_at,
                status
            )
            VALUES (?, 'v5.2026-04-21', '{"preview_max_side":480}', 'scan_full', ?, ?, 'completed')
            """,
            (scan_session_id, started_at, started_at),
        )
        conn.commit()
        return int(cursor.lastrowid)


def _insert_person(db_path: Path, person_uuid: str) -> int:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO person(person_uuid, display_name, is_named, status, merged_into_person_id, created_at, updated_at)
            VALUES (?, NULL, 0, 'active', NULL, '2026-04-22T00:00:00+00:00', '2026-04-22T00:00:00+00:00')
            """,
            (person_uuid,),
        )
        conn.commit()
        return int(cursor.lastrowid)


def _insert_photo_asset(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO library_source(root_path, label, enabled, status, last_discovered_at, created_at, updated_at)
            VALUES ('/tmp/src', 'src', 1, 'active', NULL, '2026-04-22T00:00:00+00:00', '2026-04-22T00:00:00+00:00')
            """,
        )
        cursor = conn.execute(
            """
            INSERT INTO photo_asset(
              library_source_id,
              primary_path,
              primary_fingerprint,
              fingerprint_algo,
              file_size,
              mtime_ns,
              capture_datetime,
              capture_month,
              is_live_photo,
              live_mov_path,
              live_mov_size,
              live_mov_mtime_ns,
              asset_status,
              created_at,
              updated_at
            )
            VALUES (1, 'IMG_0001.HEIC', 'fp-1', 'sha256', 123, 456, NULL, NULL, 0, NULL, NULL, NULL, 'active', '2026-04-22T00:00:00+00:00', '2026-04-22T00:00:00+00:00')
            """,
        )
        conn.commit()
        return int(cursor.lastrowid)


def _insert_face_observation(db_path: Path, photo_asset_id: int, face_index: int) -> int:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO face_observation(
                photo_asset_id,
                face_index,
                crop_relpath,
                aligned_relpath,
                context_relpath,
                bbox_x1,
                bbox_y1,
                bbox_x2,
                bbox_y2,
                detector_confidence,
                face_area_ratio,
                magface_quality,
                quality_score,
                active,
                inactive_reason,
                pending_reassign,
                created_at,
                updated_at
            )
            VALUES (?, ?, 'crops/f.jpg', 'aligned/f.jpg', 'context/f.jpg', 0.0, 0.0, 10.0, 10.0, 0.99, 0.12, 0.88, 0.91, 1, NULL, 0, '2026-04-22T00:00:00+00:00', '2026-04-22T00:00:00+00:00')
            """,
            (photo_asset_id, face_index),
        )
        conn.commit()
        return int(cursor.lastrowid)


def test_assignment_run_samples_include_three_required_audit_types(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_session_id = _insert_scan_session(layout.library_db_path, "2026-04-22T00:00:00+00:00")
    assignment_run_id = _insert_assignment_run(layout.library_db_path, scan_session_id, "2026-04-22T00:01:00+00:00")
    person_a = _insert_person(layout.library_db_path, "00000000-0000-0000-0000-000000000001")
    person_b = _insert_person(layout.library_db_path, "00000000-0000-0000-0000-000000000002")
    photo_asset_id = _insert_photo_asset(layout.library_db_path)
    face_a = _insert_face_observation(layout.library_db_path, photo_asset_id, 0)
    face_b = _insert_face_observation(layout.library_db_path, photo_asset_id, 1)
    face_c = _insert_face_observation(layout.library_db_path, photo_asset_id, 2)
    service = AuditSamplingService(layout.library_db_path)

    service.sample_assignment_run(
        scan_session_id=scan_session_id,
        assignment_run_id=assignment_run_id,
        assignments=[
            AssignmentAuditInput(
                face_observation_id=face_a,
                person_id=person_a,
                assignment_source="hdbscan",
                margin=0.01,
            ),
            AssignmentAuditInput(
                face_observation_id=face_b,
                person_id=person_a,
                assignment_source="recall",
                margin=0.20,
                reassign_after_exclusion=True,
            ),
            AssignmentAuditInput(
                face_observation_id=face_c,
                person_id=person_b,
                assignment_source="person_consensus",
                margin=0.18,
                new_anonymous_person=True,
            ),
        ],
    )

    items = service.list_audit_items(scan_session_id=scan_session_id, limit=20)
    assert {item.audit_type for item in items} >= {
        "low_margin_auto_assign",
        "reassign_after_exclusion",
        "new_anonymous_person",
    }

    with sqlite3.connect(layout.library_db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM scan_audit_item WHERE assignment_run_id=?",
            (assignment_run_id,),
        ).fetchone()
    assert row is not None
    assert int(row[0]) >= 3


def test_service_registry_wires_audit_and_ops_event_services(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")

    registry = build_service_registry(library_db_path=layout.library_db_path)

    assert isinstance(registry.audit_service, AuditSamplingService)
    assert isinstance(registry.ops_event_service, OpsEventService)


def test_sample_assignment_run_raises_when_run_session_mismatch(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_a = _insert_scan_session(layout.library_db_path, "2026-04-22T00:00:00+00:00")
    scan_b = _insert_scan_session(layout.library_db_path, "2026-04-22T00:02:00+00:00", status="completed")
    assignment_run_id = _insert_assignment_run(layout.library_db_path, scan_a, "2026-04-22T00:03:00+00:00")
    person_id = _insert_person(layout.library_db_path, "00000000-0000-0000-0000-000000000011")
    photo_asset_id = _insert_photo_asset(layout.library_db_path)
    face_id = _insert_face_observation(layout.library_db_path, photo_asset_id, 0)
    service = AuditSamplingService(layout.library_db_path)

    with pytest.raises(ValueError, match="assignment_run 与 scan_session_id 不匹配"):
        service.sample_assignment_run(
            scan_session_id=scan_b,
            assignment_run_id=assignment_run_id,
            assignments=[
                AssignmentAuditInput(
                    face_observation_id=face_id,
                    person_id=person_id,
                    assignment_source="hdbscan",
                    margin=0.01,
                )
            ],
        )

    with sqlite3.connect(layout.library_db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM scan_audit_item").fetchone()
    assert row is not None
    assert int(row[0]) == 0


def test_sample_assignment_run_raises_when_assignment_run_not_exists(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_session_id = _insert_scan_session(layout.library_db_path, "2026-04-22T00:00:00+00:00")
    person_id = _insert_person(layout.library_db_path, "00000000-0000-0000-0000-000000000013")
    photo_asset_id = _insert_photo_asset(layout.library_db_path)
    face_id = _insert_face_observation(layout.library_db_path, photo_asset_id, 0)
    service = AuditSamplingService(layout.library_db_path)

    with pytest.raises(ValueError, match="assignment_run 不存在"):
        service.sample_assignment_run(
            scan_session_id=scan_session_id,
            assignment_run_id=99999,
            assignments=[
                AssignmentAuditInput(
                    face_observation_id=face_id,
                    person_id=person_id,
                    assignment_source="hdbscan",
                    margin=0.01,
                )
            ],
        )

    with sqlite3.connect(layout.library_db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM scan_audit_item").fetchone()
    assert row is not None
    assert int(row[0]) == 0


def test_system_evidence_fields_cannot_be_overridden_by_assignment_evidence(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_session_id = _insert_scan_session(layout.library_db_path, "2026-04-22T00:00:00+00:00")
    assignment_run_id = _insert_assignment_run(layout.library_db_path, scan_session_id, "2026-04-22T00:01:00+00:00")
    person_id = _insert_person(layout.library_db_path, "00000000-0000-0000-0000-000000000012")
    photo_asset_id = _insert_photo_asset(layout.library_db_path)
    face_id = _insert_face_observation(layout.library_db_path, photo_asset_id, 0)
    service = AuditSamplingService(layout.library_db_path)

    service.sample_assignment_run(
        scan_session_id=scan_session_id,
        assignment_run_id=assignment_run_id,
        assignments=[
            AssignmentAuditInput(
                face_observation_id=face_id,
                person_id=person_id,
                assignment_source="hdbscan",
                margin=0.01,
                evidence={
                    "assignment_run_id": -1,
                    "assignment_source": "spoofed_source",
                    "margin": 999.0,
                    "threshold": -999.0,
                    "custom_note": "kept",
                },
            )
        ],
        low_margin_threshold=0.04,
    )

    items = service.list_audit_items(scan_session_id=scan_session_id, limit=10)
    low_margin_items = [item for item in items if item.audit_type == "low_margin_auto_assign"]
    assert len(low_margin_items) == 1
    evidence = low_margin_items[0].evidence_json
    assert evidence["assignment_run_id"] == assignment_run_id
    assert evidence["assignment_source"] == "hdbscan"
    assert evidence["margin"] == 0.01
    assert evidence["threshold"] == 0.04
    assert evidence["custom_note"] == "kept"
