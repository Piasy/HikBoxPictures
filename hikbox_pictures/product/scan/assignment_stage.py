"""assignment 阶段：冻结 v5 链路执行与落库。"""

from __future__ import annotations

import os
import json
import sqlite3
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from hikbox_pictures.product.db.connection import connect_sqlite
from hikbox_pictures.product.engine.magface_embedder import MagFaceEmbedder
from hikbox_pictures.product.engine.param_snapshot import build_frozen_v5_param_snapshot
from hikbox_pictures.product.engine.frozen_v5 import run_frozen_v5_assignment
from hikbox_pictures.product.scan.cluster_repository import ClusterRepository
from hikbox_pictures.product.scan.incremental_assignment_service import IncrementalAssignmentService

ALLOWED_ASSIGNMENT_SOURCES = {"hdbscan", "person_consensus", "merge", "undo"}
UNASSIGNED_SOURCES = {"noise", "low_quality_ignored"}
EMBEDDING_MODEL_KEY = "magface_iresnet100_ms1mv2"
MAGFACE_CHECKPOINT_ENV = "HIKBOX_MAGFACE_CHECKPOINT"
MAGFACE_CHECKPOINT_DEFAULT = Path(".cache/magface/magface_iresnet100_ms1mv2.pth")


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


class IncrementalFallbackToFullRebuild(RuntimeError):
    """增量归属命中过多未归属样本，需要回退到 full rebuild。"""


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
        self._cluster_repo = ClusterRepository(self._library_db_path)

    def start_assignment_run(
        self,
        *,
        scan_session_id: int,
        run_kind: str,
        param_snapshot: dict[str, object] | None = None,
    ) -> AssignmentRunStart:
        snapshot = dict(param_snapshot or build_frozen_v5_param_snapshot())
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
        param_snapshot = build_frozen_v5_param_snapshot()
        use_incremental = self._should_run_incremental(
            scan_session_id=scan_session_id,
            run_kind=run_kind,
            param_snapshot=param_snapshot,
        )
        effective_run_kind = "scan_full" if str(run_kind) == "scan_incremental" and not use_incremental else str(run_kind)
        started = self.start_assignment_run(
            scan_session_id=scan_session_id,
            run_kind=effective_run_kind,
            param_snapshot=param_snapshot,
        )
        try:
            self._ensure_not_aborting(scan_session_id=scan_session_id)
            faces = self._build_face_inputs(
                scan_session_id=scan_session_id,
                param_snapshot=started.param_snapshot,
                embedding_calculator=embedding_calculator,
                include_all_active_sources=bool(str(run_kind) == "scan_incremental" and not use_incremental),
            )
            self._persist_embeddings(scan_session_id=scan_session_id, faces=faces)
            self._ensure_not_aborting(scan_session_id=scan_session_id)

            if use_incremental:
                try:
                    person_count, assignment_count = self._persist_incremental_assignment_outcome(
                        scan_session_id=scan_session_id,
                        assignment_run_id=started.assignment_run_id,
                        face_rows=faces,
                    )
                except IncrementalFallbackToFullRebuild:
                    full_faces = self._build_face_inputs(
                        scan_session_id=scan_session_id,
                        param_snapshot=started.param_snapshot,
                        embedding_calculator=embedding_calculator,
                        include_all_active_sources=True,
                    )
                    self._persist_embeddings(scan_session_id=scan_session_id, faces=full_faces)
                    self._ensure_not_aborting(scan_session_id=scan_session_id)
                    self._promote_assignment_run_to_full(assignment_run_id=started.assignment_run_id)
                    runtime_result = run_frozen_v5_assignment(faces=full_faces, params=started.param_snapshot)
                    self._ensure_not_aborting(scan_session_id=scan_session_id)
                    person_count, assignment_count = self._persist_assignment_outcome(
                        scan_session_id=scan_session_id,
                        assignment_run_id=started.assignment_run_id,
                        run_kind="scan_full",
                        face_rows=full_faces,
                        person_rows=list(runtime_result.get("persons", [])),
                        assignment_rows=list(runtime_result.get("faces", [])),
                        cluster_rows=list(runtime_result.get("clusters", [])),
                        rebuild_scope="full",
                    )
            else:
                runtime_result = run_frozen_v5_assignment(faces=faces, params=started.param_snapshot)
                self._ensure_not_aborting(scan_session_id=scan_session_id)
                person_count, assignment_count = self._persist_assignment_outcome(
                    scan_session_id=scan_session_id,
                    assignment_run_id=started.assignment_run_id,
                    run_kind=effective_run_kind,
                    face_rows=faces,
                    person_rows=list(runtime_result.get("persons", [])),
                    assignment_rows=list(runtime_result.get("faces", [])),
                    cluster_rows=list(runtime_result.get("clusters", [])),
                    rebuild_scope="full",
                )
            return AssignmentStageResult(
                assignment_run_id=started.assignment_run_id,
                person_count=int(person_count),
                assignment_count=int(assignment_count),
            )
        except Exception:
            self._complete_assignment_run(assignment_run_id=started.assignment_run_id, status="failed")
            raise

    def _build_face_inputs(
        self,
        *,
        scan_session_id: int,
        param_snapshot: dict[str, object],
        embedding_calculator=None,
        include_all_active_sources: bool = False,
    ) -> list[dict[str, object]]:
        conn = connect_sqlite(self._library_db_path)
        try:
            if include_all_active_sources:
                rows = conn.execute(
                    """
                    SELECT
                      f.id,
                      f.photo_asset_id,
                      f.aligned_relpath,
                      f.detector_confidence,
                      f.face_area_ratio,
                      p.primary_path,
                      p.library_source_id
                    FROM face_observation AS f
                    INNER JOIN photo_asset AS p ON p.id = f.photo_asset_id
                    WHERE f.active=1
                      AND p.asset_status='active'
                    ORDER BY f.id ASC
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT
                      f.id,
                      f.photo_asset_id,
                      f.aligned_relpath,
                      f.detector_confidence,
                      f.face_area_ratio,
                      p.primary_path,
                      p.library_source_id
                    FROM face_observation AS f
                    INNER JOIN photo_asset AS p ON p.id = f.photo_asset_id
                    INNER JOIN scan_session_source AS s
                      ON s.library_source_id = p.library_source_id
                     AND s.scan_session_id = ?
                    WHERE f.active=1
                      AND p.asset_status='active'
                    ORDER BY f.id ASC
                    """,
                    (int(scan_session_id),),
                ).fetchall()
        finally:
            conn.close()

        faces: list[dict[str, object]] = []
        calculator = embedding_calculator or _build_default_embedding_calculator(param_snapshot=param_snapshot)
        for row in rows:
            observation_id = int(row[0])
            photo_asset_id = int(row[1])
            aligned_relpath = str(row[2])
            detector_confidence = float(row[3])
            face_area_ratio = float(row[4])
            primary_path = str(row[5])
            library_source_id = int(row[6])
            aligned_path = self._output_root / aligned_relpath
            embedding_main, embedding_flip, magface_quality = _run_embedding_calculator(
                calculator=calculator,
                aligned_path=aligned_path,
            )
            quality_score = float(
                magface_quality * max(0.05, detector_confidence) * np.sqrt(max(face_area_ratio, 1e-9))
            )
            faces.append(
                {
                    "face_observation_id": observation_id,
                    "photo_asset_id": photo_asset_id,
                    "photo_relpath": f"{library_source_id}/{primary_path}",
                    "quality_score": quality_score,
                    "embedding_main": embedding_main,
                    "embedding_flip": embedding_flip,
                    "magface_quality": float(magface_quality),
                    "detector_confidence": detector_confidence,
                    "face_area_ratio": face_area_ratio,
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
                if row.get("embedding_flip") is not None:
                    self._upsert_embedding_row(
                        conn,
                        face_observation_id=face_observation_id,
                        variant="flip",
                        vector=np.asarray(row["embedding_flip"], dtype=np.float32),
                    )
                else:
                    conn.execute(
                        """
                        DELETE FROM face_embedding
                        WHERE face_observation_id=?
                          AND feature_type='face'
                          AND model_key=?
                          AND variant='flip'
                        """,
                        (face_observation_id, EMBEDDING_MODEL_KEY),
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
            ) VALUES (?, 'face', ?, ?, 512, 'float32', ?, CURRENT_TIMESTAMP)
            ON CONFLICT(face_observation_id, feature_type, model_key, variant)
            DO UPDATE SET
              vector_blob=excluded.vector_blob,
              created_at=CURRENT_TIMESTAMP
            """,
            (
                int(face_observation_id),
                EMBEDDING_MODEL_KEY,
                str(variant),
                safe_vector.astype(np.float32).tobytes(),
            ),
        )

    def _persist_face_quality_scores(self, *, conn: sqlite3.Connection, face_rows: list[dict[str, object]]) -> None:
        for row in face_rows:
            observation_id = int(row.get("face_observation_id") or 0)
            if observation_id <= 0:
                continue
            magface_quality = float(row.get("magface_quality") or 0.0)
            quality_score = float(row.get("quality_score") or 0.0)
            conn.execute(
                """
                UPDATE face_observation
                SET magface_quality=?,
                    quality_score=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (magface_quality, quality_score, observation_id),
            )

    def _load_active_person_signatures(self, *, conn: sqlite3.Connection) -> dict[int, tuple[int, ...]]:
        person_rows = conn.execute("SELECT id FROM person WHERE status='active' ORDER BY id ASC").fetchall()
        signatures: dict[int, list[int]] = {int(row[0]): [] for row in person_rows}
        assignment_rows = conn.execute(
            """
            SELECT a.person_id, a.face_observation_id
            FROM person_face_assignment
            AS a
            INNER JOIN face_observation AS f ON f.id = a.face_observation_id
            INNER JOIN photo_asset AS p ON p.id = f.photo_asset_id
            WHERE a.active=1
              AND f.active=1
              AND p.asset_status='active'
            ORDER BY a.person_id ASC, a.face_observation_id ASC
            """
        ).fetchall()
        for row in assignment_rows:
            person_id = int(row[0])
            if person_id not in signatures:
                continue
            signatures[person_id].append(int(row[1]))
        return {
            person_id: tuple(sorted({int(face_observation_id) for face_observation_id in face_ids if int(face_observation_id) > 0}))
            for person_id, face_ids in signatures.items()
        }

    def _build_person_signature(
        self,
        row: dict[str, object],
        *,
        conn: sqlite3.Connection,
    ) -> tuple[int, ...]:
        raw_face_ids = row.get("face_observation_ids") or []
        if not isinstance(raw_face_ids, list):
            return ()
        active_face_ids = self._filter_active_face_ids(
            face_ids=[int(face_id) for face_id in raw_face_ids if int(face_id) > 0],
            conn=conn,
        )
        return tuple(sorted(active_face_ids))

    def _deactivate_active_assignments(self, *, conn: sqlite3.Connection) -> None:
        conn.execute(
            "UPDATE person_face_assignment SET active=0, updated_at=CURRENT_TIMESTAMP WHERE active=1"
        )

    def _retire_unreused_active_persons(
        self,
        *,
        existing_person_ids: set[int],
        reused_person_ids: set[int],
        conn: sqlite3.Connection,
    ) -> None:
        retired_person_ids = sorted(existing_person_ids - reused_person_ids)
        for person_id in retired_person_ids:
            conn.execute(
                """
                UPDATE person
                SET status='merged',
                    merged_into_person_id=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (person_id,),
            )

    def _upsert_persons(
        self,
        person_rows: list[dict[str, object]],
        *,
        run_kind: str,
        existing_active_signatures: dict[int, tuple[int, ...]] | None,
        conn: sqlite3.Connection,
    ) -> tuple[dict[str, int], set[int]]:
        person_map: dict[str, int] = {}
        reused_person_ids: set[int] = set()
        reusable_person_ids_by_signature: dict[tuple[int, ...], list[int]] = defaultdict(list)
        if str(run_kind) == "scan_full":
            for person_id, signature in (existing_active_signatures or {}).items():
                reusable_person_ids_by_signature[signature].append(int(person_id))

        for row in person_rows:
            person_temp_key = str(row.get("person_temp_key") or "")
            if not person_temp_key:
                continue
            person_id = 0
            if str(run_kind) == "scan_full":
                signature = self._build_person_signature(row, conn=conn)
                reusable_person_ids = reusable_person_ids_by_signature.get(signature) or []
                if reusable_person_ids:
                    person_id = int(reusable_person_ids.pop(0))
                    reused_person_ids.add(person_id)
                    conn.execute(
                        """
                        UPDATE person
                        SET status='active',
                            merged_into_person_id=NULL,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=?
                        """,
                        (person_id,),
                    )
            if person_id <= 0:
                cursor = conn.execute(
                    """
                    INSERT INTO person(
                      person_uuid, display_name, is_named, status, merged_into_person_id, created_at, updated_at
                    ) VALUES (?, NULL, 0, 'active', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (str(uuid.uuid4()),),
                )
                person_id = int(cursor.lastrowid)
            person_map[person_temp_key] = person_id
        return person_map, reused_person_ids

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

    def _clear_pending_reassign_for_faces(
        self,
        *,
        face_rows: list[dict[str, object]],
        conn: sqlite3.Connection,
    ) -> None:
        face_ids = sorted(
            {
                int(row.get("face_observation_id") or 0)
                for row in face_rows
                if int(row.get("face_observation_id") or 0) > 0
            }
        )
        if not face_ids:
            return
        placeholders = ", ".join("?" for _ in face_ids)
        conn.execute(
            f"""
            UPDATE face_observation
            SET pending_reassign=0,
                updated_at=CURRENT_TIMESTAMP
            WHERE id IN ({placeholders})
            """,
            tuple(face_ids),
        )

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

    def _promote_assignment_run_to_full(self, *, assignment_run_id: int) -> None:
        conn = connect_sqlite(self._library_db_path)
        try:
            conn.execute(
                """
                UPDATE assignment_run
                SET run_kind='scan_full',
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (int(assignment_run_id),),
            )
            conn.commit()
        finally:
            conn.close()

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
        run_kind: str,
        face_rows: list[dict[str, object]],
        person_rows: list[dict[str, object]],
        assignment_rows: list[dict[str, object]],
        cluster_rows: list[dict[str, object]],
        rebuild_scope: str,
    ) -> tuple[int, int]:
        conn = connect_sqlite(self._library_db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_not_aborting(scan_session_id=scan_session_id, conn=conn)
            self._persist_face_quality_scores(conn=conn, face_rows=face_rows)
            existing_active_signatures: dict[int, tuple[int, ...]] | None = None
            if str(run_kind) == "scan_full":
                existing_active_signatures = self._load_active_person_signatures(conn=conn)
                self._deactivate_active_assignments(conn=conn)
            person_map, reused_person_ids = self._upsert_persons(
                person_rows,
                run_kind=run_kind,
                existing_active_signatures=existing_active_signatures,
                conn=conn,
            )
            assignment_count = self._persist_assignments(
                scan_session_id=scan_session_id,
                assignment_rows=assignment_rows,
                assignment_run_id=assignment_run_id,
                person_map=person_map,
                conn=conn,
            )
            if str(run_kind) == "scan_full":
                self._clear_pending_reassign_for_faces(face_rows=face_rows, conn=conn)
                self._retire_unreused_active_persons(
                    existing_person_ids=set((existing_active_signatures or {}).keys()),
                    reused_person_ids=reused_person_ids,
                    conn=conn,
                )
            self._cluster_repo.replace_all_clusters(
                assignment_run_id=assignment_run_id,
                cluster_rows=cluster_rows,
                person_id_by_temp_key=person_map,
                face_quality_by_id=self._face_quality_by_id(face_rows),
                conn=conn,
                rebuild_scope=rebuild_scope,
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

    def _persist_incremental_assignment_outcome(
        self,
        *,
        scan_session_id: int,
        assignment_run_id: int,
        face_rows: list[dict[str, object]],
    ) -> tuple[int, int]:
        conn = connect_sqlite(self._library_db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_not_aborting(scan_session_id=scan_session_id, conn=conn)
            self._persist_face_quality_scores(conn=conn, face_rows=face_rows)
            candidate_face_ids = self._list_incremental_candidate_face_ids(scan_session_id=scan_session_id, conn=conn)
            incremental_service = IncrementalAssignmentService(
                library_db_path=self._library_db_path,
                embedding_db_path=self._embedding_db_path,
                cluster_repo=self._cluster_repo,
            )
            result = incremental_service.run(
                assignment_run_id=assignment_run_id,
                face_observation_ids=candidate_face_ids,
                conn=conn,
                abort_checker=lambda: self._ensure_not_aborting(scan_session_id=scan_session_id, conn=conn),
            )
            if self._should_fallback_to_full_rebuild(
                candidate_face_count=len(candidate_face_ids),
                attached_face_count=int(result.attached_count),
            ):
                raise IncrementalFallbackToFullRebuild(
                    f"incremental unresolved too high: candidate={len(candidate_face_ids)} attached={result.attached_count}"
                )
            self._mark_session_sources_stage_done(scan_session_id=scan_session_id, conn=conn)
            self._complete_assignment_run(assignment_run_id=assignment_run_id, status="completed", conn=conn)
            conn.commit()
            return int(result.person_count), int(result.attached_count)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _should_fallback_to_full_rebuild(
        self,
        *,
        candidate_face_count: int,
        attached_face_count: int,
    ) -> bool:
        if int(candidate_face_count) < 2:
            return False
        unresolved_count = max(0, int(candidate_face_count) - int(attached_face_count))
        if unresolved_count < 2:
            return False
        return float(unresolved_count) / float(candidate_face_count) >= 0.5

    def _should_run_incremental(
        self,
        *,
        scan_session_id: int,
        run_kind: str,
        param_snapshot: dict[str, object],
    ) -> bool:
        if str(run_kind) != "scan_incremental":
            return False
        conn = connect_sqlite(self._library_db_path)
        try:
            if not self._cluster_repo.has_active_clusters(conn=conn):
                return False
            row = conn.execute(
                """
                SELECT param_snapshot_json
                FROM assignment_run
                WHERE scan_session_id <> ?
                  AND status='completed'
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(scan_session_id),),
            ).fetchone()
            if row is None:
                return False
            latest_snapshot = json.loads(str(row[0]))
            return latest_snapshot == param_snapshot
        finally:
            conn.close()

    def _list_incremental_candidate_face_ids(
        self,
        *,
        scan_session_id: int,
        conn: sqlite3.Connection,
    ) -> list[int]:
        rows = conn.execute(
            """
            SELECT f.id
            FROM face_observation AS f
            INNER JOIN photo_asset AS p ON p.id = f.photo_asset_id
            INNER JOIN scan_session_source AS s
              ON s.library_source_id = p.library_source_id
             AND s.scan_session_id = ?
            LEFT JOIN person_face_assignment AS a
              ON a.face_observation_id = f.id
             AND a.active = 1
            WHERE f.active = 1
              AND p.asset_status = 'active'
              AND (f.pending_reassign = 1 OR a.id IS NULL)
            ORDER BY f.id ASC
            """,
            (int(scan_session_id),),
        ).fetchall()
        return [int(row[0]) for row in rows]

    def _face_quality_by_id(self, face_rows: list[dict[str, object]]) -> dict[int, float]:
        return {
            int(row.get("face_observation_id") or 0): float(row.get("quality_score") or 0.0)
            for row in face_rows
            if int(row.get("face_observation_id") or 0) > 0
        }

    def _filter_active_face_ids(
        self,
        *,
        face_ids: list[int],
        conn: sqlite3.Connection,
    ) -> list[int]:
        unique_ids = sorted({int(face_id) for face_id in face_ids if int(face_id) > 0})
        if not unique_ids:
            return []
        placeholders = ", ".join("?" for _ in unique_ids)
        rows = conn.execute(
            f"""
            SELECT f.id
            FROM face_observation AS f
            INNER JOIN photo_asset AS p ON p.id = f.photo_asset_id
            WHERE f.id IN ({placeholders})
              AND f.active=1
              AND p.asset_status='active'
            ORDER BY f.id ASC
            """,
            tuple(unique_ids),
        ).fetchall()
        return [int(row[0]) for row in rows]

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


def _resolve_magface_checkpoint(*, param_snapshot: dict[str, object]) -> Path:
    snapshot_path = str(param_snapshot.get("magface_checkpoint") or "").strip()
    if snapshot_path:
        return Path(snapshot_path).expanduser().resolve()

    env_path = str(os.environ.get(MAGFACE_CHECKPOINT_ENV, "")).strip()
    if env_path:
        return Path(env_path).expanduser().resolve()
    return MAGFACE_CHECKPOINT_DEFAULT.expanduser().resolve()


def _build_default_embedding_calculator(*, param_snapshot: dict[str, object]):
    checkpoint_path = _resolve_magface_checkpoint(param_snapshot=param_snapshot)
    enable_flip = bool(param_snapshot.get("embedding_enable_flip", True))
    flip_weight = float(param_snapshot.get("embedding_flip_weight", 1.0))
    enable_flip = enable_flip and flip_weight > 0
    embedder = MagFaceEmbedder(checkpoint_path=checkpoint_path)

    def _calculator(aligned_path: Path):
        if not aligned_path.exists():
            raise FileNotFoundError(f"aligned 文件不存在: {aligned_path}")
        aligned_bgr = cv2.imread(str(aligned_path), cv2.IMREAD_COLOR)
        if aligned_bgr is None:
            raise FileNotFoundError(f"aligned 文件不存在或无法读取: {aligned_path}")

        embedding_main, magface_quality = embedder.embed(aligned_bgr)
        embedding_flip: list[float] | None = None
        if enable_flip:
            aligned_bgr_flip = cv2.flip(aligned_bgr, 1)
            embedding_flip, _ = embedder.embed(aligned_bgr_flip)
        return embedding_main, embedding_flip, float(magface_quality)

    return _calculator


def _run_embedding_calculator(
    *,
    calculator,
    aligned_path: Path,
) -> tuple[list[float], list[float] | None, float]:
    result = calculator(aligned_path)
    if not isinstance(result, tuple):
        raise ValueError("embedding_calculator 返回值必须是 tuple")

    if len(result) == 3:
        raw_main, raw_flip, raw_magface_quality = result
        magface_quality = float(raw_magface_quality)
    elif len(result) == 2:
        raw_main, raw_flip = result
        magface_quality = float(np.linalg.norm(np.asarray(raw_main, dtype=np.float32)))
    else:
        raise ValueError("embedding_calculator 返回值必须是 (main, flip) 或 (main, flip, magface_quality)")

    main_vector = _normalize_vector(np.asarray(raw_main, dtype=np.float32)).astype(float).tolist()
    if raw_flip is None:
        flip_vector = None
    else:
        flip_vector = _normalize_vector(np.asarray(raw_flip, dtype=np.float32)).astype(float).tolist()
    return main_vector, flip_vector, magface_quality


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
