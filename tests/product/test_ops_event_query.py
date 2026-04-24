import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hikbox_pictures.product.audit.service import AuditSamplingService
from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.scan.session_service import ScanSessionRepository
from tests.product.task6_test_support import create_task6_workspace, seed_face_observations


def test_filter_by_scan_session_and_export_run(tmp_path: Path) -> None:
    ops_event_service_cls = _load_ops_event_service()
    layout, first_session_id, second_session_id, first_export_run_id, second_export_run_id = _seed_workspace(tmp_path)
    service = ops_event_service_cls(layout.library_db)

    service.record_event(
        event_type="scan.progress",
        severity="info",
        payload={"scope": "scan-only"},
        scan_session_id=first_session_id,
    )
    service.record_event(
        event_type="export.progress",
        severity="info",
        payload={"scope": "export-only"},
        export_run_id=first_export_run_id,
    )
    combined = service.record_event(
        event_type="scan.export.linked",
        severity="warning",
        payload={"scope": "both"},
        scan_session_id=first_session_id,
        export_run_id=first_export_run_id,
    )
    service.record_event(
        event_type="scan.export.other",
        severity="error",
        payload={"scope": "other"},
        scan_session_id=second_session_id,
        export_run_id=second_export_run_id,
    )

    scan_items = service.query_events(scan_session_id=first_session_id, limit=20).items
    export_items = service.query_events(export_run_id=first_export_run_id, limit=20).items
    combined_items = service.query_events(
        scan_session_id=first_session_id,
        export_run_id=first_export_run_id,
        limit=20,
    ).items

    assert {item.payload["scope"] for item in scan_items} == {"scan-only", "both"}
    assert {item.payload["scope"] for item in export_items} == {"export-only", "both"}
    assert [item.id for item in combined_items] == [combined.id]


def test_query_by_severity_event_type_and_paginate(tmp_path: Path) -> None:
    ops_event_service_cls = _load_ops_event_service()
    layout, first_session_id, _second_session_id, _first_export_run_id, _second_export_run_id = _seed_workspace(tmp_path)
    service = ops_event_service_cls(layout.library_db)

    service.record_event(
        event_type="export.progress",
        severity="warning",
        payload={"seq": 1},
        scan_session_id=first_session_id,
    )
    service.record_event(
        event_type="export.progress",
        severity="warning",
        payload={"seq": 2},
        scan_session_id=first_session_id,
    )
    service.record_event(
        event_type="export.progress",
        severity="warning",
        payload={"seq": 3},
        scan_session_id=first_session_id,
    )
    service.record_event(
        event_type="export.finished",
        severity="warning",
        payload={"seq": 4},
        scan_session_id=first_session_id,
    )
    service.record_event(
        event_type="export.progress",
        severity="info",
        payload={"seq": 5},
        scan_session_id=first_session_id,
    )

    first_page = service.query_events(
        severity="warning",
        event_type="export.progress",
        limit=2,
    )
    second_page = service.query_events(
        severity="warning",
        event_type="export.progress",
        limit=2,
        before_id=first_page.next_before_id,
    )

    assert [item.payload["seq"] for item in first_page.items] == [3, 2]
    assert first_page.next_before_id == first_page.items[-1].id
    assert [item.payload["seq"] for item in second_page.items] == [1]
    assert second_page.next_before_id is None


def test_paginate_with_before_id_remains_stable_when_new_events_arrive_between_pages(tmp_path: Path) -> None:
    ops_event_service_cls = _load_ops_event_service()
    layout, first_session_id, _second_session_id, _first_export_run_id, _second_export_run_id = _seed_workspace(tmp_path)
    service = ops_event_service_cls(layout.library_db)

    for seq in range(1, 6):
        service.record_event(
            event_type="export.progress",
            severity="warning",
            payload={"seq": seq},
            scan_session_id=first_session_id,
        )

    first_page = service.query_events(
        severity="warning",
        event_type="export.progress",
        limit=2,
    )
    service.record_event(
        event_type="export.progress",
        severity="warning",
        payload={"seq": 99},
        scan_session_id=first_session_id,
    )
    second_page = service.query_events(
        severity="warning",
        event_type="export.progress",
        limit=2,
        before_id=first_page.next_before_id,
    )

    assert [item.payload["seq"] for item in first_page.items] == [5, 4]
    assert [item.payload["seq"] for item in second_page.items] == [3, 2]
    assert {item.id for item in first_page.items}.isdisjoint({item.id for item in second_page.items})


def test_sample_assignment_run_internal_freeze_marker_does_not_pollute_public_log_query(tmp_path: Path) -> None:
    ops_event_service_cls = _load_ops_event_service()
    layout, session_id, _runtime_root = create_task6_workspace(tmp_path)
    _seed_empty_audit_run(layout.library_db, session_id)
    service = ops_event_service_cls(layout.library_db)

    service.record_event(
        event_type="export.progress",
        severity="info",
        payload={"seq": 1},
        scan_session_id=session_id,
    )
    service.record_event(
        event_type="export.progress",
        severity="info",
        payload={"seq": 2},
        scan_session_id=session_id,
    )
    service.record_event(
        event_type="export.progress",
        severity="info",
        payload={"seq": 3},
        scan_session_id=session_id,
    )

    before_page = service.query_events(scan_session_id=session_id, limit=2)
    AuditSamplingService(layout.library_db).sample_assignment_run(1)
    after_page = service.query_events(scan_session_id=session_id, limit=2)

    assert [item.event_type for item in before_page.items] == ["export.progress", "export.progress"]
    assert [item.payload["seq"] for item in before_page.items] == [3, 2]
    assert [(item.id, item.event_type, item.payload) for item in after_page.items] == [
        (item.id, item.event_type, item.payload) for item in before_page.items
    ]


def _load_ops_event_service():
    try:
        from hikbox_pictures.product.ops_event import OpsEventService
    except ImportError as exc:
        pytest.fail(f"缺少 ops_event 服务实现: {exc}")
    return OpsEventService


def _seed_workspace(tmp_path: Path):
    layout = initialize_workspace(
        workspace_root=tmp_path / "workspace",
        external_root=tmp_path / "external",
    )
    repo = ScanSessionRepository(layout.library_db)
    first_session = repo.create_session(
        run_kind="scan_full",
        status="completed",
        triggered_by="manual_cli",
    )
    second_session = repo.create_session(
        run_kind="scan_incremental",
        status="running",
        triggered_by="manual_cli",
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        first_template_id = int(
            conn.execute(
                """
                INSERT INTO export_template(name, output_root, enabled, created_at, updated_at)
                VALUES (?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                ("导出模板一", str(tmp_path / "export-1")),
            ).lastrowid
        )
        second_template_id = int(
            conn.execute(
                """
                INSERT INTO export_template(name, output_root, enabled, created_at, updated_at)
                VALUES (?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                ("导出模板二", str(tmp_path / "export-2")),
            ).lastrowid
        )
        first_export_run_id = int(
            conn.execute(
                """
                INSERT INTO export_run(template_id, status, summary_json, started_at, finished_at)
                VALUES (?, 'completed', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (first_template_id, json.dumps({"exported": 1}, ensure_ascii=False)),
            ).lastrowid
        )
        second_export_run_id = int(
            conn.execute(
                """
                INSERT INTO export_run(template_id, status, summary_json, started_at, finished_at)
                VALUES (?, 'completed', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (second_template_id, json.dumps({"exported": 2}, ensure_ascii=False)),
            ).lastrowid
        )
        conn.commit()
    finally:
        conn.close()

    return layout, first_session.id, second_session.id, first_export_run_id, second_export_run_id


def _seed_empty_audit_run(library_db: Path, scan_session_id: int) -> None:
    conn = sqlite3.connect(library_db)
    try:
        conn.execute(
            """
            INSERT INTO assignment_run(
              id, scan_session_id, algorithm_version, param_snapshot_json, run_kind,
              started_at, finished_at, status
            ) VALUES (1, ?, 'frozen_v5', '{}', 'scan_full', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'completed')
            """,
            (scan_session_id,),
        )
        conn.commit()
    finally:
        conn.close()
