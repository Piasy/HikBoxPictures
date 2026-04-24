import os
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.export.run_service import ExportRunService, ExportRunningLockError
from hikbox_pictures.product.export.template_service import ExportTemplateService
from hikbox_pictures.product.people.repository import PeopleRepository
from hikbox_pictures.product.people.service import PeopleService
from hikbox_pictures.product.source.repository import SourceRepository
from hikbox_pictures.product.source.service import SourceService


def test_people_writes_get_domain_lock_during_execute_run_and_rename_stays_available(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    fast_photo_path = source_root / "fast.jpg"
    slow_photo_path = source_root / "slow.jpg"
    fast_photo_path.write_bytes(b"fast-photo")
    slow_photo_path.write_bytes(b"slow-photo")
    os.utime(fast_photo_path, (datetime(2024, 7, 1, 9, 0, 0).timestamp(),) * 2)
    os.utime(slow_photo_path, (datetime(2024, 7, 1, 9, 5, 0).timestamp(),) * 2)

    layout = initialize_workspace(
        workspace_root=tmp_path / "workspace",
        external_root=tmp_path / "external",
    )
    source = SourceService(SourceRepository(layout.library_db)).add_source(str(source_root), label="src")
    first_person_id = _insert_person(layout.library_db, display_name="Alice")
    second_person_id = _insert_person(layout.library_db, display_name="Bob")
    third_person_id = _insert_person(layout.library_db, display_name="Carol")
    fast_asset_id = _insert_asset(layout.library_db, source_id=source.id, relpath="fast.jpg")
    slow_asset_id = _insert_asset(layout.library_db, source_id=source.id, relpath="slow.jpg")
    first_face_id = _assign_face_and_return_face_id(layout.library_db, asset_id=fast_asset_id, person_id=first_person_id)
    _assign_face_and_return_face_id(layout.library_db, asset_id=slow_asset_id, person_id=second_person_id)

    people_service = PeopleService(PeopleRepository(layout.library_db))
    merge_result = people_service.merge_people([first_person_id, second_person_id])

    template = ExportTemplateService(layout.library_db).create_template(
        name="runtime-lock",
        output_root=str(tmp_path / "exports"),
        person_ids=[merge_result.winner_person_id],
    )
    run_service = ExportRunService(layout.library_db)
    run = run_service.start_run(template.id)

    def _connect_short_timeout(db_path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(db_path, timeout=0.1)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    monkeypatch.setattr("hikbox_pictures.product.export.run_service.connect_sqlite", _connect_short_timeout)
    monkeypatch.setattr("hikbox_pictures.product.people.repository.connect_sqlite", _connect_short_timeout)

    entered_delivery_write_lock = threading.Event()
    rename_waiting_on_write_lock = threading.Event()
    allow_export_finish = threading.Event()
    export_outcome: dict[str, object] = {}
    rename_outcome: dict[str, object] = {}

    from hikbox_pictures.product.export import run_service as export_run_module
    from hikbox_pictures.product.people import service as people_service_module

    original_insert_delivery = export_run_module.ExportRunService._insert_delivery
    original_sleep = people_service_module.time.sleep

    def blocking_insert_delivery(self, conn, **kwargs):
        original_insert_delivery(self, conn, **kwargs)
        if Path(str(kwargs["destination_path"])).name == "slow.jpg":
            entered_delivery_write_lock.set()
            if not allow_export_finish.wait(timeout=5):
                raise AssertionError("等待主线程完成并发断言超时")
        return None

    def blocking_retry_sleep(seconds: float) -> None:
        rename_waiting_on_write_lock.set()
        if not allow_export_finish.wait(timeout=5):
            raise AssertionError("等待导出释放写锁超时")
        original_sleep(0)

    monkeypatch.setattr("hikbox_pictures.product.export.run_service.ExportRunService._insert_delivery", blocking_insert_delivery)
    monkeypatch.setattr("hikbox_pictures.product.people.service.time.sleep", blocking_retry_sleep)

    def run_export() -> None:
        try:
            export_outcome["result"] = run_service.execute_run(run.export_run_id)
        except Exception as exc:
            export_outcome["error"] = exc

    def run_rename() -> None:
        try:
            rename_outcome["result"] = people_service.rename_person(third_person_id, "Carol Renamed")
        except Exception as exc:
            rename_outcome["error"] = exc

    export_thread = threading.Thread(target=run_export, daemon=True)
    export_thread.start()
    assert entered_delivery_write_lock.wait(timeout=5), "导出线程未进入 export_delivery 写锁窗口"

    blocked_calls = [
        lambda: people_service.exclude_face(person_id=merge_result.winner_person_id, face_observation_id=first_face_id),
        lambda: people_service.merge_people([merge_result.winner_person_id, third_person_id]),
        lambda: people_service.undo_last_merge(),
    ]
    blocked_exceptions: list[Exception] = []
    for call in blocked_calls:
        try:
            call()
            raise AssertionError("预期真实导出运行态阻断人物归属/合并写操作")
        except Exception as exc:
            blocked_exceptions.append(exc)

    rename_thread = threading.Thread(target=run_rename, daemon=True)
    rename_thread.start()
    assert rename_waiting_on_write_lock.wait(timeout=5), "rename_person 未在真实写锁窗口命中重试"

    try:
        allow_export_finish.set()
        export_thread.join(timeout=5)
        rename_thread.join(timeout=5)
    finally:
        allow_export_finish.set()

    assert "error" not in export_outcome, export_outcome.get("error")
    assert export_thread.is_alive() is False
    assert rename_thread.is_alive() is False
    export_result = export_outcome.get("result")
    assert export_result is not None
    assert export_result.exported_count == 2
    assert export_result.status == "completed"
    assert all(isinstance(exc, ExportRunningLockError) for exc in blocked_exceptions)
    assert all(getattr(exc, "error_code", None) == "EXPORT_RUNNING_LOCK" for exc in blocked_exceptions)
    assert "error" not in rename_outcome, rename_outcome.get("error")
    renamed = rename_outcome.get("result")
    assert renamed is not None
    assert renamed.id == third_person_id
    assert renamed.display_name == "Carol Renamed"


def test_people_writes_blocked_while_export_running(tmp_path: Path) -> None:
    layout = initialize_workspace(
        workspace_root=tmp_path / "workspace",
        external_root=tmp_path / "external",
    )
    source_root = tmp_path / "people-lock-source"
    source_root.mkdir(parents=True, exist_ok=True)
    source_id = SourceService(SourceRepository(layout.library_db)).add_source(str(source_root), label="lock-src").id
    first_person_id = _insert_person(layout.library_db, display_name="Alice")
    second_person_id = _insert_person(layout.library_db, display_name="Bob")
    third_person_id = _insert_person(layout.library_db, display_name="Carol")
    first_face_id = _insert_face_with_assignment(layout.library_db, source_id=source_id, person_id=first_person_id)
    second_face_id = _insert_face_with_assignment(layout.library_db, source_id=source_id, person_id=second_person_id)

    people_service = PeopleService(PeopleRepository(layout.library_db))
    merge_result = people_service.merge_people([first_person_id, second_person_id])

    template = ExportTemplateService(layout.library_db).create_template(
        name="running-lock",
        output_root=str(tmp_path / "exports"),
        person_ids=[first_person_id],
    )
    ExportRunService(layout.library_db).start_run(template.id)

    blocked_calls = [
        lambda: people_service.exclude_face(person_id=merge_result.winner_person_id, face_observation_id=first_face_id),
        lambda: people_service.merge_people([merge_result.winner_person_id, third_person_id]),
        lambda: people_service.undo_last_merge(),
    ]
    for call in blocked_calls:
        try:
            call()
            raise AssertionError("预期导出运行中阻断人物归属/合并写操作")
        except ExportRunningLockError as exc:
            assert exc.error_code == "EXPORT_RUNNING_LOCK"

    renamed = people_service.rename_person(third_person_id, "Carol Renamed")

    conn = sqlite3.connect(layout.library_db)
    try:
        active_assignments = conn.execute(
            """
            SELECT face_observation_id, person_id
            FROM person_face_assignment
            WHERE active=1
            ORDER BY face_observation_id ASC
            """
        ).fetchall()
        people_rows = conn.execute(
            """
            SELECT id, display_name, status, merged_into_person_id
            FROM person
            ORDER BY id ASC
            """
        ).fetchall()
        merge_status = conn.execute(
            "SELECT status FROM merge_operation WHERE id=?",
            (merge_result.merge_operation_id,),
        ).fetchone()
    finally:
        conn.close()

    assert renamed.id == third_person_id
    assert renamed.display_name == "Carol Renamed"
    assert [(int(row[0]), int(row[1])) for row in active_assignments] == [
        (first_face_id, merge_result.winner_person_id),
        (second_face_id, merge_result.winner_person_id),
    ]
    assert [(int(row[0]), str(row[1]), str(row[2]), row[3]) for row in people_rows] == [
        (merge_result.winner_person_id, "Alice", "active", None),
        (second_person_id, "Bob", "merged", merge_result.winner_person_id),
        (third_person_id, "Carol Renamed", "active", None),
    ]
    assert merge_status is not None and str(merge_status[0]) == "applied"


def test_live_photo_mov_is_exported_with_photo_when_present(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    photo_path = source_root / "live.jpg"
    photo_path.write_bytes(b"photo-bytes")
    mov_path = source_root / "live.mov"
    mov_path.write_bytes(b"mov-bytes")
    os.utime(photo_path, (datetime(2024, 5, 1, 10, 0, 0).timestamp(),) * 2)

    layout = initialize_workspace(
        workspace_root=tmp_path / "workspace",
        external_root=tmp_path / "external",
    )
    source = SourceService(SourceRepository(layout.library_db)).add_source(str(source_root), label="src")
    person_id = _insert_person(layout.library_db, display_name="Alice")
    asset_id = _insert_live_asset(layout.library_db, source_id=source.id, relpath="live.jpg", live_mov_path="live.mov")
    _assign_face(layout.library_db, asset_id=asset_id, person_id=person_id)

    template = ExportTemplateService(layout.library_db).create_template(
        name="live",
        output_root=str(tmp_path / "exports"),
        person_ids=[person_id],
    )
    run_service = ExportRunService(layout.library_db)
    run = run_service.start_run(template.id)
    result = run_service.execute_run(run.export_run_id)

    rows = _fetch_deliveries(layout.library_db, run.export_run_id)
    assert result.exported_count == 2
    assert [row["media_kind"] for row in rows] == ["photo", "live_mov"]
    assert (tmp_path / "exports" / "only" / "2024-05" / "live.jpg").read_bytes() == b"photo-bytes"
    assert (tmp_path / "exports" / "only" / "2024-05" / "live.mov").read_bytes() == b"mov-bytes"


def test_missing_live_mov_is_silently_skipped(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    photo_path = source_root / "missing-live.jpg"
    photo_path.write_bytes(b"photo-bytes")
    os.utime(photo_path, (datetime(2024, 6, 1, 11, 0, 0).timestamp(),) * 2)

    layout = initialize_workspace(
        workspace_root=tmp_path / "workspace",
        external_root=tmp_path / "external",
    )
    source = SourceService(SourceRepository(layout.library_db)).add_source(str(source_root), label="src")
    person_id = _insert_person(layout.library_db, display_name="Alice")
    asset_id = _insert_live_asset(
        layout.library_db,
        source_id=source.id,
        relpath="missing-live.jpg",
        live_mov_path="missing-live.mov",
    )
    _assign_face(layout.library_db, asset_id=asset_id, person_id=person_id)

    template = ExportTemplateService(layout.library_db).create_template(
        name="missing-live",
        output_root=str(tmp_path / "exports"),
        person_ids=[person_id],
    )
    run_service = ExportRunService(layout.library_db)
    run = run_service.start_run(template.id)
    result = run_service.execute_run(run.export_run_id)

    rows = _fetch_deliveries(layout.library_db, run.export_run_id)
    assert result.exported_count == 1
    assert [row["media_kind"] for row in rows] == ["photo"]
    assert (tmp_path / "exports" / "only" / "2024-06" / "missing-live.jpg").read_bytes() == b"photo-bytes"
    assert not (tmp_path / "exports" / "only" / "2024-06" / "missing-live.mov").exists()


def test_unreadable_photo_marks_failed_without_aborting_run(tmp_path: Path, monkeypatch) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    photo_path = source_root / "blocked.jpg"
    photo_path.write_bytes(b"photo-bytes")
    os.utime(photo_path, (datetime(2024, 8, 1, 10, 0, 0).timestamp(),) * 2)

    layout = initialize_workspace(
        workspace_root=tmp_path / "workspace",
        external_root=tmp_path / "external",
    )
    source = SourceService(SourceRepository(layout.library_db)).add_source(str(source_root), label="src")
    person_id = _insert_person(layout.library_db, display_name="Alice")
    asset_id = _insert_asset(layout.library_db, source_id=source.id, relpath="blocked.jpg")
    _assign_face(layout.library_db, asset_id=asset_id, person_id=person_id)

    monkeypatch.setattr(
        ExportRunService,
        "_is_source_readable",
        lambda self, path: Path(path).name != "blocked.jpg",
    )

    template = ExportTemplateService(layout.library_db).create_template(
        name="blocked-photo",
        output_root=str(tmp_path / "exports"),
        person_ids=[person_id],
    )
    run_service = ExportRunService(layout.library_db)
    run = run_service.start_run(template.id)
    result = run_service.execute_run(run.export_run_id)

    rows = _fetch_deliveries(layout.library_db, run.export_run_id)
    assert result.status == "failed"
    assert result.failed_count == 1
    assert rows == [
        {
            "media_kind": "photo",
            "destination_path": str(tmp_path / "exports" / "only" / "2024-08" / "blocked.jpg"),
            "delivery_status": "failed",
        }
    ]
    assert not (tmp_path / "exports" / "only" / "2024-08" / "blocked.jpg").exists()


def test_unreadable_live_mov_is_silently_skipped(tmp_path: Path, monkeypatch) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    photo_path = source_root / "live.jpg"
    mov_path = source_root / "live.mov"
    photo_path.write_bytes(b"photo-bytes")
    mov_path.write_bytes(b"mov-bytes")
    os.utime(photo_path, (datetime(2024, 9, 1, 10, 0, 0).timestamp(),) * 2)

    layout = initialize_workspace(
        workspace_root=tmp_path / "workspace",
        external_root=tmp_path / "external",
    )
    source = SourceService(SourceRepository(layout.library_db)).add_source(str(source_root), label="src")
    person_id = _insert_person(layout.library_db, display_name="Alice")
    asset_id = _insert_live_asset(layout.library_db, source_id=source.id, relpath="live.jpg", live_mov_path="live.mov")
    _assign_face(layout.library_db, asset_id=asset_id, person_id=person_id)

    monkeypatch.setattr(
        ExportRunService,
        "_is_source_readable",
        lambda self, path: Path(path).name != "live.mov",
    )

    template = ExportTemplateService(layout.library_db).create_template(
        name="blocked-mov",
        output_root=str(tmp_path / "exports"),
        person_ids=[person_id],
    )
    run_service = ExportRunService(layout.library_db)
    run = run_service.start_run(template.id)
    result = run_service.execute_run(run.export_run_id)

    rows = _fetch_deliveries(layout.library_db, run.export_run_id)
    assert result.status == "completed"
    assert result.exported_count == 1
    assert result.failed_count == 0
    assert rows == [
        {
            "media_kind": "photo",
            "destination_path": str(tmp_path / "exports" / "only" / "2024-09" / "live.jpg"),
            "delivery_status": "exported",
        }
    ]
    assert (tmp_path / "exports" / "only" / "2024-09" / "live.jpg").read_bytes() == b"photo-bytes"
    assert not (tmp_path / "exports" / "only" / "2024-09" / "live.mov").exists()


def _insert_person(library_db: Path, *, display_name: str) -> int:
    conn = sqlite3.connect(library_db)
    try:
        cursor = conn.execute(
            """
            INSERT INTO person(
              person_uuid, display_name, is_named, status, merged_into_person_id, created_at, updated_at
            ) VALUES (?, ?, 1, 'active', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (str(uuid.uuid4()), display_name),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def _insert_live_asset(library_db: Path, *, source_id: int, relpath: str, live_mov_path: str) -> int:
    conn = sqlite3.connect(library_db)
    try:
        cursor = conn.execute(
            """
            INSERT INTO photo_asset(
              library_source_id, primary_path, primary_fingerprint, fingerprint_algo, file_size, mtime_ns,
              capture_datetime, capture_month, is_live_photo, live_mov_path, live_mov_size, live_mov_mtime_ns,
              asset_status, created_at, updated_at
            ) VALUES (?, ?, ?, 'sha256', 100, 200, NULL, NULL, 1, ?, 50, 200, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (source_id, relpath, f"fp-{relpath}", live_mov_path),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def _insert_asset(library_db: Path, *, source_id: int, relpath: str) -> int:
    conn = sqlite3.connect(library_db)
    try:
        cursor = conn.execute(
            """
            INSERT INTO photo_asset(
              library_source_id, primary_path, primary_fingerprint, fingerprint_algo, file_size, mtime_ns,
              capture_datetime, capture_month, is_live_photo, live_mov_path, live_mov_size, live_mov_mtime_ns,
              asset_status, created_at, updated_at
            ) VALUES (?, ?, ?, 'sha256', 100, 200, NULL, NULL, 0, NULL, NULL, NULL, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (source_id, relpath, f"fp-{relpath}"),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def _insert_face_with_assignment(library_db: Path, *, source_id: int, person_id: int) -> int:
    conn = sqlite3.connect(library_db)
    try:
        session_id = int(
            conn.execute(
                """
                INSERT INTO scan_session(
                  run_kind, status, triggered_by, resume_from_session_id, started_at, finished_at, last_error,
                  created_at, updated_at
                ) VALUES ('scan_full', 'completed', 'manual_cli', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL,
                          CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            ).lastrowid
        )
        assignment_run_id = int(
            conn.execute(
                """
                INSERT INTO assignment_run(
                  scan_session_id, algorithm_version, param_snapshot_json, run_kind, started_at, finished_at, status, updated_at
                ) VALUES (?, 'frozen_v5', '{}', 'scan_full', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'completed', CURRENT_TIMESTAMP)
                """,
                (session_id,),
            ).lastrowid
        )
        asset_id = int(
            conn.execute(
                """
                INSERT INTO photo_asset(
                  library_source_id, primary_path, primary_fingerprint, fingerprint_algo, file_size, mtime_ns,
                  capture_datetime, capture_month, is_live_photo, live_mov_path, live_mov_size, live_mov_mtime_ns,
                  asset_status, created_at, updated_at
                ) VALUES (?, ?, ?, 'sha256', 100, 200, NULL, NULL, 0, NULL, NULL, NULL, 'active',
                          CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (source_id, f"asset-{person_id}.jpg", f"asset-fp-{person_id}"),
            ).lastrowid
        )
        face_observation_id = int(
            conn.execute(
                """
                INSERT INTO face_observation(
                  photo_asset_id, face_index, crop_relpath, aligned_relpath, context_relpath,
                  bbox_x1, bbox_y1, bbox_x2, bbox_y2,
                  detector_confidence, face_area_ratio, magface_quality, quality_score,
                  active, inactive_reason, pending_reassign, created_at, updated_at
                ) VALUES (?, 1, 'crop.jpg', 'aligned.jpg', 'context.jpg',
                          10, 10, 30, 30, 0.95, 0.2, 1.0, 0.9, 1, NULL, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (asset_id,),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO person_face_assignment(
              person_id, face_observation_id, assignment_run_id, assignment_source,
              active, confidence, margin, created_at, updated_at
            ) VALUES (?, ?, ?, 'hdbscan', 1, 0.95, 0.1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (person_id, face_observation_id, assignment_run_id),
        )
        conn.commit()
        return face_observation_id
    finally:
        conn.close()


def _assign_face_and_return_face_id(library_db: Path, *, asset_id: int, person_id: int) -> int:
    conn = sqlite3.connect(library_db)
    try:
        session_id = int(
            conn.execute(
                """
                INSERT INTO scan_session(
                  run_kind, status, triggered_by, resume_from_session_id, started_at, finished_at, last_error,
                  created_at, updated_at
                ) VALUES ('scan_full', 'completed', 'manual_cli', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL,
                          CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            ).lastrowid
        )
        assignment_run_id = int(
            conn.execute(
                """
                INSERT INTO assignment_run(
                  scan_session_id, algorithm_version, param_snapshot_json, run_kind, started_at, finished_at, status, updated_at
                ) VALUES (?, 'frozen_v5', '{}', 'scan_full', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'completed', CURRENT_TIMESTAMP)
                """,
                (session_id,),
            ).lastrowid
        )
        face_observation_id = int(
            conn.execute(
                """
                INSERT INTO face_observation(
                  photo_asset_id, face_index, crop_relpath, aligned_relpath, context_relpath,
                  bbox_x1, bbox_y1, bbox_x2, bbox_y2,
                  detector_confidence, face_area_ratio, magface_quality, quality_score,
                  active, inactive_reason, pending_reassign, created_at, updated_at
                ) VALUES (?, 1, 'crop.jpg', 'aligned.jpg', 'context.jpg',
                          10, 10, 30, 30, 0.95, 0.2, 1.0, 0.9, 1, NULL, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (asset_id,),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO person_face_assignment(
              person_id, face_observation_id, assignment_run_id, assignment_source,
              active, confidence, margin, created_at, updated_at
            ) VALUES (?, ?, ?, 'hdbscan', 1, 0.95, 0.1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (person_id, face_observation_id, assignment_run_id),
        )
        conn.commit()
        return face_observation_id
    finally:
        conn.close()


def _assign_face(library_db: Path, *, asset_id: int, person_id: int) -> None:
    conn = sqlite3.connect(library_db)
    try:
        session_id = int(
            conn.execute(
                """
                INSERT INTO scan_session(
                  run_kind, status, triggered_by, resume_from_session_id, started_at, finished_at, last_error,
                  created_at, updated_at
                ) VALUES ('scan_full', 'completed', 'manual_cli', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL,
                          CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            ).lastrowid
        )
        assignment_run_id = int(
            conn.execute(
                """
                INSERT INTO assignment_run(
                  scan_session_id, algorithm_version, param_snapshot_json, run_kind, started_at, finished_at, status, updated_at
                ) VALUES (?, 'frozen_v5', '{}', 'scan_full', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'completed', CURRENT_TIMESTAMP)
                """,
                (session_id,),
            ).lastrowid
        )
        face_observation_id = int(
            conn.execute(
                """
                INSERT INTO face_observation(
                  photo_asset_id, face_index, crop_relpath, aligned_relpath, context_relpath,
                  bbox_x1, bbox_y1, bbox_x2, bbox_y2,
                  detector_confidence, face_area_ratio, magface_quality, quality_score,
                  active, inactive_reason, pending_reassign, created_at, updated_at
                ) VALUES (?, 1, 'crop.jpg', 'aligned.jpg', 'context.jpg',
                          10, 10, 30, 30, 0.95, 0.2, 1.0, 0.9, 1, NULL, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (asset_id,),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO person_face_assignment(
              person_id, face_observation_id, assignment_run_id, assignment_source,
              active, confidence, margin, created_at, updated_at
            ) VALUES (?, ?, ?, 'hdbscan', 1, 0.95, 0.1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (person_id, face_observation_id, assignment_run_id),
        )
        conn.commit()
    finally:
        conn.close()


def _fetch_deliveries(library_db: Path, export_run_id: int) -> list[dict[str, object]]:
    conn = sqlite3.connect(library_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT media_kind, destination_path, delivery_status
            FROM export_delivery
            WHERE export_run_id=?
            ORDER BY id ASC
            """,
            (export_run_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
