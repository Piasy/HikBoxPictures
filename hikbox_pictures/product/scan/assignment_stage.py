"""assignment 阶段：冻结 v5 链路执行与落库。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

from hikbox_pictures.product.db.connection import connect_sqlite
from hikbox_pictures.product.engine.param_snapshot import build_frozen_v5_param_snapshot
from hikbox_pictures.product.engine.frozen_v5 import run_frozen_v5_assignment

ALLOWED_ASSIGNMENT_SOURCES = {"hdbscan", "person_consensus", "merge", "undo"}
UNASSIGNED_SOURCES = {"noise", "low_quality_ignored"}


@dataclass(frozen=True)
class AssignmentRunStart:
    assignment_run_id: int
    param_snapshot: dict[str, object]


@dataclass(frozen=True)
class AssignmentStageResult:
    assignment_run_id: int
    person_count: int
    assignment_count: int


class AssignmentAbortedError(RuntimeError):
    """assignment 执行期间收到 abort。"""


class AssignmentStageService:
    """冻结链路执行服务。"""

    def __init__(
        self,
        *,
        library_db_path: Path,
        embedding_db_path: Path,
        output_root: Path,
    ):
        self._library_db_path = Path(library_db_path)
        self._embedding_db_path = Path(embedding_db_path)
        self._output_root = Path(output_root)

    def start_assignment_run(self, *, scan_session_id: int, run_kind: str) -> AssignmentRunStart:
        snapshot = build_frozen_v5_param_snapshot()
        conn = connect_sqlite(self._library_db_path)
        try:
            cursor = conn.execute(
                """
                INSERT INTO assignment_run(
                  scan_session_id, algorithm_version, param_snapshot_json, run_kind,
                  started_at, finished_at, status
                ) VALUES (?, 'frozen_v5', ?, ?, ?, NULL, 'running')
                """,
                (
                    int(scan_session_id),
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                    str(run_kind),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
            return AssignmentRunStart(assignment_run_id=int(cursor.lastrowid), param_snapshot=snapshot)
        finally:
            conn.close()

    def run_frozen_v5_assignment(
        self,
        *,
        scan_session_id: int,
        run_kind: str,
        embedding_calculator=None,
    ) -> AssignmentStageResult:
        started = self.start_assignment_run(scan_session_id=scan_session_id, run_kind=run_kind)
        try:
            self._ensure_not_aborting(scan_session_id=scan_session_id)
            faces = self._build_face_inputs(scan_session_id=scan_session_id, embedding_calculator=embedding_calculator)
            self._persist_embeddings(scan_session_id=scan_session_id, faces=faces)
            self._ensure_not_aborting(scan_session_id=scan_session_id)

            runtime_result = run_frozen_v5_assignment(faces=faces, params=started.param_snapshot)
            self._ensure_not_aborting(scan_session_id=scan_session_id)
            person_count, assignment_count = self._persist_assignment_outcome(
                scan_session_id=scan_session_id,
                assignment_run_id=started.assignment_run_id,
                person_rows=list(runtime_result.get("persons", [])),
                assignment_rows=list(runtime_result.get("faces", [])),
            )
            return AssignmentStageResult(
                assignment_run_id=started.assignment_run_id,
                person_count=int(person_count),
                assignment_count=int(assignment_count),
            )
        except Exception:
            self._complete_assignment_run(assignment_run_id=started.assignment_run_id, status="failed")
            raise

    def _build_face_inputs(self, *, scan_session_id: int, embedding_calculator=None) -> list[dict[str, object]]:
        conn = connect_sqlite(self._library_db_path)
        try:
            rows = conn.execute(
                """
                SELECT
                  f.id,
                  f.photo_asset_id,
                  f.aligned_relpath,
                  f.quality_score
                FROM face_observation AS f
                INNER JOIN photo_asset AS p ON p.id = f.photo_asset_id
                INNER JOIN scan_session_source AS s
                  ON s.library_source_id = p.library_source_id
                 AND s.scan_session_id = ?
                WHERE f.active=1
                ORDER BY f.id ASC
                """,
                (int(scan_session_id),),
            ).fetchall()
        finally:
            conn.close()

        faces: list[dict[str, object]] = []
        calculator = embedding_calculator or _default_embedding_calculator
        for row in rows:
            observation_id = int(row[0])
            photo_asset_id = int(row[1])
            aligned_relpath = str(row[2])
            quality_score = float(row[3])
            aligned_path = self._output_root / aligned_relpath
            embedding_main, embedding_flip = calculator(aligned_path)
            faces.append(
                {
                    "face_observation_id": observation_id,
                    "photo_asset_id": photo_asset_id,
                    "photo_relpath": f"asset-{photo_asset_id}",
                    "quality_score": quality_score,
                    "embedding_main": embedding_main,
                    "embedding_flip": embedding_flip,
                }
            )
        return faces

    def _persist_embeddings(self, *, scan_session_id: int, faces: list[dict[str, object]]) -> None:
        conn = connect_sqlite(self._embedding_db_path)
        try:
            conn.execute("BEGIN")
            for idx, row in enumerate(faces, start=1):
                if idx % 16 == 0:
                    self._ensure_not_aborting(scan_session_id=scan_session_id)
                face_observation_id = int(row["face_observation_id"])
                self._upsert_embedding_row(
                    conn,
                    face_observation_id=face_observation_id,
                    variant="main",
                    vector=np.asarray(row["embedding_main"], dtype=np.float32),
                )
                self._upsert_embedding_row(
                    conn,
                    face_observation_id=face_observation_id,
                    variant="flip",
                    vector=np.asarray(row["embedding_flip"], dtype=np.float32),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _upsert_embedding_row(self, conn: sqlite3.Connection, *, face_observation_id: int, variant: str, vector: np.ndarray) -> None:
        safe_vector = _normalize_vector(vector)
        conn.execute(
            """
            INSERT INTO face_embedding(
              face_observation_id, feature_type, model_key, variant, dim, dtype, vector_blob, created_at
            ) VALUES (?, 'face', 'frozen_v5_pixel_v1', ?, 512, 'float32', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(face_observation_id, feature_type, model_key, variant)
            DO UPDATE SET
              vector_blob=excluded.vector_blob,
              created_at=CURRENT_TIMESTAMP
            """,
            (
                int(face_observation_id),
                str(variant),
                safe_vector.astype(np.float32).tobytes(),
            ),
        )

    def _upsert_persons(self, person_rows: list[dict[str, object]], *, conn: sqlite3.Connection) -> dict[str, int]:
        person_map: dict[str, int] = {}
        for row in person_rows:
            person_temp_key = str(row.get("person_temp_key") or "")
            if not person_temp_key:
                continue
            cursor = conn.execute(
                """
                INSERT INTO person(
                  person_uuid, display_name, is_named, status, merged_into_person_id, created_at, updated_at
                ) VALUES (?, NULL, 0, 'active', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (str(uuid.uuid4()),),
            )
            person_map[person_temp_key] = int(cursor.lastrowid)
        return person_map

    def _persist_assignments(
        self,
        *,
        scan_session_id: int,
        assignment_rows: list[dict[str, object]],
        assignment_run_id: int,
        person_map: dict[str, int],
        conn: sqlite3.Connection,
    ) -> int:
        count = 0
        for idx, row in enumerate(assignment_rows, start=1):
            if idx % 16 == 0:
                self._ensure_not_aborting(scan_session_id=scan_session_id, conn=conn)
            source = str(row.get("assignment_source") or "")
            if source in UNASSIGNED_SOURCES:
                continue
            if source not in ALLOWED_ASSIGNMENT_SOURCES:
                raise ValueError(f"非法 assignment_source: {source}")

            person_temp_key = str(row.get("person_temp_key") or "")
            person_id = int(person_map.get(person_temp_key, 0))
            face_observation_id = int(row.get("face_observation_id") or 0)
            if person_id <= 0 or face_observation_id <= 0:
                continue

            conn.execute(
                "UPDATE person_face_assignment SET active=0, updated_at=CURRENT_TIMESTAMP WHERE face_observation_id=? AND active=1",
                (face_observation_id,),
            )
            conn.execute(
                """
                INSERT INTO person_face_assignment(
                  person_id, face_observation_id, assignment_run_id, assignment_source,
                  active, confidence, margin, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    person_id,
                    face_observation_id,
                    int(assignment_run_id),
                    source,
                    None if row.get("probability") is None else float(row["probability"]),
                ),
            )
            count += 1
        return count

    def _complete_assignment_run(
        self,
        *,
        assignment_run_id: int,
        status: str,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        db = conn or connect_sqlite(self._library_db_path)
        managed_conn = conn is None
        try:
            db.execute(
                """
                UPDATE assignment_run
                SET status=?,
                    finished_at=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (str(status), datetime.now().isoformat(timespec="seconds"), int(assignment_run_id)),
            )
            if managed_conn:
                db.commit()
        finally:
            if managed_conn:
                db.close()

    def _mark_session_sources_stage_done(self, *, scan_session_id: int, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT id, stage_status_json FROM scan_session_source WHERE scan_session_id=?",
            (int(scan_session_id),),
        ).fetchall()
        for row in rows:
            stage_status = json.loads(str(row[1]))
            stage_status.setdefault("discover", "done")
            stage_status.setdefault("metadata", "done")
            stage_status.setdefault("detect", "done")
            stage_status["embed"] = "done"
            stage_status["cluster"] = "done"
            stage_status["assignment"] = "done"
            conn.execute(
                "UPDATE scan_session_source SET stage_status_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (json.dumps(stage_status, ensure_ascii=False, sort_keys=True), int(row[0])),
            )
        for stage in ("embed", "cluster", "assignment"):
            conn.execute(
                """
                INSERT INTO scan_checkpoint(scan_session_id, stage, cursor_json, processed_count, updated_at)
                VALUES (?, ?, '{}', 0, CURRENT_TIMESTAMP)
                ON CONFLICT(scan_session_id, stage)
                DO UPDATE SET cursor_json=excluded.cursor_json, updated_at=CURRENT_TIMESTAMP
                """,
                (int(scan_session_id), stage),
            )

    def _persist_assignment_outcome(
        self,
        *,
        scan_session_id: int,
        assignment_run_id: int,
        person_rows: list[dict[str, object]],
        assignment_rows: list[dict[str, object]],
    ) -> tuple[int, int]:
        conn = connect_sqlite(self._library_db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_not_aborting(scan_session_id=scan_session_id, conn=conn)
            person_map = self._upsert_persons(person_rows, conn=conn)
            assignment_count = self._persist_assignments(
                scan_session_id=scan_session_id,
                assignment_rows=assignment_rows,
                assignment_run_id=assignment_run_id,
                person_map=person_map,
                conn=conn,
            )
            self._mark_session_sources_stage_done(scan_session_id=scan_session_id, conn=conn)
            self._complete_assignment_run(assignment_run_id=assignment_run_id, status="completed", conn=conn)
            conn.commit()
            return len(person_map), assignment_count
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_not_aborting(self, *, scan_session_id: int, conn: sqlite3.Connection | None = None) -> None:
        db = conn or connect_sqlite(self._library_db_path)
        managed_conn = conn is None
        try:
            row = db.execute("SELECT status FROM scan_session WHERE id=?", (int(scan_session_id),)).fetchone()
            if row is None:
                return
            status = str(row[0])
            if status == "aborting":
                raise AssignmentAbortedError(f"assignment aborted by user: session={scan_session_id}")
        finally:
            if managed_conn:
                db.close()


def _default_embedding_calculator(aligned_path: Path) -> tuple[list[float], list[float]]:
    if not aligned_path.exists():
        raise FileNotFoundError(f"aligned 文件不存在: {aligned_path}")
    image = Image.open(aligned_path).convert("L")
    try:
        base = image.resize((32, 16), Image.Resampling.BILINEAR)
        main = np.asarray(base, dtype=np.float32).reshape(-1)

        flip_img = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT).resize((32, 16), Image.Resampling.BILINEAR)
        flip = np.asarray(flip_img, dtype=np.float32).reshape(-1)

        main = _normalize_vector(main)
        flip = _normalize_vector(flip)
        return main.astype(float).tolist(), flip.astype(float).tolist()
    finally:
        image.close()


def _normalize_vector(vector: np.ndarray) -> np.ndarray:
    safe = np.asarray(vector, dtype=np.float32)
    if safe.shape[0] < 512:
        safe = np.pad(safe, (0, 512 - safe.shape[0]), mode="constant")
    elif safe.shape[0] > 512:
        safe = safe[:512]
    norm = float(np.linalg.norm(safe))
    if norm <= 1e-9:
        return safe.astype(np.float32)
    return (safe / norm).astype(np.float32)
