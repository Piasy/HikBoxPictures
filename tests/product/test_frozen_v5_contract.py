from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.engine.frozen_v5 import FROZEN_V5_STAGE_SEQUENCE, FrozenV5Executor
from hikbox_pictures.product.engine.param_snapshot import (
    AHC_PASS_2_TIE_BREAK,
    ALGORITHM_VERSION,
    IGNORED_ASSIGNMENT_SOURCES,
    LATE_FUSION_MISSING_SIMILARITY,
    UNKNOWN_ASSIGNMENT_SOURCE_FALLBACK,
    PERSON_CONSENSUS_SIMILARITY_THRESHOLD,
    build_param_snapshot,
)
from hikbox_pictures.product.scan.assignment_stage import AssignmentStageService, FaceEmbeddingRecord


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


def _insert_photo_asset(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO library_source(root_path, label, enabled, status, last_discovered_at, created_at, updated_at)
            VALUES ('/tmp/src-frozen', 'src-frozen', 1, 'active', NULL, '2026-04-22T00:00:00+00:00', '2026-04-22T00:00:00+00:00')
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
            VALUES (1, 'IMG_FROZEN.HEIC', 'fp-frozen', 'sha256', 123, 456, NULL, NULL, 0, NULL, NULL, NULL, 'active', '2026-04-22T00:00:00+00:00', '2026-04-22T00:00:00+00:00')
            """
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


def test_param_snapshot_has_no_embedding_flip_weight() -> None:
    snapshot = build_param_snapshot()

    assert ALGORITHM_VERSION == "v5.2026-04-21"
    assert "embedding_flip_weight" not in snapshot
    assert snapshot["stage_sequence"] == list(FROZEN_V5_STAGE_SEQUENCE)
    assert snapshot["person_consensus_similarity_threshold"] == PERSON_CONSENSUS_SIMILARITY_THRESHOLD
    assert snapshot["late_fusion_missing_similarity"] == LATE_FUSION_MISSING_SIMILARITY
    assert snapshot["ahc_pass_2_tie_break"] == AHC_PASS_2_TIE_BREAK
    assert snapshot["ignored_assignment_sources"] == list(IGNORED_ASSIGNMENT_SOURCES)
    assert snapshot["unknown_assignment_source_fallback"] == UNKNOWN_ASSIGNMENT_SOURCE_FALLBACK


def test_main_and_flip_embeddings_persisted_in_embedding_db(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_session_id = _insert_scan_session(layout.library_db_path)
    photo_asset_id = _insert_photo_asset(layout.library_db_path)
    observation_id = _insert_face_observation(layout.library_db_path, photo_asset_id, 0)
    service = AssignmentStageService(layout.library_db_path, layout.embedding_db_path)

    service.start_assignment_run(scan_session_id=scan_session_id, run_kind="scan_full")
    service.persist_face_embeddings(
        [
            FaceEmbeddingRecord(
                face_observation_id=observation_id,
                main_embedding=[0.1] * 512,
                flip_embedding=[0.2] * 512,
            )
        ]
    )

    with sqlite3.connect(layout.embedding_db_path) as conn:
        rows = conn.execute(
            """
            SELECT face_observation_id, variant, dim, dtype
            FROM face_embedding
            WHERE face_observation_id=?
            ORDER BY variant
            """
            ,
            (observation_id,),
        ).fetchall()

    assert rows == [
        (observation_id, "flip", 512, "float32"),
        (observation_id, "main", 512, "float32"),
    ]


def test_face_embedding_upsert_preserves_created_at(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    photo_asset_id = _insert_photo_asset(layout.library_db_path)
    observation_id = _insert_face_observation(layout.library_db_path, photo_asset_id, 0)
    service = AssignmentStageService(layout.library_db_path, layout.embedding_db_path)

    service.persist_face_embeddings(
        [
            FaceEmbeddingRecord(
                face_observation_id=observation_id,
                main_embedding=[0.1] * 512,
                flip_embedding=[0.2] * 512,
            )
        ]
    )
    with sqlite3.connect(layout.embedding_db_path) as conn:
        first = conn.execute(
            """
            SELECT created_at
            FROM face_embedding
            WHERE face_observation_id=? AND variant='main'
            """,
            (observation_id,),
        ).fetchone()
    assert first is not None
    first_created_at = str(first[0])

    time.sleep(0.02)
    service.persist_face_embeddings(
        [
            FaceEmbeddingRecord(
                face_observation_id=observation_id,
                main_embedding=[0.3] * 512,
                flip_embedding=[0.4] * 512,
            )
        ]
    )
    with sqlite3.connect(layout.embedding_db_path) as conn:
        second = conn.execute(
            """
            SELECT created_at
            FROM face_embedding
            WHERE face_observation_id=? AND variant='main'
            """,
            (observation_id,),
        ).fetchone()
    assert second is not None
    assert str(second[0]) == first_created_at


def test_frozen_assignment_flow_does_not_create_flip_json_cache(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_session_id = _insert_scan_session(layout.library_db_path)
    service = AssignmentStageService(layout.library_db_path, layout.embedding_db_path)

    service.run_frozen_v5_assignment(
        scan_session_id=scan_session_id,
        run_kind="scan_full",
        executor_inputs=[],
    )

    cache_path = (tmp_path / "workspace" / "cache" / "flip_embeddings.json").resolve()
    assert cache_path.exists() is False


def test_persist_face_embeddings_raises_when_observation_missing_and_writes_nothing(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    service = AssignmentStageService(layout.library_db_path, layout.embedding_db_path)

    with pytest.raises(ValueError, match="face_observation 不存在"):
        service.persist_face_embeddings(
            [
                FaceEmbeddingRecord(
                    face_observation_id=99999,
                    main_embedding=[0.1] * 512,
                    flip_embedding=[0.2] * 512,
                )
            ]
        )

    with sqlite3.connect(layout.embedding_db_path) as conn:
        row = conn.execute("SELECT COUNT(1) FROM face_embedding").fetchone()
    assert row is not None
    assert int(row[0]) == 0


def test_persist_face_embeddings_raises_when_observation_inactive(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    photo_asset_id = _insert_photo_asset(layout.library_db_path)
    observation_id = _insert_face_observation(layout.library_db_path, photo_asset_id, 0)
    with sqlite3.connect(layout.library_db_path) as conn:
        conn.execute(
            "UPDATE face_observation SET active=0, inactive_reason='manual_drop' WHERE id=?",
            (observation_id,),
        )
        conn.commit()
    service = AssignmentStageService(layout.library_db_path, layout.embedding_db_path)

    with pytest.raises(ValueError, match="face_observation 已失效"):
        service.persist_face_embeddings(
            [
                FaceEmbeddingRecord(
                    face_observation_id=observation_id,
                    main_embedding=[0.1] * 512,
                    flip_embedding=[0.2] * 512,
                )
            ]
        )

    with sqlite3.connect(layout.embedding_db_path) as conn:
        row = conn.execute("SELECT COUNT(1) FROM face_embedding").fetchone()
    assert row is not None
    assert int(row[0]) == 0


def test_persist_face_embeddings_raises_when_vector_contains_nan_or_inf(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    photo_asset_id = _insert_photo_asset(layout.library_db_path)
    observation_id = _insert_face_observation(layout.library_db_path, photo_asset_id, 0)
    service = AssignmentStageService(layout.library_db_path, layout.embedding_db_path)

    with pytest.raises(ValueError, match="NaN/Inf"):
        service.persist_face_embeddings(
            [
                FaceEmbeddingRecord(
                    face_observation_id=observation_id,
                    main_embedding=[0.1] * 511 + [float("nan")],
                    flip_embedding=[0.2] * 512,
                )
            ]
        )
    with pytest.raises(ValueError, match="NaN/Inf"):
        service.persist_face_embeddings(
            [
                FaceEmbeddingRecord(
                    face_observation_id=observation_id,
                    main_embedding=[0.1] * 512,
                    flip_embedding=[0.2] * 511 + [float("inf")],
                )
            ]
        )

    with sqlite3.connect(layout.embedding_db_path) as conn:
        row = conn.execute("SELECT COUNT(1) FROM face_embedding").fetchone()
    assert row is not None
    assert int(row[0]) == 0


def test_frozen_v5_stage_semantics_and_source_normalization_and_late_fusion() -> None:
    call_order: list[str] = []

    class _TrackingExecutor(FrozenV5Executor):
        def _run_ahc_pass_1(self, rows: list[dict[str, object]]) -> list[dict[str, object]]:
            call_order.append("ahc_pass_1")
            return super()._run_ahc_pass_1(rows)

        def _run_ahc_pass_2(self, rows: list[dict[str, object]]) -> list[dict[str, object]]:
            call_order.append("ahc_pass_2")
            return super()._run_ahc_pass_2(rows)

        def _run_person_consensus(self, rows: list[dict[str, object]]) -> list[dict[str, object]]:
            call_order.append("person_consensus")
            return super()._run_person_consensus(rows)

        def _run_person_cluster_recall(self, rows: list[dict[str, object]]) -> list[dict[str, object]]:
            call_order.append("person_cluster_recall")
            return super()._run_person_cluster_recall(rows)

    executor = _TrackingExecutor()
    result = executor.execute(
        [
            {
                "face_observation_id": 1,
                "person_id": 2,
                "assignment_source": "person_consensus_candidate",
                "sim_main": 0.31,
                "sim_flip": 0.82,
            },
            {
                "face_observation_id": 1,
                "person_id": 3,
                "assignment_source": "hdbscan",
                "sim_main": 0.65,
                "sim_flip": 0.66,
            },
            {
                "face_observation_id": 2,
                "person_id": 4,
                "assignment_source": "recall_candidate",
                "sim_main": 0.28,
                "sim_flip": 0.79,
            },
            {
                "face_observation_id": 3,
                "person_id": 5,
                "assignment_source": "person_cluster_recall",
                "sim_main": 0.71,
                "sim_flip": 0.72,
            },
        ]
    )

    assert call_order == list(FROZEN_V5_STAGE_SEQUENCE)
    assert len(result) == 3

    by_face = {item.face_observation_id: item for item in result}
    assert by_face[1].person_id == 2
    assert by_face[1].assignment_source == "person_consensus"
    assert by_face[1].similarity == 0.82

    assert by_face[2].assignment_source == "recall"
    assert by_face[2].similarity == 0.79

    assert by_face[3].assignment_source == "recall"
    assert by_face[3].similarity == 0.72


def test_frozen_v5_low_score_person_consensus_candidate_downgrades_to_hdbscan() -> None:
    executor = FrozenV5Executor()
    result = executor.execute(
        [
            {
                "face_observation_id": 11,
                "person_id": 2,
                "assignment_source": "person_consensus_candidate",
                "sim_main": 0.33,
                "sim_flip": 0.79,
            }
        ]
    )
    assert len(result) == 1
    assert result[0].assignment_source == "hdbscan"


def test_run_frozen_v5_assignment_marks_run_failed_when_executor_raises(tmp_path: Path) -> None:
    class _FailingExecutor(FrozenV5Executor):
        def execute(self, candidates: list[dict[str, object]]) -> list[object]:  # type: ignore[override]
            raise RuntimeError("executor boom")

    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_session_id = _insert_scan_session(layout.library_db_path)
    service = AssignmentStageService(
        layout.library_db_path,
        layout.embedding_db_path,
        executor=_FailingExecutor(),
    )

    with pytest.raises(RuntimeError, match="executor boom"):
        service.run_frozen_v5_assignment(
            scan_session_id=scan_session_id,
            run_kind="scan_full",
            executor_inputs=[],
        )

    with sqlite3.connect(layout.library_db_path) as conn:
        runs = conn.execute("SELECT status, finished_at FROM assignment_run ORDER BY id").fetchall()
    assert len(runs) == 1
    assert runs[0][0] == "failed"
    assert runs[0][1] is not None


def test_run_frozen_v5_assignment_marks_run_failed_when_executor_keyboard_interrupt(tmp_path: Path) -> None:
    class _InterruptExecutor(FrozenV5Executor):
        def execute(self, candidates: list[dict[str, object]]) -> list[object]:  # type: ignore[override]
            raise KeyboardInterrupt()

    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    scan_session_id = _insert_scan_session(layout.library_db_path)
    service = AssignmentStageService(
        layout.library_db_path,
        layout.embedding_db_path,
        executor=_InterruptExecutor(),
    )

    with pytest.raises(KeyboardInterrupt):
        service.run_frozen_v5_assignment(
            scan_session_id=scan_session_id,
            run_kind="scan_full",
            executor_inputs=[],
        )

    with sqlite3.connect(layout.library_db_path) as conn:
        runs = conn.execute("SELECT status, finished_at FROM assignment_run ORDER BY id").fetchall()
    assert len(runs) == 1
    assert runs[0][0] == "failed"
    assert runs[0][1] is not None


def test_frozen_v5_handles_none_similarity_values_without_crash() -> None:
    executor = FrozenV5Executor()
    result = executor.execute(
        [
            {
                "face_observation_id": 21,
                "person_id": 7,
                "assignment_source": "hdbscan",
                "sim_main": None,
                "sim_flip": 0.42,
            },
            {
                "face_observation_id": 22,
                "person_id": 8,
                "assignment_source": "hdbscan",
                "sim_main": None,
                "sim_flip": None,
            },
        ]
    )
    by_face = {item.face_observation_id: item for item in result}
    assert by_face[21].sim_main == LATE_FUSION_MISSING_SIMILARITY
    assert by_face[21].similarity == 0.42
    assert by_face[22].sim_main == LATE_FUSION_MISSING_SIMILARITY
    assert by_face[22].sim_flip == LATE_FUSION_MISSING_SIMILARITY
    assert by_face[22].similarity == LATE_FUSION_MISSING_SIMILARITY


def test_frozen_v5_ahc_pass_2_tie_break_is_stable_against_input_order() -> None:
    executor = FrozenV5Executor()
    candidate_a = {
        "face_observation_id": 31,
        "person_id": 10,
        "assignment_source": "z_source",
        "sim_main": 0.75,
        "sim_flip": 0.75,
    }
    candidate_b = {
        "face_observation_id": 31,
        "person_id": 9,
        "assignment_source": "a_source",
        "sim_main": 0.75,
        "sim_flip": 0.75,
    }

    result_1 = executor.execute([candidate_a, candidate_b])
    result_2 = executor.execute([candidate_b, candidate_a])

    assert len(result_1) == 1
    assert len(result_2) == 1
    assert result_1[0].person_id == 9
    assert result_2[0].person_id == 9
    assert result_1[0].assignment_source == result_2[0].assignment_source

    source_x = {
        "face_observation_id": 32,
        "person_id": 20,
        "assignment_source": "undo",
        "sim_main": 0.66,
        "sim_flip": 0.66,
    }
    source_y = {
        "face_observation_id": 32,
        "person_id": 20,
        "assignment_source": "merge",
        "sim_main": 0.66,
        "sim_flip": 0.66,
    }
    result_3 = executor.execute([source_x, source_y])
    result_4 = executor.execute([source_y, source_x])
    assert len(result_3) == 1
    assert len(result_4) == 1
    assert result_3[0].assignment_source == "merge"
    assert result_4[0].assignment_source == "merge"


def test_frozen_v5_ignored_source_allows_missing_person_id() -> None:
    executor = FrozenV5Executor()
    result = executor.execute(
        [
            {
                "face_observation_id": 41,
                "assignment_source": "noise",
                "sim_main": 0.12,
                "sim_flip": 0.2,
            }
        ]
    )
    assert len(result) == 1
    assert result[0].assignment_source == "noise"
    assert result[0].person_id is None


def test_frozen_v5_non_ignored_source_missing_person_id_raises_value_error() -> None:
    executor = FrozenV5Executor()
    with pytest.raises(ValueError, match="缺少 person_id"):
        executor.execute(
            [
                {
                    "face_observation_id": 42,
                    "assignment_source": "hdbscan",
                    "sim_main": 0.41,
                    "sim_flip": 0.52,
                }
            ]
        )


def test_frozen_v5_rejects_nan_or_inf_similarity_input() -> None:
    executor = FrozenV5Executor()
    with pytest.raises(ValueError, match="similarity 非法"):
        executor.execute(
            [
                {
                    "face_observation_id": 51,
                    "person_id": 1,
                    "assignment_source": "hdbscan",
                    "sim_main": float("nan"),
                    "sim_flip": 0.2,
                }
            ]
        )
    with pytest.raises(ValueError, match="similarity 非法"):
        executor.execute(
            [
                {
                    "face_observation_id": 52,
                    "person_id": 1,
                    "assignment_source": "hdbscan",
                    "sim_main": 0.2,
                    "sim_flip": float("inf"),
                }
            ]
        )


def test_frozen_v5_rejects_non_integral_ids() -> None:
    executor = FrozenV5Executor()
    with pytest.raises(ValueError, match="face_observation_id 非法"):
        executor.execute(
            [
                {
                    "face_observation_id": 1.0,
                    "person_id": 1,
                    "assignment_source": "hdbscan",
                    "sim_main": 0.2,
                    "sim_flip": 0.3,
                }
            ]
        )
    with pytest.raises(ValueError, match="face_observation_id 非法"):
        executor.execute(
            [
                {
                    "face_observation_id": 1.9,
                    "person_id": 1,
                    "assignment_source": "hdbscan",
                    "sim_main": 0.2,
                    "sim_flip": 0.3,
                }
            ]
        )
    with pytest.raises(ValueError, match="person_id 非法"):
        executor.execute(
            [
                {
                    "face_observation_id": 1,
                    "person_id": 1.0,
                    "assignment_source": "hdbscan",
                    "sim_main": 0.2,
                    "sim_flip": 0.3,
                }
            ]
        )
    with pytest.raises(ValueError, match="person_id 非法"):
        executor.execute(
            [
                {
                    "face_observation_id": 1,
                    "person_id": 1.9,
                    "assignment_source": "hdbscan",
                    "sim_main": 0.2,
                    "sim_flip": 0.3,
                }
            ]
        )
    with pytest.raises(ValueError, match="face_observation_id 非法"):
        executor.execute(
            [
                {
                    "face_observation_id": True,
                    "person_id": 1,
                    "assignment_source": "hdbscan",
                    "sim_main": 0.2,
                    "sim_flip": 0.3,
                }
            ]
        )
    with pytest.raises(ValueError, match="person_id 非法"):
        executor.execute(
            [
                {
                    "face_observation_id": 1,
                    "person_id": False,
                    "assignment_source": "hdbscan",
                    "sim_main": 0.2,
                    "sim_flip": 0.3,
                }
            ]
        )


def test_persist_face_embeddings_rejects_duplicate_observation_ids(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    photo_asset_id = _insert_photo_asset(layout.library_db_path)
    observation_id = _insert_face_observation(layout.library_db_path, photo_asset_id, 0)
    service = AssignmentStageService(layout.library_db_path, layout.embedding_db_path)

    with pytest.raises(ValueError, match="重复 face_observation_id"):
        service.persist_face_embeddings(
            [
                FaceEmbeddingRecord(
                    face_observation_id=observation_id,
                    main_embedding=[0.1] * 512,
                    flip_embedding=[0.2] * 512,
                ),
                FaceEmbeddingRecord(
                    face_observation_id=observation_id,
                    main_embedding=[0.3] * 512,
                    flip_embedding=[0.4] * 512,
                ),
            ]
        )

    with sqlite3.connect(layout.embedding_db_path) as conn:
        row = conn.execute("SELECT COUNT(1) FROM face_embedding").fetchone()
    assert row is not None
    assert int(row[0]) == 0


def test_persist_face_embeddings_rejects_non_strict_face_observation_id(tmp_path: Path) -> None:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    service = AssignmentStageService(layout.library_db_path, layout.embedding_db_path)

    with pytest.raises(ValueError, match="persist_face_embeddings.face_observation_id 非法"):
        service.persist_face_embeddings(
            [
                FaceEmbeddingRecord(
                    face_observation_id=1.0,  # type: ignore[arg-type]
                    main_embedding=[0.1] * 512,
                    flip_embedding=[0.2] * 512,
                )
            ]
        )
    with pytest.raises(ValueError, match="persist_face_embeddings.face_observation_id 非法"):
        service.persist_face_embeddings(
            [
                FaceEmbeddingRecord(
                    face_observation_id=True,  # type: ignore[arg-type]
                    main_embedding=[0.1] * 512,
                    flip_embedding=[0.2] * 512,
                )
            ]
        )

    with sqlite3.connect(layout.embedding_db_path) as conn:
        row = conn.execute("SELECT COUNT(1) FROM face_embedding").fetchone()
    assert row is not None
    assert int(row[0]) == 0
