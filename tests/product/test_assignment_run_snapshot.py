from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.scan import assignment_stage as assignment_stage_module
from hikbox_pictures.product.engine.param_snapshot import (
    AHC_PASS_2_TIE_BREAK,
    ALGORITHM_VERSION,
    FROZEN_V5_STAGE_SEQUENCE,
    IGNORED_ASSIGNMENT_SOURCES,
    UNKNOWN_ASSIGNMENT_SOURCE_FALLBACK,
)
from hikbox_pictures.product.scan.assignment_stage import AssignmentCandidate, AssignmentStageService


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
            VALUES ('scan_full', 'running', 'manual_cli', NULL, '2026-04-22T00:00:00+00:00', NULL, NULL, '2026-04-22T00:00:00+00:00', '2026-04-22T00:00:00+00:00')
            """
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


def _insert_photo_asset(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO library_source(root_path, label, enabled, status, last_discovered_at, created_at, updated_at)
            VALUES ('/tmp/src', 'src', 1, 'active', NULL, '2026-04-22T00:00:00+00:00', '2026-04-22T00:00:00+00:00')
            """
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
            """
        )
        conn.commit()
        return int(cursor.lastrowid)


def test_assignment_run_records_algorithm_version_and_param_snapshot_json(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_session_id = _insert_scan_session(layout.library_db_path)
    service = AssignmentStageService(layout.library_db_path, layout.embedding_db_path)

    run = service.start_assignment_run(scan_session_id=scan_session_id, run_kind="scan_full")

    assert run.algorithm_version == ALGORITHM_VERSION
    assert run.param_snapshot_json["preview_max_side"] == 480
    assert run.param_snapshot_json["stage_sequence"] == list(FROZEN_V5_STAGE_SEQUENCE)
    assert run.param_snapshot_json["ignored_assignment_sources"] == list(IGNORED_ASSIGNMENT_SOURCES)
    assert run.param_snapshot_json["unknown_assignment_source_fallback"] == UNKNOWN_ASSIGNMENT_SOURCE_FALLBACK
    assert run.param_snapshot_json["ahc_pass_2_tie_break"] == AHC_PASS_2_TIE_BREAK

    with sqlite3.connect(layout.library_db_path) as conn:
        row = conn.execute(
            "SELECT algorithm_version, param_snapshot_json FROM assignment_run WHERE id=?",
            (run.id,),
        ).fetchone()
    assert row is not None
    assert row[0] == ALGORITHM_VERSION
    assert json.loads(row[1])["preview_max_side"] == 480


def test_noise_and_low_quality_ignored_not_persisted_as_assignment(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_session_id = _insert_scan_session(layout.library_db_path)
    person_id = _insert_person(layout.library_db_path, "00000000-0000-0000-0000-000000000001")
    photo_asset_id = _insert_photo_asset(layout.library_db_path)
    face_a = _insert_face_observation(layout.library_db_path, photo_asset_id, 0)
    face_b = _insert_face_observation(layout.library_db_path, photo_asset_id, 1)
    face_c = _insert_face_observation(layout.library_db_path, photo_asset_id, 2)
    service = AssignmentStageService(layout.library_db_path, layout.embedding_db_path)

    run = service.run_assignment(
        scan_session_id=scan_session_id,
        run_kind="scan_full",
        candidates=[
            AssignmentCandidate(face_observation_id=face_a, person_id=person_id, assignment_source="hdbscan", similarity=0.91),
            AssignmentCandidate(face_observation_id=face_b, person_id=person_id, assignment_source="noise", similarity=0.71),
            AssignmentCandidate(
                face_observation_id=face_c,
                person_id=person_id,
                assignment_source="low_quality_ignored",
                similarity=0.66,
            ),
        ],
    )

    with sqlite3.connect(layout.library_db_path) as conn:
        rows = conn.execute(
            """
            SELECT face_observation_id, assignment_source
            FROM person_face_assignment
            WHERE assignment_run_id=?
            ORDER BY id
            """,
            (run.id,),
        ).fetchall()

    assert rows == [(face_a, "hdbscan")]


def test_repeat_assignment_run_deactivates_previous_active_rows(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_session_id = _insert_scan_session(layout.library_db_path)
    person_a = _insert_person(layout.library_db_path, "00000000-0000-0000-0000-000000000011")
    person_b = _insert_person(layout.library_db_path, "00000000-0000-0000-0000-000000000012")
    photo_asset_id = _insert_photo_asset(layout.library_db_path)
    face_id = _insert_face_observation(layout.library_db_path, photo_asset_id, 0)
    service = AssignmentStageService(layout.library_db_path, layout.embedding_db_path)

    run_1 = service.run_assignment(
        scan_session_id=scan_session_id,
        run_kind="scan_full",
        candidates=[AssignmentCandidate(face_observation_id=face_id, person_id=person_a, assignment_source="hdbscan", similarity=0.81)],
    )
    run_2 = service.run_assignment(
        scan_session_id=scan_session_id,
        run_kind="scan_full",
        candidates=[AssignmentCandidate(face_observation_id=face_id, person_id=person_b, assignment_source="hdbscan", similarity=0.93)],
    )

    with sqlite3.connect(layout.library_db_path) as conn:
        rows = conn.execute(
            """
            SELECT assignment_run_id, person_id, active
            FROM person_face_assignment
            WHERE face_observation_id=?
            ORDER BY id
            """,
            (face_id,),
        ).fetchall()
        run_states = conn.execute(
            """
            SELECT id, status
            FROM assignment_run
            WHERE id IN (?, ?)
            ORDER BY id
            """,
            (run_1.id, run_2.id),
        ).fetchall()

    assert rows == [
        (run_1.id, person_a, 0),
        (run_2.id, person_b, 1),
    ]
    assert run_states == [
        (run_1.id, "completed"),
        (run_2.id, "completed"),
    ]


def test_assignment_fk_violation_marks_run_failed_with_finished_at(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_session_id = _insert_scan_session(layout.library_db_path)
    photo_asset_id = _insert_photo_asset(layout.library_db_path)
    face_id = _insert_face_observation(layout.library_db_path, photo_asset_id, 0)
    service = AssignmentStageService(layout.library_db_path, layout.embedding_db_path)

    with pytest.raises(sqlite3.IntegrityError):
        service.run_assignment(
            scan_session_id=scan_session_id,
            run_kind="scan_full",
            candidates=[
                AssignmentCandidate(
                    face_observation_id=face_id,
                    person_id=999999,
                    assignment_source="hdbscan",
                    similarity=0.52,
                )
            ],
        )

    with sqlite3.connect(layout.library_db_path) as conn:
        row = conn.execute(
            """
            SELECT status, finished_at
            FROM assignment_run
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    assert row is not None
    assert row[0] == "failed"
    assert row[1] is not None


def test_assignment_batch_is_atomic_when_later_candidate_violates_fk(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_session_id = _insert_scan_session(layout.library_db_path)
    person_id = _insert_person(layout.library_db_path, "00000000-0000-0000-0000-000000000021")
    photo_asset_id = _insert_photo_asset(layout.library_db_path)
    face_a = _insert_face_observation(layout.library_db_path, photo_asset_id, 0)
    face_b = _insert_face_observation(layout.library_db_path, photo_asset_id, 1)
    service = AssignmentStageService(layout.library_db_path, layout.embedding_db_path)

    with pytest.raises(sqlite3.IntegrityError):
        service.run_assignment(
            scan_session_id=scan_session_id,
            run_kind="scan_full",
            candidates=[
                AssignmentCandidate(face_observation_id=face_a, person_id=person_id, assignment_source="hdbscan", similarity=0.88),
                AssignmentCandidate(face_observation_id=face_b, person_id=999999, assignment_source="hdbscan", similarity=0.67),
            ],
        )

    with sqlite3.connect(layout.library_db_path) as conn:
        assignment_count = conn.execute("SELECT COUNT(1) FROM person_face_assignment").fetchone()
        run_state = conn.execute(
            """
            SELECT status, finished_at
            FROM assignment_run
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    assert assignment_count is not None
    assert int(assignment_count[0]) == 0
    assert run_state is not None
    assert run_state[0] == "failed"
    assert run_state[1] is not None


def test_assignment_non_sql_error_marks_run_failed(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_session_id = _insert_scan_session(layout.library_db_path)
    person_id = _insert_person(layout.library_db_path, "00000000-0000-0000-0000-000000000022")
    photo_asset_id = _insert_photo_asset(layout.library_db_path)
    face_id = _insert_face_observation(layout.library_db_path, photo_asset_id, 0)
    service = AssignmentStageService(layout.library_db_path, layout.embedding_db_path)

    with pytest.raises(ValueError, match="candidate similarity 非法"):
        service.run_assignment(
            scan_session_id=scan_session_id,
            run_kind="scan_full",
            candidates=[
                AssignmentCandidate(
                    face_observation_id=face_id,
                    person_id=person_id,
                    assignment_source="hdbscan",
                    similarity=None,  # type: ignore[arg-type]
                )
            ],
        )

    with sqlite3.connect(layout.library_db_path) as conn:
        row = conn.execute(
            """
            SELECT status, finished_at
            FROM assignment_run
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    assert row is not None
    assert row[0] == "failed"
    assert row[1] is not None


def test_assignment_noise_or_low_quality_still_clears_previous_active(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_session_id = _insert_scan_session(layout.library_db_path)
    person_id = _insert_person(layout.library_db_path, "00000000-0000-0000-0000-000000000031")
    photo_asset_id = _insert_photo_asset(layout.library_db_path)
    face_id = _insert_face_observation(layout.library_db_path, photo_asset_id, 0)
    service = AssignmentStageService(layout.library_db_path, layout.embedding_db_path)

    service.run_assignment(
        scan_session_id=scan_session_id,
        run_kind="scan_full",
        candidates=[AssignmentCandidate(face_observation_id=face_id, person_id=person_id, assignment_source="hdbscan", similarity=0.95)],
    )
    service.run_assignment(
        scan_session_id=scan_session_id,
        run_kind="scan_full",
        candidates=[AssignmentCandidate(face_observation_id=face_id, person_id=person_id, assignment_source="noise", similarity=0.40)],
    )

    with sqlite3.connect(layout.library_db_path) as conn:
        active_count = conn.execute(
            "SELECT COUNT(1) FROM person_face_assignment WHERE face_observation_id=? AND active=1",
            (face_id,),
        ).fetchone()
        rows = conn.execute(
            "SELECT assignment_source, active FROM person_face_assignment WHERE face_observation_id=? ORDER BY id",
            (face_id,),
        ).fetchall()

    assert active_count is not None
    assert int(active_count[0]) == 0
    assert rows == [("hdbscan", 0)]


def test_face_observation_allows_multiple_inactive_history_but_single_active(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    photo_asset_id = _insert_photo_asset(layout.library_db_path)

    with sqlite3.connect(layout.library_db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        base_sql = """
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
            VALUES (?, ?, 'crops/f.jpg', 'aligned/f.jpg', 'context/f.jpg', 0.0, 0.0, 10.0, 10.0, 0.99, 0.12, 0.88, 0.91, ?, ?, 0, '2026-04-22T00:00:00+00:00', '2026-04-22T00:00:00+00:00')
        """
        conn.execute(base_sql, (photo_asset_id, 0, 0, "re_detect_replaced"))
        conn.execute(base_sql, (photo_asset_id, 0, 0, "manual_drop"))
        conn.execute(base_sql, (photo_asset_id, 0, 1, None))
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(base_sql, (photo_asset_id, 0, 1, None))

        rows = conn.execute(
            "SELECT active, inactive_reason FROM face_observation WHERE photo_asset_id=? AND face_index=? ORDER BY id",
            (photo_asset_id, 0),
        ).fetchall()

    assert rows == [
        (0, "re_detect_replaced"),
        (0, "manual_drop"),
        (1, None),
    ]


def test_ignored_source_with_missing_face_observation_marks_run_failed(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_session_id = _insert_scan_session(layout.library_db_path)
    person_id = _insert_person(layout.library_db_path, "00000000-0000-0000-0000-000000000041")
    service = AssignmentStageService(layout.library_db_path, layout.embedding_db_path)

    with pytest.raises(ValueError, match="face_observation 不存在"):
        service.run_assignment(
            scan_session_id=scan_session_id,
            run_kind="scan_full",
            candidates=[
                AssignmentCandidate(
                    face_observation_id=999999,
                    person_id=person_id,
                    assignment_source="noise",
                    similarity=0.1,
                )
            ],
        )

    with sqlite3.connect(layout.library_db_path) as conn:
        row = conn.execute(
            """
            SELECT status, finished_at
            FROM assignment_run
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    assert row is not None
    assert row[0] == "failed"
    assert row[1] is not None


def test_run_frozen_with_noise_and_missing_person_id_completes_and_clears_active(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_session_id = _insert_scan_session(layout.library_db_path)
    person_id = _insert_person(layout.library_db_path, "00000000-0000-0000-0000-000000000042")
    photo_asset_id = _insert_photo_asset(layout.library_db_path)
    face_id = _insert_face_observation(layout.library_db_path, photo_asset_id, 0)
    service = AssignmentStageService(layout.library_db_path, layout.embedding_db_path)

    service.run_assignment(
        scan_session_id=scan_session_id,
        run_kind="scan_full",
        candidates=[AssignmentCandidate(face_observation_id=face_id, person_id=person_id, assignment_source="hdbscan", similarity=0.88)],
    )
    frozen_run = service.run_frozen_v5_assignment(
        scan_session_id=scan_session_id,
        run_kind="scan_full",
        executor_inputs=[
            {
                "face_observation_id": face_id,
                "assignment_source": "noise",
                "sim_main": 0.12,
                "sim_flip": 0.18,
            }
        ],
    )

    with sqlite3.connect(layout.library_db_path) as conn:
        active_count = conn.execute(
            "SELECT COUNT(1) FROM person_face_assignment WHERE face_observation_id=? AND active=1",
            (face_id,),
        ).fetchone()
        run_state = conn.execute(
            "SELECT status FROM assignment_run WHERE id=?",
            (frozen_run.id,),
        ).fetchone()

    assert active_count is not None
    assert int(active_count[0]) == 0
    assert run_state is not None
    assert run_state[0] == "completed"


def test_inactive_observation_in_run_assignment_marks_run_failed(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_session_id = _insert_scan_session(layout.library_db_path)
    person_id = _insert_person(layout.library_db_path, "00000000-0000-0000-0000-000000000043")
    photo_asset_id = _insert_photo_asset(layout.library_db_path)
    face_id = _insert_face_observation(layout.library_db_path, photo_asset_id, 0)
    with sqlite3.connect(layout.library_db_path) as conn:
        conn.execute(
            "UPDATE face_observation SET active=0, inactive_reason='manual_drop' WHERE id=?",
            (face_id,),
        )
        conn.commit()
    service = AssignmentStageService(layout.library_db_path, layout.embedding_db_path)

    with pytest.raises(ValueError, match="face_observation 已失效"):
        service.run_assignment(
            scan_session_id=scan_session_id,
            run_kind="scan_full",
            candidates=[
                AssignmentCandidate(
                    face_observation_id=face_id,
                    person_id=person_id,
                    assignment_source="hdbscan",
                    similarity=0.88,
                )
            ],
        )

    with sqlite3.connect(layout.library_db_path) as conn:
        run_state = conn.execute(
            "SELECT status, finished_at FROM assignment_run ORDER BY id DESC LIMIT 1",
        ).fetchone()
    assert run_state is not None
    assert run_state[0] == "failed"
    assert run_state[1] is not None


def test_start_assignment_run_rejects_run_kind_mismatch(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_session_id = _insert_scan_session(layout.library_db_path)
    service = AssignmentStageService(layout.library_db_path, layout.embedding_db_path)

    with pytest.raises(ValueError, match="run_kind 不匹配"):
        service.start_assignment_run(scan_session_id=scan_session_id, run_kind="scan_incremental")

    with sqlite3.connect(layout.library_db_path) as conn:
        run_count = conn.execute("SELECT COUNT(1) FROM assignment_run").fetchone()
    assert run_count is not None
    assert int(run_count[0]) == 0


def test_run_assignment_rejects_duplicate_face_candidates_and_marks_failed(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_session_id = _insert_scan_session(layout.library_db_path)
    person_id = _insert_person(layout.library_db_path, "00000000-0000-0000-0000-000000000044")
    photo_asset_id = _insert_photo_asset(layout.library_db_path)
    face_id = _insert_face_observation(layout.library_db_path, photo_asset_id, 0)
    service = AssignmentStageService(layout.library_db_path, layout.embedding_db_path)

    with pytest.raises(ValueError, match="重复 face_observation_id"):
        service.run_assignment(
            scan_session_id=scan_session_id,
            run_kind="scan_full",
            candidates=[
                AssignmentCandidate(
                    face_observation_id=face_id,
                    person_id=person_id,
                    assignment_source="hdbscan",
                    similarity=0.88,
                ),
                AssignmentCandidate(
                    face_observation_id=face_id,
                    person_id=person_id,
                    assignment_source="noise",
                    similarity=0.10,
                ),
            ],
        )

    with sqlite3.connect(layout.library_db_path) as conn:
        run_state = conn.execute(
            "SELECT status, finished_at FROM assignment_run ORDER BY id DESC LIMIT 1",
        ).fetchone()
        assignment_count = conn.execute("SELECT COUNT(1) FROM person_face_assignment").fetchone()

    assert run_state is not None
    assert run_state[0] == "failed"
    assert run_state[1] is not None
    assert assignment_count is not None
    assert int(assignment_count[0]) == 0


def test_run_assignment_rejects_nan_or_inf_similarity_and_marks_failed(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_session_id = _insert_scan_session(layout.library_db_path)
    person_id = _insert_person(layout.library_db_path, "00000000-0000-0000-0000-000000000045")
    photo_asset_id = _insert_photo_asset(layout.library_db_path)
    face_id = _insert_face_observation(layout.library_db_path, photo_asset_id, 0)
    service = AssignmentStageService(layout.library_db_path, layout.embedding_db_path)

    with pytest.raises(ValueError, match="candidate similarity 非法"):
        service.run_assignment(
            scan_session_id=scan_session_id,
            run_kind="scan_full",
            candidates=[
                AssignmentCandidate(
                    face_observation_id=face_id,
                    person_id=person_id,
                    assignment_source="hdbscan",
                    similarity=float("nan"),
                )
            ],
        )
    with pytest.raises(ValueError, match="candidate similarity 非法"):
        service.run_assignment(
            scan_session_id=scan_session_id,
            run_kind="scan_full",
            candidates=[
                AssignmentCandidate(
                    face_observation_id=face_id,
                    person_id=person_id,
                    assignment_source="hdbscan",
                    similarity=float("inf"),
                )
            ],
        )

    with sqlite3.connect(layout.library_db_path) as conn:
        run_rows = conn.execute(
            "SELECT status, finished_at FROM assignment_run ORDER BY id",
        ).fetchall()
        assignment_count = conn.execute("SELECT COUNT(1) FROM person_face_assignment").fetchone()

    assert len(run_rows) == 2
    assert run_rows[0][0] == "failed"
    assert run_rows[0][1] is not None
    assert run_rows[1][0] == "failed"
    assert run_rows[1][1] is not None
    assert assignment_count is not None
    assert int(assignment_count[0]) == 0


def test_run_assignment_rejects_non_strict_face_observation_id_and_marks_failed(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_session_id = _insert_scan_session(layout.library_db_path)
    person_id = _insert_person(layout.library_db_path, "00000000-0000-0000-0000-000000000046")
    service = AssignmentStageService(layout.library_db_path, layout.embedding_db_path)

    with pytest.raises(ValueError, match="run_assignment.face_observation_id 非法"):
        service.run_assignment(
            scan_session_id=scan_session_id,
            run_kind="scan_full",
            candidates=[
                AssignmentCandidate(
                    face_observation_id=1.0,  # type: ignore[arg-type]
                    person_id=person_id,
                    assignment_source="hdbscan",
                    similarity=0.88,
                )
            ],
        )
    with pytest.raises(ValueError, match="run_assignment.face_observation_id 非法"):
        service.run_assignment(
            scan_session_id=scan_session_id,
            run_kind="scan_full",
            candidates=[
                AssignmentCandidate(
                    face_observation_id=True,  # type: ignore[arg-type]
                    person_id=person_id,
                    assignment_source="hdbscan",
                    similarity=0.88,
                )
            ],
        )

    with sqlite3.connect(layout.library_db_path) as conn:
        runs = conn.execute("SELECT status, finished_at FROM assignment_run ORDER BY id").fetchall()
        assignment_count = conn.execute("SELECT COUNT(1) FROM person_face_assignment").fetchone()

    assert len(runs) == 2
    assert runs[0][0] == "failed"
    assert runs[0][1] is not None
    assert runs[1][0] == "failed"
    assert runs[1][1] is not None
    assert assignment_count is not None
    assert int(assignment_count[0]) == 0


def test_assignment_observation_validation_uses_chunked_queries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_session_id = _insert_scan_session(layout.library_db_path)
    person_id = _insert_person(layout.library_db_path, "00000000-0000-0000-0000-000000000047")
    photo_asset_id = _insert_photo_asset(layout.library_db_path)
    face_ids = [
        _insert_face_observation(layout.library_db_path, photo_asset_id, 0),
        _insert_face_observation(layout.library_db_path, photo_asset_id, 1),
        _insert_face_observation(layout.library_db_path, photo_asset_id, 2),
    ]
    service = AssignmentStageService(layout.library_db_path, layout.embedding_db_path)

    original_connect = assignment_stage_module.connect_sqlite
    select_count = {"value": 0}

    def _counted_connect(path: Path) -> sqlite3.Connection:
        conn = original_connect(path)

        def _trace(sql: str) -> None:
            if "select id, active from face_observation" in sql.lower():
                select_count["value"] += 1

        conn.set_trace_callback(_trace)
        return conn

    monkeypatch.setattr(assignment_stage_module, "connect_sqlite", _counted_connect)
    monkeypatch.setattr(assignment_stage_module, "OBSERVATION_VALIDATE_CHUNK_SIZE", 2)

    run = service.run_assignment(
        scan_session_id=scan_session_id,
        run_kind="scan_full",
        candidates=[
            AssignmentCandidate(face_observation_id=face_ids[0], person_id=person_id, assignment_source="hdbscan", similarity=0.9),
            AssignmentCandidate(face_observation_id=face_ids[1], person_id=person_id, assignment_source="hdbscan", similarity=0.8),
            AssignmentCandidate(face_observation_id=face_ids[2], person_id=person_id, assignment_source="hdbscan", similarity=0.7),
        ],
    )

    with sqlite3.connect(layout.library_db_path) as conn:
        rows = conn.execute(
            "SELECT face_observation_id, active FROM person_face_assignment WHERE assignment_run_id=? ORDER BY face_observation_id",
            (run.id,),
        ).fetchall()
    assert rows == [
        (face_ids[0], 1),
        (face_ids[1], 1),
        (face_ids[2], 1),
    ]
    assert select_count["value"] >= 2


def test_run_frozen_v5_assignment_fails_when_embedding_missing_and_marks_failed(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_session_id = _insert_scan_session(layout.library_db_path)
    person_id = _insert_person(layout.library_db_path, "00000000-0000-0000-0000-000000000048")
    photo_asset_id = _insert_photo_asset(layout.library_db_path)
    face_id = _insert_face_observation(layout.library_db_path, photo_asset_id, 0)
    service = AssignmentStageService(layout.library_db_path, layout.embedding_db_path)

    with pytest.raises(ValueError, match="embedding 缺失"):
        service.run_frozen_v5_assignment(
            scan_session_id=scan_session_id,
            run_kind="scan_full",
            executor_inputs=[
                {
                    "face_observation_id": face_id,
                    "person_id": person_id,
                    "assignment_source": "hdbscan",
                    "sim_main": 0.31,
                    "sim_flip": 0.42,
                }
            ],
        )

    with sqlite3.connect(layout.library_db_path) as conn:
        run_row = conn.execute(
            "SELECT status, finished_at FROM assignment_run ORDER BY id DESC LIMIT 1",
        ).fetchone()
    assert run_row is not None
    assert run_row[0] == "failed"
    assert run_row[1] is not None
