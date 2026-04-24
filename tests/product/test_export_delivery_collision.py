import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.export.run_service import ExportRunService
from hikbox_pictures.product.export.template_service import ExportTemplateService
from hikbox_pictures.product.source.repository import SourceRepository
from hikbox_pictures.product.source.service import SourceService


def test_start_run_persists_running_export_run(tmp_path: Path) -> None:
    layout = initialize_workspace(
        workspace_root=tmp_path / "workspace",
        external_root=tmp_path / "external",
    )
    person_id = _insert_person(layout.library_db, display_name="Alice")
    template = ExportTemplateService(layout.library_db).create_template(
        name="startup",
        output_root=str(tmp_path / "exports"),
        person_ids=[person_id],
    )

    run = ExportRunService(layout.library_db).start_run(template.id)

    conn = sqlite3.connect(layout.library_db)
    try:
        row = conn.execute(
            "SELECT template_id, status FROM export_run WHERE id=?",
            (run.export_run_id,),
        ).fetchone()
    finally:
        conn.close()

    assert row == (template.id, "running")
    assert run.status == "running"


def test_export_requires_all_selected_persons_and_month_falls_back_to_mtime(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    first_photo = source_root / "first.jpg"
    first_photo.write_bytes(b"first-photo")
    second_photo = source_root / "second.jpg"
    second_photo.write_bytes(b"second-photo")
    fallback_dt = datetime(2024, 3, 15, 9, 30, 0)
    os.utime(second_photo, (fallback_dt.timestamp(), fallback_dt.timestamp()))

    layout = initialize_workspace(
        workspace_root=tmp_path / "workspace",
        external_root=tmp_path / "external",
    )
    output_root = tmp_path / "exports"
    source = SourceService(SourceRepository(layout.library_db)).add_source(str(source_root), label="src")
    person_a_id = _insert_person(layout.library_db, display_name="Alice")
    person_b_id = _insert_person(layout.library_db, display_name="Bob")
    only_person_a_asset_id = _insert_asset(
        layout.library_db,
        source_id=source.id,
        relpath="first.jpg",
        capture_datetime="2025-02-10T08:00:00+08:00",
    )
    both_people_asset_id = _insert_asset(layout.library_db, source_id=source.id, relpath="second.jpg", capture_datetime=None)
    _assign_face(layout.library_db, asset_id=only_person_a_asset_id, person_id=person_a_id, face_index=1, bbox=(10, 10, 30, 30))
    _assign_face(layout.library_db, asset_id=both_people_asset_id, person_id=person_a_id, face_index=1, bbox=(10, 10, 30, 30))
    _assign_face(layout.library_db, asset_id=both_people_asset_id, person_id=person_b_id, face_index=2, bbox=(40, 10, 60, 30))

    template = ExportTemplateService(layout.library_db).create_template(
        name="family",
        output_root=str(output_root),
        person_ids=[person_a_id, person_b_id],
    )

    run_service = ExportRunService(layout.library_db)
    run = run_service.start_run(template.id)
    result = run_service.execute_run(run.export_run_id)

    rows = _fetch_deliveries(layout.library_db, run.export_run_id)
    assert result.exported_count == 1
    assert [row["photo_asset_id"] for row in rows] == [both_people_asset_id]
    assert rows[0]["month_key"] == "2024-03"
    assert rows[0]["bucket"] == "only"
    assert rows[0]["delivery_status"] == "exported"
    assert rows[0]["destination_path"].endswith("only/2024-03/second.jpg")
    assert (output_root / "only" / "2024-03" / "second.jpg").read_bytes() == b"second-photo"
    assert not (output_root / "only" / "2025-02" / "first.jpg").exists()


def test_export_delivery_collision_marks_skipped_exists_without_overwrite(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    photo_path = source_root / "collision.jpg"
    photo_path.write_bytes(b"new-photo")
    os.utime(photo_path, (datetime(2024, 4, 20, 8, 0, 0).timestamp(),) * 2)

    layout = initialize_workspace(
        workspace_root=tmp_path / "workspace",
        external_root=tmp_path / "external",
    )
    output_root = tmp_path / "exports"
    source = SourceService(SourceRepository(layout.library_db)).add_source(str(source_root), label="src")
    person_a_id = _insert_person(layout.library_db, display_name="Alice")
    person_b_id = _insert_person(layout.library_db, display_name="Bob")
    asset_id = _insert_asset(layout.library_db, source_id=source.id, relpath="collision.jpg", capture_datetime=None)
    _assign_face(layout.library_db, asset_id=asset_id, person_id=person_a_id, face_index=1, bbox=(10, 10, 30, 30))
    _assign_face(layout.library_db, asset_id=asset_id, person_id=person_b_id, face_index=2, bbox=(40, 10, 50, 20))

    collision_path = output_root / "group" / "2024-04" / "collision.jpg"
    collision_path.parent.mkdir(parents=True, exist_ok=True)
    collision_path.write_bytes(b"existing-photo")

    template = ExportTemplateService(layout.library_db).create_template(
        name="solo",
        output_root=str(output_root),
        person_ids=[person_a_id],
    )

    run_service = ExportRunService(layout.library_db)
    run = run_service.start_run(template.id)
    result = run_service.execute_run(run.export_run_id)

    rows = _fetch_deliveries(layout.library_db, run.export_run_id)
    assert result.skipped_exists_count == 1
    assert rows[0]["bucket"] == "group"
    assert rows[0]["delivery_status"] == "skipped_exists"
    assert rows[0]["destination_path"] == str(collision_path)
    assert collision_path.read_bytes() == b"existing-photo"


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


def _insert_asset(
    library_db: Path,
    *,
    source_id: int,
    relpath: str,
    capture_datetime: str | None,
) -> int:
    file_path = Path(relpath)
    conn = sqlite3.connect(library_db)
    try:
        cursor = conn.execute(
            """
            INSERT INTO photo_asset(
              library_source_id, primary_path, primary_fingerprint, fingerprint_algo, file_size, mtime_ns,
              capture_datetime, capture_month, is_live_photo, live_mov_path, live_mov_size, live_mov_mtime_ns,
              asset_status, created_at, updated_at
            ) VALUES (?, ?, ?, 'sha256', ?, ?, ?, NULL, 0, NULL, NULL, NULL, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (source_id, relpath, f"fp-{relpath}", 100, 200, capture_datetime),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def _assign_face(
    library_db: Path,
    *,
    asset_id: int,
    person_id: int,
    face_index: int,
    bbox: tuple[float, float, float, float],
) -> int:
    conn = sqlite3.connect(library_db)
    try:
        assignment_run_id = _ensure_assignment_run(conn)
        cursor = conn.execute(
            """
            INSERT INTO face_observation(
              photo_asset_id, face_index, crop_relpath, aligned_relpath, context_relpath,
              bbox_x1, bbox_y1, bbox_x2, bbox_y2,
              detector_confidence, face_area_ratio, magface_quality, quality_score,
              active, inactive_reason, pending_reassign, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0.95, 0.2, 1.0, 0.9, 1, NULL, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                asset_id,
                face_index,
                f"crop-{asset_id}-{face_index}.jpg",
                f"aligned-{asset_id}-{face_index}.jpg",
                f"context-{asset_id}-{face_index}.jpg",
                bbox[0],
                bbox[1],
                bbox[2],
                bbox[3],
            ),
        )
        face_observation_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO person_face_assignment(
              person_id, face_observation_id, assignment_run_id, assignment_source,
              active, confidence, margin, created_at, updated_at
            ) VALUES (?, ?, ?, 'hdbscan', 1, 0.95, 0.10, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (person_id, face_observation_id, assignment_run_id),
        )
        conn.commit()
        return face_observation_id
    finally:
        conn.close()


def _ensure_assignment_run(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT id FROM scan_session ORDER BY id ASC LIMIT 1").fetchone()
    if row is None:
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
    else:
        session_id = int(row[0])
    run_row = conn.execute("SELECT id FROM assignment_run ORDER BY id ASC LIMIT 1").fetchone()
    if run_row is not None:
        return int(run_row[0])
    cursor = conn.execute(
        """
        INSERT INTO assignment_run(
          scan_session_id, algorithm_version, param_snapshot_json, run_kind, started_at, finished_at, status, updated_at
        ) VALUES (?, 'frozen_v5', '{}', 'scan_full', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'completed', CURRENT_TIMESTAMP)
        """,
        (session_id,),
    )
    return int(cursor.lastrowid)


def _fetch_deliveries(library_db: Path, export_run_id: int) -> list[dict[str, object]]:
    conn = sqlite3.connect(library_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT photo_asset_id, media_kind, bucket, month_key, destination_path, delivery_status
            FROM export_delivery
            WHERE export_run_id=?
            ORDER BY id ASC
            """,
            (export_run_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
