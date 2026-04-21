from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

from hikbox_pictures.product.db.connection import connect_sqlite
from hikbox_pictures.product.engine.frozen_v5 import FrozenV5Executor
from hikbox_pictures.product.engine.param_snapshot import (
    ALGORITHM_VERSION,
    IGNORED_ASSIGNMENT_SOURCES,
    build_param_snapshot,
)

OBSERVATION_VALIDATE_CHUNK_SIZE = 500


@dataclass(frozen=True)
class AssignmentRunRecord:
    id: int
    scan_session_id: int
    run_kind: str
    status: str
    algorithm_version: str
    param_snapshot_json: dict[str, object]
    started_at: str
    finished_at: str | None


@dataclass(frozen=True)
class AssignmentCandidate:
    face_observation_id: int
    person_id: int | None
    assignment_source: str
    similarity: float


@dataclass(frozen=True)
class FaceEmbeddingRecord:
    face_observation_id: int
    main_embedding: Sequence[float]
    flip_embedding: Sequence[float]


class AssignmentStageService:
    def __init__(
        self,
        library_db_path: Path,
        embedding_db_path: Path,
        *,
        executor: FrozenV5Executor | None = None,
    ) -> None:
        self._library_db_path = library_db_path
        self._embedding_db_path = embedding_db_path
        self._executor = executor or FrozenV5Executor()

    def start_assignment_run(
        self,
        *,
        scan_session_id: int,
        run_kind: str,
    ) -> AssignmentRunRecord:
        with connect_sqlite(self._library_db_path) as conn:
            session_row = conn.execute(
                "SELECT run_kind FROM scan_session WHERE id=?",
                (scan_session_id,),
            ).fetchone()
        if session_row is None:
            raise ValueError(f"scan_session 不存在: {scan_session_id}")
        session_run_kind = str(session_row[0])
        if session_run_kind != run_kind:
            raise ValueError(
                f"run_kind 不匹配: session={session_run_kind}, requested={run_kind}, scan_session_id={scan_session_id}"
            )

        started_at = _utc_now()
        snapshot = build_param_snapshot()
        with connect_sqlite(self._library_db_path) as conn:
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
                VALUES (?, ?, ?, ?, ?, NULL, 'running')
                """,
                (
                    scan_session_id,
                    ALGORITHM_VERSION,
                    json.dumps(snapshot, ensure_ascii=False),
                    run_kind,
                    started_at,
                ),
            )
            conn.commit()
            run_id = int(cursor.lastrowid)
        return AssignmentRunRecord(
            id=run_id,
            scan_session_id=scan_session_id,
            run_kind=run_kind,
            status="running",
            algorithm_version=ALGORITHM_VERSION,
            param_snapshot_json=snapshot,
            started_at=started_at,
            finished_at=None,
        )

    def persist_face_embeddings(self, records: Sequence[FaceEmbeddingRecord], *, model_key: str = "magface-iresnet100") -> None:
        face_ids = [
            _coerce_strict_int(record.face_observation_id, field_name="persist_face_embeddings.face_observation_id")
            for record in records
        ]
        counts: dict[int, int] = {}
        for face_id in face_ids:
            counts[face_id] = counts.get(face_id, 0) + 1
        repeated_ids = sorted(face_id for face_id, count in counts.items() if count > 1)
        if repeated_ids:
            raise ValueError(f"persist_face_embeddings 存在重复 face_observation_id: {repeated_ids}")

        observation_ids = set(face_ids)
        if observation_ids:
            self._validate_observation_ids_active(observation_ids)

        with connect_sqlite(self._embedding_db_path) as conn:
            for record in records:
                main_vector = _to_float32_vector(record.main_embedding)
                flip_vector = _to_float32_vector(record.flip_embedding)
                self._upsert_embedding(
                    conn=conn,
                    face_observation_id=_coerce_strict_int(
                        record.face_observation_id,
                        field_name="persist_face_embeddings.face_observation_id",
                    ),
                    model_key=model_key,
                    variant="main",
                    vector=main_vector,
                )
                self._upsert_embedding(
                    conn=conn,
                    face_observation_id=_coerce_strict_int(
                        record.face_observation_id,
                        field_name="persist_face_embeddings.face_observation_id",
                    ),
                    model_key=model_key,
                    variant="flip",
                    vector=flip_vector,
                )
            conn.commit()

    def run_assignment(
        self,
        *,
        scan_session_id: int,
        run_kind: str,
        candidates: Sequence[AssignmentCandidate],
    ) -> AssignmentRunRecord:
        run = self.start_assignment_run(scan_session_id=scan_session_id, run_kind=run_kind)
        try:
            return self._run_assignment_with_existing_run(run=run, candidates=candidates)
        except BaseException:
            self._mark_assignment_run_failed(run.id)
            raise

    def run_frozen_v5_assignment(
        self,
        *,
        scan_session_id: int,
        run_kind: str,
        executor_inputs: Iterable[dict[str, object]],
    ) -> AssignmentRunRecord:
        run = self.start_assignment_run(scan_session_id=scan_session_id, run_kind=run_kind)
        try:
            frozen_candidates = self._executor.execute(executor_inputs)
            candidates = [
                AssignmentCandidate(
                    face_observation_id=item.face_observation_id,
                    person_id=item.person_id,
                    assignment_source=item.assignment_source,
                    similarity=item.similarity,
                )
                for item in frozen_candidates
            ]
            self._validate_frozen_candidates_embeddings_ready(candidates)
            return self._run_assignment_with_existing_run(run=run, candidates=candidates)
        except BaseException:
            self._mark_assignment_run_failed(run.id)
            raise

    def _run_assignment_with_existing_run(
        self,
        *,
        run: AssignmentRunRecord,
        candidates: Sequence[AssignmentCandidate],
    ) -> AssignmentRunRecord:
        self._validate_assignment_candidates(candidates)
        now = _utc_now()
        with connect_sqlite(self._library_db_path) as conn:
            for candidate in candidates:
                face_observation_id = _coerce_strict_int(
                    candidate.face_observation_id,
                    field_name="run_assignment.face_observation_id",
                )
                conn.execute(
                    """
                    UPDATE person_face_assignment
                    SET active=0,
                        updated_at=?
                    WHERE face_observation_id=?
                      AND active=1
                    """,
                    (now, face_observation_id),
                )
                if candidate.assignment_source in IGNORED_ASSIGNMENT_SOURCES:
                    continue
                if candidate.person_id is None:
                    raise ValueError(f"assignment_source={candidate.assignment_source} 缺少 person_id")
                person_id = _coerce_strict_int(
                    candidate.person_id,
                    field_name="run_assignment.person_id",
                )
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
                    VALUES (?, ?, ?, ?, 1, ?, NULL, ?, ?)
                    """,
                    (
                        person_id,
                        face_observation_id,
                        run.id,
                        candidate.assignment_source,
                        float(candidate.similarity),
                        now,
                        now,
                    ),
                )
            conn.execute(
                """
                UPDATE assignment_run
                SET status='completed',
                    finished_at=?
                WHERE id=?
                """,
                (now, run.id),
            )
            conn.commit()
        return AssignmentRunRecord(
            id=run.id,
            scan_session_id=run.scan_session_id,
            run_kind=run.run_kind,
            status="completed",
            algorithm_version=run.algorithm_version,
            param_snapshot_json=run.param_snapshot_json,
            started_at=run.started_at,
            finished_at=now,
        )

    def _validate_assignment_candidates(self, candidates: Sequence[AssignmentCandidate]) -> None:
        face_ids = [
            _coerce_strict_int(candidate.face_observation_id, field_name="run_assignment.face_observation_id")
            for candidate in candidates
        ]
        for candidate in candidates:
            try:
                similarity = float(candidate.similarity)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"candidate similarity 非法: {candidate.similarity!r}") from exc
            if not math.isfinite(similarity):
                raise ValueError(f"candidate similarity 非法: {candidate.similarity!r}")

        counts: dict[int, int] = {}
        for face_id in face_ids:
            counts[face_id] = counts.get(face_id, 0) + 1
        repeated_ids = sorted(face_id for face_id, count in counts.items() if count > 1)
        if repeated_ids:
            raise ValueError(f"assignment candidates 存在重复 face_observation_id: {repeated_ids}")

        observation_ids = set(face_ids)
        if not observation_ids:
            return
        self._validate_observation_ids_active(observation_ids)

    def _validate_frozen_candidates_embeddings_ready(
        self,
        candidates: Sequence[AssignmentCandidate],
        *,
        model_key: str = "magface-iresnet100",
    ) -> None:
        target_face_ids = sorted(
            {
                _coerce_strict_int(candidate.face_observation_id, field_name="run_frozen_v5_assignment.face_observation_id")
                for candidate in candidates
                if candidate.assignment_source not in IGNORED_ASSIGNMENT_SOURCES
            }
        )
        if not target_face_ids:
            return

        variants_by_face: dict[int, set[str]] = {face_id: set() for face_id in target_face_ids}
        with connect_sqlite(self._embedding_db_path) as conn:
            for idx in range(0, len(target_face_ids), OBSERVATION_VALIDATE_CHUNK_SIZE):
                chunk = target_face_ids[idx : idx + OBSERVATION_VALIDATE_CHUNK_SIZE]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"""
                    SELECT face_observation_id, variant
                    FROM face_embedding
                    WHERE feature_type='face'
                      AND model_key=?
                      AND variant IN ('main', 'flip')
                      AND face_observation_id IN ({placeholders})
                    """,
                    (model_key, *chunk),
                ).fetchall()
                for row in rows:
                    variants_by_face[int(row[0])].add(str(row[1]))

        missing_ids = sorted(face_id for face_id, variants in variants_by_face.items() if variants != {"main", "flip"})
        if missing_ids:
            raise ValueError(f"embedding 缺失(main/flip): {missing_ids}")

    def _validate_observation_ids_active(self, observation_ids: set[int]) -> None:
        sorted_ids = sorted(observation_ids)
        rows: list[sqlite3.Row | tuple[object, ...]] = []
        with connect_sqlite(self._library_db_path) as conn:
            for idx in range(0, len(sorted_ids), OBSERVATION_VALIDATE_CHUNK_SIZE):
                chunk = sorted_ids[idx : idx + OBSERVATION_VALIDATE_CHUNK_SIZE]
                placeholders = ",".join("?" for _ in chunk)
                rows.extend(
                    conn.execute(
                        f"SELECT id, active FROM face_observation WHERE id IN ({placeholders})",
                        tuple(chunk),
                    ).fetchall()
                )
        existing_ids = {int(row[0]) for row in rows}
        active_ids = {int(row[0]) for row in rows if int(row[1]) == 1}
        missing_ids = sorted(observation_ids - existing_ids)
        if missing_ids:
            raise ValueError(f"face_observation 不存在: {missing_ids}")
        inactive_ids = sorted(observation_ids - active_ids)
        if inactive_ids:
            raise ValueError(f"face_observation 已失效: {inactive_ids}")

    def _mark_assignment_run_failed(self, run_id: int) -> None:
        finished_at = _utc_now()
        with connect_sqlite(self._library_db_path) as conn:
            conn.execute(
                """
                UPDATE assignment_run
                SET status='failed',
                    finished_at=?
                WHERE id=?
                  AND status='running'
                """,
                (finished_at, run_id),
            )
            conn.commit()

    def _upsert_embedding(
        self,
        *,
        conn: sqlite3.Connection,
        face_observation_id: int,
        model_key: str,
        variant: str,
        vector: np.ndarray,
    ) -> None:
        now = _utc_now()
        conn.execute(
            """
            INSERT INTO face_embedding(
                face_observation_id,
                feature_type,
                model_key,
                variant,
                dim,
                dtype,
                vector_blob,
                created_at
            )
            VALUES (?, 'face', ?, ?, 512, 'float32', ?, ?)
            ON CONFLICT(face_observation_id, feature_type, model_key, variant)
            DO UPDATE SET vector_blob=excluded.vector_blob
            """,
            (
                _coerce_strict_int(face_observation_id, field_name="upsert_embedding.face_observation_id"),
                model_key,
                variant,
                vector.tobytes(),
                now,
            ),
        )


def _to_float32_vector(values: Sequence[float]) -> np.ndarray:
    vector = np.asarray(values, dtype=np.float32)
    if vector.shape != (512,):
        raise ValueError("embedding 维度必须为 512")
    if not np.all(np.isfinite(vector)):
        raise ValueError("embedding 向量包含 NaN/Inf")
    return vector


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _coerce_strict_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} 非法: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise ValueError(f"{field_name} 非法: {value!r}")
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and (stripped.isdigit() or (stripped[0] in "+-" and stripped[1:].isdigit())):
            return int(stripped)
        raise ValueError(f"{field_name} 非法: {value!r}")
    raise ValueError(f"{field_name} 非法: {value!r}")
