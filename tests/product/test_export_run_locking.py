from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.export.run_service import ExportRunLockError, ExportRunService, assert_people_writes_allowed
from hikbox_pictures.product.export.template_service import ExportTemplateService
from hikbox_pictures.product.scan.assignment_stage import AssignmentCandidate, AssignmentStageService


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
    live_mov_path: str | None,
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
            VALUES (?, ?, ?, 'sha256', 123, 1710000000000000000, '2026-03-14T12:00:00+08:00', '2026-03', ?, ?, NULL, NULL, 'active', '2026-04-22T00:00:00+00:00', '2026-04-22T00:00:00+00:00')
            """,
            (
                library_source_id,
                primary_path,
                f"fp-{primary_path}",
                1 if live_mov_path else 0,
                live_mov_path,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def _insert_face_observation(db_path: Path, *, photo_asset_id: int, face_index: int) -> int:
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
            VALUES (?, ?, 'crops/f.jpg', 'aligned/f.jpg', 'context/f.jpg', 0.0, 0.0, 16.0, 16.0, 0.99, 0.12, 0.88, 0.91, 1, NULL, 0, '2026-04-22T00:00:00+00:00', '2026-04-22T00:00:00+00:00')
            """,
            (photo_asset_id, face_index),
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


def _count_assignment_runs(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(1) FROM assignment_run").fetchone()
    assert row is not None
    return int(row[0])


def test_missing_live_mov_is_silently_skipped(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "live.heic").write_bytes(b"photo-content")

    person_id = _insert_person(
        layout.library_db_path,
        person_uuid="00000000-0000-0000-0000-000000000301",
        display_name="A",
    )
    source_id = _insert_library_source(layout.library_db_path, source_root)
    photo_id = _insert_photo_asset(
        layout.library_db_path,
        library_source_id=source_id,
        primary_path="live.heic",
        live_mov_path="live.mov",
    )
    session_id = _insert_scan_session(layout.library_db_path)
    assignment_run_id = _insert_assignment_run(layout.library_db_path, scan_session_id=session_id)
    face_id = _insert_face_observation(layout.library_db_path, photo_asset_id=photo_id, face_index=0)
    _insert_active_assignment(
        layout.library_db_path,
        person_id=person_id,
        face_observation_id=face_id,
        assignment_run_id=assignment_run_id,
    )

    template_service = ExportTemplateService(layout.library_db_path)
    template = template_service.create_template(
        name="模板-live",
        output_root=(tmp_path / "exports").resolve(),
        person_ids=[person_id],
    )
    run_service = ExportRunService(layout.library_db_path)
    run = run_service.execute_export(template_id=template.id)

    with sqlite3.connect(layout.library_db_path) as conn:
        rows = conn.execute(
            """
            SELECT media_kind, delivery_status
            FROM export_delivery
            WHERE export_run_id=?
            ORDER BY id
            """,
            (run.id,),
        ).fetchall()
    assert rows == [("photo", "exported")]


def test_people_writes_blocked_while_export_running(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    person_id = _insert_person(
        layout.library_db_path,
        person_uuid="00000000-0000-0000-0000-000000000401",
        display_name="A",
    )
    template_service = ExportTemplateService(layout.library_db_path)
    template = template_service.create_template(
        name="模板-lock",
        output_root=(tmp_path / "exports").resolve(),
        person_ids=[person_id],
    )
    run_service = ExportRunService(layout.library_db_path)
    run = run_service.start_export_run(template_id=template.id)

    with pytest.raises(ExportRunLockError, match=f"export_run_id={run.id}"):
        assert_people_writes_allowed(layout.library_db_path)

    finished = run_service.finish_export_run(run_id=run.id, status="completed", summary={"exported": 0})
    assert finished.summary_json == {"exported": 0, "skipped_exists": 0, "failed": 0}
    assert_people_writes_allowed(layout.library_db_path)


@pytest.mark.parametrize("entrypoint", ["run_assignment", "run_frozen_v5_assignment"])
def test_assignment_write_entrypoints_blocked_while_export_running_no_assignment_run_side_effect(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    source_root = tmp_path / "source-entry-lock"
    source_root.mkdir(parents=True, exist_ok=True)
    person_id = _insert_person(
        layout.library_db_path,
        person_uuid="00000000-0000-0000-0000-000000000402",
        display_name="A",
    )
    source_id = _insert_library_source(layout.library_db_path, source_root)
    photo_id = _insert_photo_asset(
        layout.library_db_path,
        library_source_id=source_id,
        primary_path="entry-lock.heic",
        live_mov_path=None,
    )
    face_id = _insert_face_observation(layout.library_db_path, photo_asset_id=photo_id, face_index=0)
    scan_session_id = _insert_scan_session(layout.library_db_path)

    template = ExportTemplateService(layout.library_db_path).create_template(
        name="模板-entry-lock",
        output_root=(tmp_path / "exports").resolve(),
        person_ids=[person_id],
    )
    run = ExportRunService(layout.library_db_path).start_export_run(template_id=template.id)

    service = AssignmentStageService(layout.library_db_path, layout.embedding_db_path)
    before_count = _count_assignment_runs(layout.library_db_path)

    with pytest.raises(ExportRunLockError, match=f"export_run_id={run.id}"):
        if entrypoint == "run_assignment":
            service.run_assignment(
                scan_session_id=scan_session_id,
                run_kind="scan_full",
                candidates=[
                    AssignmentCandidate(
                        face_observation_id=face_id,
                        person_id=person_id,
                        assignment_source="hdbscan",
                        similarity=0.91,
                    )
                ],
            )
        else:
            service.run_frozen_v5_assignment(
                scan_session_id=scan_session_id,
                run_kind="scan_full",
                executor_inputs=[
                    {
                        "face_observation_id": face_id,
                        "person_id": person_id,
                        "assignment_source": "hdbscan",
                        "sim_main": 0.91,
                        "sim_flip": 0.88,
                    }
                ],
            )

    after_count = _count_assignment_runs(layout.library_db_path)
    assert after_count == before_count


def test_execute_export_keyboardinterrupt_marks_aborted_and_releases_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    source_root = tmp_path / "source-kbi"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "kbi.jpg").write_bytes(b"kbi")

    person_id = _insert_person(
        layout.library_db_path,
        person_uuid="00000000-0000-0000-0000-000000000499",
        display_name="A",
    )
    source_id = _insert_library_source(layout.library_db_path, source_root)
    photo_id = _insert_photo_asset(
        layout.library_db_path,
        library_source_id=source_id,
        primary_path="kbi.jpg",
        live_mov_path=None,
    )
    face_id = _insert_face_observation(layout.library_db_path, photo_asset_id=photo_id, face_index=0)
    scan_session_id = _insert_scan_session(layout.library_db_path)
    assignment_run_id = _insert_assignment_run(layout.library_db_path, scan_session_id=scan_session_id)
    _insert_active_assignment(
        layout.library_db_path,
        person_id=person_id,
        face_observation_id=face_id,
        assignment_run_id=assignment_run_id,
    )

    template = ExportTemplateService(layout.library_db_path).create_template(
        name="模板-kbi",
        output_root=(tmp_path / "exports").resolve(),
        person_ids=[person_id],
    )

    from hikbox_pictures.product.export import run_service as run_service_module

    def raise_keyboard_interrupt(*, source_path: Path, destination_path: Path):
        raise KeyboardInterrupt("interrupt for test")

    monkeypatch.setattr(run_service_module, "_deliver_file", raise_keyboard_interrupt)

    service = ExportRunService(layout.library_db_path)
    with pytest.raises(KeyboardInterrupt, match="interrupt for test"):
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
    assert row[0] == "aborted"
    assert json.loads(str(row[1])) == {"exported": 0, "skipped_exists": 0, "failed": 0}
    assert_people_writes_allowed(layout.library_db_path)


def test_assert_people_writes_allowed_without_export_tables_does_not_create_tables(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")

    with sqlite3.connect(layout.library_db_path) as conn:
        before = conn.execute(
            "SELECT COUNT(1) FROM sqlite_master WHERE type='table' AND name='export_run'"
        ).fetchone()
    assert before is not None
    assert int(before[0]) == 0

    assert_people_writes_allowed(layout.library_db_path)

    with sqlite3.connect(layout.library_db_path) as conn:
        after = conn.execute(
            "SELECT COUNT(1) FROM sqlite_master WHERE type='table' AND name='export_run'"
        ).fetchone()
    assert after is not None
    assert int(after[0]) == 0
