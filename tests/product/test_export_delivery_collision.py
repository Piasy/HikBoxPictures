from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.export.run_service import ExportRunService
from hikbox_pictures.product.export.template_service import ExportTemplateService


def _insert_person(db_path: Path, *, person_uuid: str, display_name: str) -> int:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO person(person_uuid, display_name, is_named, status, merged_into_person_id, created_at, updated_at)
            VALUES (?, ?, 1, 'active', NULL, '2026-04-22T00:00:00+00:00', '2026-04-22T00:00:00+00:00')
            """,
            (person_uuid, display_name),
        )
        conn.commit()
        return int(cursor.lastrowid)


def _insert_scan_session(db_path: Path) -> int:
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
            VALUES ('scan_full', 'completed', 'manual_cli', NULL, '2026-04-22T00:00:00+00:00', '2026-04-22T00:05:00+00:00', NULL, '2026-04-22T00:00:00+00:00', '2026-04-22T00:05:00+00:00')
            """
        )
        conn.commit()
        return int(cursor.lastrowid)


def _insert_assignment_run(db_path: Path, *, scan_session_id: int) -> int:
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
            VALUES (?, 'v5.2026-04-21', '{}', 'scan_full', '2026-04-22T00:00:00+00:00', '2026-04-22T00:05:00+00:00', 'completed')
            """,
            (scan_session_id,),
        )
        conn.commit()
        return int(cursor.lastrowid)


def _insert_library_source(db_path: Path, root_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO library_source(root_path, label, enabled, status, last_discovered_at, created_at, updated_at)
            VALUES (?, 'src', 1, 'active', NULL, '2026-04-22T00:00:00+00:00', '2026-04-22T00:00:00+00:00')
            """,
            (str(root_path),),
        )
        conn.commit()
        return int(cursor.lastrowid)


def _insert_photo_asset(
    db_path: Path,
    *,
    library_source_id: int,
    primary_path: str,
    capture_datetime: str,
    mtime_ns: int,
) -> int:
    with sqlite3.connect(db_path) as conn:
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
            VALUES (?, ?, ?, 'sha256', 123, ?, ?, substr(?, 1, 7), 0, NULL, NULL, NULL, 'active', '2026-04-22T00:00:00+00:00', '2026-04-22T00:00:00+00:00')
            """,
            (library_source_id, primary_path, f"fp-{primary_path}", mtime_ns, capture_datetime, capture_datetime),
        )
        conn.commit()
        return int(cursor.lastrowid)


def _insert_face_observation(
    db_path: Path,
    *,
    photo_asset_id: int,
    face_index: int,
    width: float,
    height: float,
) -> int:
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
            VALUES (?, ?, 'crops/f.jpg', 'aligned/f.jpg', 'context/f.jpg', 0.0, 0.0, ?, ?, 0.99, 0.12, 0.88, 0.91, 1, NULL, 0, '2026-04-22T00:00:00+00:00', '2026-04-22T00:00:00+00:00')
            """,
            (photo_asset_id, face_index, width, height),
        )
        conn.commit()
        return int(cursor.lastrowid)


def _insert_active_assignment(
    db_path: Path,
    *,
    person_id: int,
    face_observation_id: int,
    assignment_run_id: int,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO person_face_assignment(
                person_id,
                face_observation_id,
                assignment_run_id,
                assignment_source,
                active,
                confidence,
                margin,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, 'hdbscan', 1, 0.9, NULL, '2026-04-22T00:00:00+00:00', '2026-04-22T00:00:00+00:00')
            """,
            (person_id, face_observation_id, assignment_run_id),
        )
        conn.commit()


def test_export_delivery_collision_and_bucket_paths(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "only.jpg").write_bytes(b"only-photo")
    (source_root / "group.jpg").write_bytes(b"group-photo")

    named_person_id = _insert_person(
        layout.library_db_path,
        person_uuid="00000000-0000-0000-0000-000000000201",
        display_name="A",
    )
    extra_person_id = _insert_person(
        layout.library_db_path,
        person_uuid="00000000-0000-0000-0000-000000000202",
        display_name="B",
    )
    source_id = _insert_library_source(layout.library_db_path, source_root)
    photo_only_id = _insert_photo_asset(
        layout.library_db_path,
        library_source_id=source_id,
        primary_path="only.jpg",
        capture_datetime="2026-03-14T12:00:00+08:00",
        mtime_ns=1710000000000000000,
    )
    photo_group_id = _insert_photo_asset(
        layout.library_db_path,
        library_source_id=source_id,
        primary_path="group.jpg",
        capture_datetime="2026-04-01T09:00:00+08:00",
        mtime_ns=1710000001000000000,
    )
    session_id = _insert_scan_session(layout.library_db_path)
    assignment_run_id = _insert_assignment_run(layout.library_db_path, scan_session_id=session_id)

    only_selected_face = _insert_face_observation(
        layout.library_db_path,
        photo_asset_id=photo_only_id,
        face_index=0,
        width=24.0,
        height=24.0,
    )
    group_selected_face = _insert_face_observation(
        layout.library_db_path,
        photo_asset_id=photo_group_id,
        face_index=0,
        width=20.0,
        height=20.0,
    )
    group_extra_face = _insert_face_observation(
        layout.library_db_path,
        photo_asset_id=photo_group_id,
        face_index=1,
        width=10.0,
        height=10.0,
    )
    _insert_active_assignment(
        layout.library_db_path,
        person_id=named_person_id,
        face_observation_id=only_selected_face,
        assignment_run_id=assignment_run_id,
    )
    _insert_active_assignment(
        layout.library_db_path,
        person_id=named_person_id,
        face_observation_id=group_selected_face,
        assignment_run_id=assignment_run_id,
    )
    _insert_active_assignment(
        layout.library_db_path,
        person_id=extra_person_id,
        face_observation_id=group_extra_face,
        assignment_run_id=assignment_run_id,
    )

    output_root = (tmp_path / "exports").resolve()
    (output_root / "only" / "2026-03").mkdir(parents=True, exist_ok=True)
    collision_path = output_root / "only" / "2026-03" / "only.jpg"
    collision_path.write_bytes(b"existing")

    template_service = ExportTemplateService(layout.library_db_path)
    template = template_service.create_template(
        name="模板-导出",
        output_root=output_root,
        person_ids=[named_person_id],
    )

    run_service = ExportRunService(layout.library_db_path)
    run = run_service.execute_export(template_id=template.id)

    with sqlite3.connect(layout.library_db_path) as conn:
        rows = conn.execute(
            """
            SELECT photo_asset_id, media_kind, bucket, month_key, destination_path, delivery_status
            FROM export_delivery
            WHERE export_run_id=?
            ORDER BY id
            """,
            (run.id,),
        ).fetchall()

    assert rows == [
        (
            photo_only_id,
            "photo",
            "only",
            "2026-03",
            str(output_root / "only" / "2026-03" / "only.jpg"),
            "skipped_exists",
        ),
        (
            photo_group_id,
            "photo",
            "group",
            "2026-04",
            str(output_root / "group" / "2026-04" / "group.jpg"),
            "exported",
        ),
    ]
    assert (output_root / "group" / "2026-04" / "group.jpg").exists()


def test_same_run_duplicate_destination_path_marked_skipped_exists(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    source_a = tmp_path / "source_a"
    source_b = tmp_path / "source_b"
    source_a.mkdir(parents=True, exist_ok=True)
    source_b.mkdir(parents=True, exist_ok=True)
    (source_a / "dup.jpg").write_bytes(b"first")
    (source_b / "dup.jpg").write_bytes(b"second")

    person_id = _insert_person(
        layout.library_db_path,
        person_uuid="00000000-0000-0000-0000-000000000211",
        display_name="A",
    )
    source_a_id = _insert_library_source(layout.library_db_path, source_a)
    source_b_id = _insert_library_source(layout.library_db_path, source_b)
    photo_a_id = _insert_photo_asset(
        layout.library_db_path,
        library_source_id=source_a_id,
        primary_path="dup.jpg",
        capture_datetime="2026-03-05T10:00:00+08:00",
        mtime_ns=1710000000000000000,
    )
    photo_b_id = _insert_photo_asset(
        layout.library_db_path,
        library_source_id=source_b_id,
        primary_path="dup.jpg",
        capture_datetime="2026-03-05T11:00:00+08:00",
        mtime_ns=1710000001000000000,
    )
    session_id = _insert_scan_session(layout.library_db_path)
    assignment_run_id = _insert_assignment_run(layout.library_db_path, scan_session_id=session_id)

    face_a = _insert_face_observation(
        layout.library_db_path,
        photo_asset_id=photo_a_id,
        face_index=0,
        width=20.0,
        height=20.0,
    )
    face_b = _insert_face_observation(
        layout.library_db_path,
        photo_asset_id=photo_b_id,
        face_index=0,
        width=20.0,
        height=20.0,
    )
    _insert_active_assignment(
        layout.library_db_path,
        person_id=person_id,
        face_observation_id=face_a,
        assignment_run_id=assignment_run_id,
    )
    _insert_active_assignment(
        layout.library_db_path,
        person_id=person_id,
        face_observation_id=face_b,
        assignment_run_id=assignment_run_id,
    )

    output_root = (tmp_path / "exports").resolve()
    template = ExportTemplateService(layout.library_db_path).create_template(
        name="模板-同路径冲突",
        output_root=output_root,
        person_ids=[person_id],
    )
    run = ExportRunService(layout.library_db_path).execute_export(template_id=template.id)

    assert run.status == "completed"
    assert run.summary_json["exported"] == 1
    assert run.summary_json["skipped_exists"] == 1
    assert run.summary_json["failed"] == 0

    with sqlite3.connect(layout.library_db_path) as conn:
        row = conn.execute(
            """
            SELECT delivery_status
            FROM export_delivery
            WHERE export_run_id=?
              AND media_kind='photo'
              AND destination_path=?
            """,
            (run.id, str(output_root / "only" / "2026-03" / "dup.jpg")),
        ).fetchone()
    assert row is not None
    assert row[0] == "exported"


def test_execute_export_failure_keeps_processed_summary_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "ok.jpg").write_bytes(b"ok")
    (source_root / "boom.jpg").write_bytes(b"boom")

    person_id = _insert_person(
        layout.library_db_path,
        person_uuid="00000000-0000-0000-0000-000000000221",
        display_name="A",
    )
    source_id = _insert_library_source(layout.library_db_path, source_root)
    photo_ok_id = _insert_photo_asset(
        layout.library_db_path,
        library_source_id=source_id,
        primary_path="ok.jpg",
        capture_datetime="2026-03-05T10:00:00+08:00",
        mtime_ns=1710000000000000000,
    )
    photo_boom_id = _insert_photo_asset(
        layout.library_db_path,
        library_source_id=source_id,
        primary_path="boom.jpg",
        capture_datetime="2026-03-05T11:00:00+08:00",
        mtime_ns=1710000001000000000,
    )
    session_id = _insert_scan_session(layout.library_db_path)
    assignment_run_id = _insert_assignment_run(layout.library_db_path, scan_session_id=session_id)

    face_ok = _insert_face_observation(
        layout.library_db_path,
        photo_asset_id=photo_ok_id,
        face_index=0,
        width=20.0,
        height=20.0,
    )
    face_boom = _insert_face_observation(
        layout.library_db_path,
        photo_asset_id=photo_boom_id,
        face_index=0,
        width=20.0,
        height=20.0,
    )
    _insert_active_assignment(
        layout.library_db_path,
        person_id=person_id,
        face_observation_id=face_ok,
        assignment_run_id=assignment_run_id,
    )
    _insert_active_assignment(
        layout.library_db_path,
        person_id=person_id,
        face_observation_id=face_boom,
        assignment_run_id=assignment_run_id,
    )

    template = ExportTemplateService(layout.library_db_path).create_template(
        name="模板-中途失败统计",
        output_root=(tmp_path / "exports").resolve(),
        person_ids=[person_id],
    )

    from hikbox_pictures.product.export import run_service as run_service_module

    call_count = {"value": 0}
    original_deliver_file = run_service_module._deliver_file

    def flaky_deliver_file(*, source_path: Path, destination_path: Path):
        call_count["value"] += 1
        if call_count["value"] == 1:
            return original_deliver_file(source_path=source_path, destination_path=destination_path)
        raise RuntimeError("inject export failure")

    monkeypatch.setattr(run_service_module, "_deliver_file", flaky_deliver_file)

    service = ExportRunService(layout.library_db_path)
    with pytest.raises(RuntimeError, match="inject export failure"):
        service.execute_export(template_id=template.id)

    with sqlite3.connect(layout.library_db_path) as conn:
        row = conn.execute(
            """
            SELECT status, summary_json
            FROM export_run
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    assert row is not None
    assert row[0] == "failed"
    summary = json.loads(str(row[1]))
    assert summary["exported"] == 1
    assert summary["skipped_exists"] == 0
    assert summary["failed"] == 0


def test_same_run_duplicate_destination_precheck_skips_without_second_delivery_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    source_a = tmp_path / "source_precheck_a"
    source_b = tmp_path / "source_precheck_b"
    source_a.mkdir(parents=True, exist_ok=True)
    source_b.mkdir(parents=True, exist_ok=True)
    (source_a / "same.jpg").write_bytes(b"a")
    (source_b / "same.jpg").write_bytes(b"b")

    person_id = _insert_person(
        layout.library_db_path,
        person_uuid="00000000-0000-0000-0000-000000000231",
        display_name="A",
    )
    source_a_id = _insert_library_source(layout.library_db_path, source_a)
    source_b_id = _insert_library_source(layout.library_db_path, source_b)
    photo_a_id = _insert_photo_asset(
        layout.library_db_path,
        library_source_id=source_a_id,
        primary_path="same.jpg",
        capture_datetime="2026-03-05T10:00:00+08:00",
        mtime_ns=1710000000000000000,
    )
    photo_b_id = _insert_photo_asset(
        layout.library_db_path,
        library_source_id=source_b_id,
        primary_path="same.jpg",
        capture_datetime="2026-03-05T10:01:00+08:00",
        mtime_ns=1710000001000000000,
    )
    session_id = _insert_scan_session(layout.library_db_path)
    assignment_run_id = _insert_assignment_run(layout.library_db_path, scan_session_id=session_id)
    face_a = _insert_face_observation(
        layout.library_db_path,
        photo_asset_id=photo_a_id,
        face_index=0,
        width=20.0,
        height=20.0,
    )
    face_b = _insert_face_observation(
        layout.library_db_path,
        photo_asset_id=photo_b_id,
        face_index=0,
        width=20.0,
        height=20.0,
    )
    _insert_active_assignment(
        layout.library_db_path,
        person_id=person_id,
        face_observation_id=face_a,
        assignment_run_id=assignment_run_id,
    )
    _insert_active_assignment(
        layout.library_db_path,
        person_id=person_id,
        face_observation_id=face_b,
        assignment_run_id=assignment_run_id,
    )

    template = ExportTemplateService(layout.library_db_path).create_template(
        name="模板-precheck",
        output_root=(tmp_path / "exports").resolve(),
        person_ids=[person_id],
    )

    from hikbox_pictures.product.export import run_service as run_service_module

    calls = {"count": 0}

    def fake_deliver_file(*, source_path: Path, destination_path: Path):
        calls["count"] += 1
        if calls["count"] == 1:
            return ("failed", "simulated failure", str(destination_path))
        raise AssertionError("重复目标路径不应触发第二次文件投递")

    monkeypatch.setattr(run_service_module, "_deliver_file", fake_deliver_file)

    run = ExportRunService(layout.library_db_path).execute_export(template_id=template.id)
    assert run.status == "completed"
    assert run.summary_json == {"exported": 0, "skipped_exists": 1, "failed": 1}
    assert calls["count"] == 1


def test_insert_delivery_unique_integrityerror_downgraded_to_skipped_exists(tmp_path: Path) -> None:
    class _FakeCursor:
        def __init__(self, row):
            self._row = row

        def fetchone(self):
            return self._row

    class _FakeConn:
        def execute(self, sql: str, params):
            if "SELECT id" in sql and "FROM export_delivery" in sql:
                return _FakeCursor(None)
            if "INSERT INTO export_delivery" in sql:
                raise sqlite3.IntegrityError(
                    "UNIQUE constraint failed: "
                    "export_delivery.export_run_id, export_delivery.media_kind, export_delivery.destination_path"
                )
            raise AssertionError(f"unexpected sql: {sql}")

    service = ExportRunService(tmp_path / "library.db")
    status = service._insert_delivery(
        conn=_FakeConn(),
        run_id=1,
        photo_asset_id=1,
        media_kind="photo",
        bucket="only",
        month_key="2026-03",
        destination_path="/tmp/only/2026-03/a.jpg",
        delivery_status="exported",
        error_message=None,
    )
    assert status == "skipped_exists"


def test_insert_delivery_non_unique_integrityerror_not_swallowed(tmp_path: Path) -> None:
    class _FakeCursor:
        def __init__(self, row):
            self._row = row

        def fetchone(self):
            return self._row

    class _FakeConn:
        def execute(self, sql: str, params):
            if "SELECT id" in sql and "FROM export_delivery" in sql:
                return _FakeCursor(None)
            if "INSERT INTO export_delivery" in sql:
                raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")
            raise AssertionError(f"unexpected sql: {sql}")

    service = ExportRunService(tmp_path / "library.db")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        service._insert_delivery(
            conn=_FakeConn(),
            run_id=1,
            photo_asset_id=1,
            media_kind="photo",
            bucket="only",
            month_key="2026-03",
            destination_path="/tmp/only/2026-03/a.jpg",
            delivery_status="exported",
            error_message=None,
        )


def test_execute_export_preserves_original_exception_when_finish_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    source_root = tmp_path / "source-raw-error"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "raw.jpg").write_bytes(b"raw")

    person_id = _insert_person(
        layout.library_db_path,
        person_uuid="00000000-0000-0000-0000-000000000241",
        display_name="A",
    )
    source_id = _insert_library_source(layout.library_db_path, source_root)
    photo_id = _insert_photo_asset(
        layout.library_db_path,
        library_source_id=source_id,
        primary_path="raw.jpg",
        capture_datetime="2026-03-05T10:00:00+08:00",
        mtime_ns=1710000000000000000,
    )
    session_id = _insert_scan_session(layout.library_db_path)
    assignment_run_id = _insert_assignment_run(layout.library_db_path, scan_session_id=session_id)
    face_id = _insert_face_observation(
        layout.library_db_path,
        photo_asset_id=photo_id,
        face_index=0,
        width=20.0,
        height=20.0,
    )
    _insert_active_assignment(
        layout.library_db_path,
        person_id=person_id,
        face_observation_id=face_id,
        assignment_run_id=assignment_run_id,
    )

    template = ExportTemplateService(layout.library_db_path).create_template(
        name="模板-原异常保留",
        output_root=(tmp_path / "exports").resolve(),
        person_ids=[person_id],
    )

    from hikbox_pictures.product.export import run_service as run_service_module

    def raise_original(*, source_path: Path, destination_path: Path):
        raise ValueError("original export error")

    monkeypatch.setattr(run_service_module, "_deliver_file", raise_original)

    service = ExportRunService(layout.library_db_path)
    original_finish = service.finish_export_run

    def broken_finish(*, run_id: int, status: str, summary: dict[str, int]):
        if status in {"failed", "aborted"}:
            raise RuntimeError("finish export failed")
        return original_finish(run_id=run_id, status=status, summary=summary)

    monkeypatch.setattr(service, "finish_export_run", broken_finish)

    with pytest.raises(ValueError, match="original export error"):
        service.execute_export(template_id=template.id)
