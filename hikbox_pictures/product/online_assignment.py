from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import json
from pathlib import Path
import sqlite3
import uuid

import hnswlib
import numpy as np

from hikbox_pictures.product.scan_shared import normalize_vector
from hikbox_pictures.product.scan_shared import utc_now_text
from hikbox_pictures.product.sources import WorkspaceContext


class OnlineAssignmentError(RuntimeError):
    """在线人物归属执行失败。"""


@dataclass(frozen=True)
class AssignmentParams:
    max_distance: float = 0.5
    min_faces: int = 3
    num_results: int = 3
    embedding_variant: str = "main"
    distance_metric: str = "cosine_distance"
    self_match_included: bool = True
    two_pass_deferred: bool = True

    def to_snapshot(self) -> dict[str, object]:
        return {
            "max_distance": self.max_distance,
            "min_faces": self.min_faces,
            "num_results": self.num_results,
            "embedding_variant": self.embedding_variant,
            "distance_metric": self.distance_metric,
            "self_match_included": self.self_match_included,
            "two_pass_deferred": self.two_pass_deferred,
        }


@dataclass
class SearchMatch:
    face_id: str
    distance: float


@dataclass
class AssignmentFace:
    face_id: str
    sort_key: tuple[str, str, int]
    embedding: np.ndarray
    person_id: str | None
    candidate: bool


@dataclass
class AssignmentDecision:
    face_id: str
    status: str
    person_id: str | None
    matched_face_ids: list[str]
    matched_distances: list[float]


@dataclass
class AssignmentRunResult:
    candidate_count: int
    assigned_count: int
    new_person_count: int
    deferred_count: int
    skipped_count: int
    failed_count: int
    decisions: list[AssignmentDecision] = field(default_factory=list)
    created_person_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExistingAssetFace:
    face_id: str
    bbox: tuple[float, float, float, float]
    image_width: int
    image_height: int
    person_id: str | None
    embedding: np.ndarray | None


@dataclass(frozen=True)
class RedetectFace:
    bbox: tuple[float, float, float, float]
    image_width: int
    image_height: int
    embedding: np.ndarray


@dataclass(frozen=True)
class PendingRedetectFace:
    detection_index: int
    reused_face_id: str | None
    bbox: tuple[float, float, float, float]
    image_width: int
    image_height: int
    embedding: np.ndarray


@dataclass(frozen=True)
class RedetectReconcileResult:
    reused_face_ids: list[str]
    invalidated_face_ids: list[str]
    pending_faces: list[PendingRedetectFace]
    reused_embeddings: dict[str, np.ndarray]
    reused_face_id_by_detection_index: dict[int, str] = field(default_factory=dict)


class FaceSearchIndex:
    """在线人物归属使用的内存 HNSW 余弦索引。"""

    def __init__(self, *, dim: int = 512, ef_construction: int = 300, m: int = 16) -> None:
        self._dim = int(dim)
        self._capacity = 16
        self._index = hnswlib.Index(space="cosine", dim=self._dim)
        self._index.init_index(max_elements=self._capacity, ef_construction=int(ef_construction), M=int(m))
        self._index.set_ef(max(50, int(m)))
        self._next_label = 1
        self._face_id_to_label: dict[str, int] = {}
        self._label_to_face_id: dict[int, str] = {}

    @property
    def count(self) -> int:
        return len(self._face_id_to_label)

    def upsert(self, face_id: str, embedding: np.ndarray) -> None:
        vector = normalize_vector(np.asarray(embedding, dtype=np.float32))
        if vector.shape != (self._dim,):
            raise OnlineAssignmentError(f"embedding 维度错误：{face_id} -> {vector.shape}")
        if face_id in self._face_id_to_label:
            self.delete(face_id)
        if self.count + 1 > self._capacity:
            self._capacity *= 2
            self._index.resize_index(self._capacity)
        label = self._next_label
        self._next_label += 1
        self._index.add_items(vector.reshape(1, -1), ids=np.asarray([label], dtype=np.int64))
        self._face_id_to_label[face_id] = label
        self._label_to_face_id[label] = face_id

    def delete(self, face_id: str) -> None:
        label = self._face_id_to_label.pop(face_id, None)
        if label is None:
            return
        self._label_to_face_id.pop(label, None)
        self._index.mark_deleted(label)

    def search(
        self,
        embedding: np.ndarray,
        *,
        num_results: int,
        max_distance: float,
        predicate: Callable[[str], bool] | None = None,
    ) -> list[SearchMatch]:
        if self.count == 0:
            return []
        query = normalize_vector(np.asarray(embedding, dtype=np.float32))
        if query.shape != (self._dim,):
            raise OnlineAssignmentError(f"embedding 维度错误：查询向量 -> {query.shape}")
        k = self.count if predicate is not None else min(max(int(num_results), 1), self.count)
        labels, distances = self._index.knn_query(query.reshape(1, -1), k=k)
        results: list[SearchMatch] = []
        for label, distance in zip(labels[0].tolist(), distances[0].tolist(), strict=False):
            face_id = self._label_to_face_id.get(int(label))
            if face_id is None:
                continue
            if predicate is not None and not predicate(face_id):
                continue
            safe_distance = float(distance)
            if safe_distance <= max_distance:
                results.append(SearchMatch(face_id=face_id, distance=safe_distance))
            if len(results) >= int(num_results):
                break
        return results


class OnlineAssignmentEngine:
    """执行 Immich v6 风格的两轮在线人物归属。"""

    def __init__(self, *, params: AssignmentParams) -> None:
        self._params = params

    def run(self, faces: list[AssignmentFace]) -> AssignmentRunResult:
        ordered_faces = sorted(faces, key=lambda item: item.sort_key)
        by_face_id = {face.face_id: face for face in ordered_faces}
        index = FaceSearchIndex(dim=512)
        for face in ordered_faces:
            index.upsert(face.face_id, face.embedding)

        candidate_faces = [face for face in ordered_faces if face.candidate]
        decisions_by_face_id: dict[str, AssignmentDecision] = {}
        created_person_ids: list[str] = []
        deferred_face_ids: list[str] = []
        deferred_count = 0
        assigned_count = 0
        new_person_count = 0

        for face in candidate_faces:
            decision, created_person_id = self._recognize_face(
                face=face,
                by_face_id=by_face_id,
                index=index,
                deferred=False,
            )
            if decision.status == "deferred":
                deferred_face_ids.append(face.face_id)
                deferred_count += 1
                continue
            decisions_by_face_id[face.face_id] = decision
            if decision.status == "assigned":
                assigned_count += 1
            if created_person_id is not None:
                created_person_ids.append(created_person_id)
                new_person_count += 1

        if self._params.two_pass_deferred:
            for face_id in deferred_face_ids:
                face = by_face_id[face_id]
                decision, created_person_id = self._recognize_face(
                    face=face,
                    by_face_id=by_face_id,
                    index=index,
                    deferred=True,
                )
                decisions_by_face_id[face.face_id] = decision
                if decision.status == "assigned":
                    assigned_count += 1
                if created_person_id is not None:
                    created_person_ids.append(created_person_id)
                    new_person_count += 1
        else:
            for face_id in deferred_face_ids:
                decisions_by_face_id[face_id] = AssignmentDecision(
                    face_id=face_id,
                    status="skipped",
                    person_id=None,
                    matched_face_ids=[],
                    matched_distances=[],
                )

        decisions = [decisions_by_face_id[face.face_id] for face in candidate_faces]
        skipped_count = sum(1 for item in decisions if item.status == "skipped")
        return AssignmentRunResult(
            candidate_count=len(candidate_faces),
            assigned_count=assigned_count,
            new_person_count=new_person_count,
            deferred_count=deferred_count,
            skipped_count=skipped_count,
            failed_count=0,
            decisions=decisions,
            created_person_ids=created_person_ids,
        )

    def _recognize_face(
        self,
        *,
        face: AssignmentFace,
        by_face_id: dict[str, AssignmentFace],
        index: FaceSearchIndex,
        deferred: bool,
    ) -> tuple[AssignmentDecision, str | None]:
        if face.person_id is not None:
            return (
                AssignmentDecision(
                    face_id=face.face_id,
                    status="skipped",
                    person_id=face.person_id,
                    matched_face_ids=[face.face_id],
                    matched_distances=[0.0],
                ),
                None,
            )
        matches = index.search(
            face.embedding,
            num_results=max(self._params.num_results, 1),
            max_distance=self._params.max_distance,
        )
        matched_face_ids = [item.face_id for item in matches]
        matched_distances = [item.distance for item in matches]
        if self._params.min_faces > 1 and len(matches) <= 1:
            return (
                AssignmentDecision(
                    face_id=face.face_id,
                    status="skipped",
                    person_id=None,
                    matched_face_ids=matched_face_ids,
                    matched_distances=matched_distances,
                ),
                None,
            )
        is_core = len(matches) >= self._params.min_faces
        if not is_core and not deferred:
            return (
                AssignmentDecision(
                    face_id=face.face_id,
                    status="deferred",
                    person_id=None,
                    matched_face_ids=matched_face_ids,
                    matched_distances=matched_distances,
                ),
                None,
            )
        person_id = next((by_face_id[item.face_id].person_id for item in matches if by_face_id[item.face_id].person_id), None)
        if person_id is None:
            assigned_matches = index.search(
                face.embedding,
                num_results=1,
                max_distance=self._params.max_distance,
                predicate=lambda candidate_face_id: by_face_id[candidate_face_id].person_id is not None,
            )
            if assigned_matches:
                person_id = by_face_id[assigned_matches[0].face_id].person_id
        created_person_id: str | None = None
        if is_core and person_id is None:
            created_person_id = str(uuid.uuid4())
            person_id = created_person_id
        if person_id is None:
            return (
                AssignmentDecision(
                    face_id=face.face_id,
                    status="skipped",
                    person_id=None,
                    matched_face_ids=matched_face_ids,
                    matched_distances=matched_distances,
                ),
                None,
            )
        face.person_id = person_id
        return (
            AssignmentDecision(
                face_id=face.face_id,
                status="assigned",
                person_id=person_id,
                matched_face_ids=matched_face_ids,
                matched_distances=matched_distances,
            ),
            created_person_id,
        )


def reconcile_asset_redetection(
    *,
    existing_faces: list[ExistingAssetFace],
    redetected_faces: list[RedetectFace],
) -> RedetectReconcileResult:
    remaining_existing = list(existing_faces)
    reused_face_ids: list[str] = []
    pending_faces: list[PendingRedetectFace] = []
    reused_embeddings: dict[str, np.ndarray] = {}
    reused_face_id_by_detection_index: dict[int, str] = {}
    for detection_index, redetected in enumerate(redetected_faces):
        reused_face: ExistingAssetFace | None = None
        for candidate in remaining_existing:
            if _normalized_iou(
                lhs_bbox=candidate.bbox,
                lhs_width=candidate.image_width,
                lhs_height=candidate.image_height,
                rhs_bbox=redetected.bbox,
                rhs_width=redetected.image_width,
                rhs_height=redetected.image_height,
            ) > 0.5:
                reused_face = candidate
                break
        if reused_face is not None:
            remaining_existing.remove(reused_face)
            reused_face_ids.append(reused_face.face_id)
            if reused_face.embedding is not None:
                reused_embeddings[reused_face.face_id] = reused_face.embedding
            reused_face_id_by_detection_index[detection_index] = reused_face.face_id
            continue
        pending_faces.append(
            PendingRedetectFace(
                detection_index=detection_index,
                reused_face_id=None,
                bbox=redetected.bbox,
                image_width=redetected.image_width,
                image_height=redetected.image_height,
                embedding=redetected.embedding,
            )
        )
    return RedetectReconcileResult(
        reused_face_ids=reused_face_ids,
        invalidated_face_ids=[face.face_id for face in remaining_existing],
        pending_faces=pending_faces,
        reused_embeddings=reused_embeddings,
        reused_face_id_by_detection_index=reused_face_id_by_detection_index,
    )


def run_online_assignment(
    *,
    workspace_context: WorkspaceContext,
    scan_session_id: int,
    append_log: Callable[[dict[str, object]], None],
    progress_callback: Callable[[str], None] | None = None,
) -> AssignmentRunResult:
    params = AssignmentParams()
    assignment_run_id = _create_assignment_run(
        workspace_context=workspace_context,
        scan_session_id=scan_session_id,
        params=params,
    )
    try:
        if progress_callback is not None:
            progress_callback("started")
        append_log(
            {
                "timestamp": utc_now_text(),
                "event": "assignment_started",
                "session_id": scan_session_id,
                "assignment_run_id": assignment_run_id,
                "algorithm_version": "immich_v6_online_v1",
                "param_snapshot": params.to_snapshot(),
            }
        )
        faces, orphan_keys = _load_assignment_faces(
            workspace_context=workspace_context,
            params=params,
        )
        if orphan_keys:
            append_log(
                {
                    "timestamp": utc_now_text(),
                    "event": "assignment_warning",
                    "session_id": scan_session_id,
                    "assignment_run_id": assignment_run_id,
                    "orphan_embedding_count": len(orphan_keys),
                    "orphan_embedding_keys": orphan_keys,
                }
            )
        result = OnlineAssignmentEngine(params=params).run(faces)
        _commit_assignment_result(
            workspace_context=workspace_context,
            assignment_run_id=assignment_run_id,
            result=result,
            orphan_keys=orphan_keys,
        )
        event_name = "assignment_completed"
        if result.assigned_count == 0 and result.new_person_count == 0 and result.failed_count == 0:
            event_name = "assignment_skipped"
        append_log(
            {
                "timestamp": utc_now_text(),
                "event": event_name,
                "session_id": scan_session_id,
                "assignment_run_id": assignment_run_id,
                "candidate_count": result.candidate_count,
                "assigned_count": result.assigned_count,
                "new_person_count": result.new_person_count,
                "deferred_count": result.deferred_count,
                "skipped_count": result.skipped_count,
                "failed_count": result.failed_count,
                "orphan_embedding_count": len(orphan_keys),
            }
        )
        if progress_callback is not None:
            progress_callback("completed" if event_name == "assignment_completed" else "skipped")
        return result
    except Exception as exc:  # noqa: BLE001
        failure_reason = _format_assignment_failure_reason(exc)
        mark_failure_error = _best_effort_mark_assignment_run_failed(
            workspace_context=workspace_context,
            assignment_run_id=assignment_run_id,
            reason=failure_reason,
        )
        raised_reason = failure_reason
        if mark_failure_error is not None:
            raised_reason = f"{failure_reason}；另外 {mark_failure_error}"
        _best_effort_append_assignment_log(
            append_log=append_log,
            payload={
                "timestamp": utc_now_text(),
                "event": "assignment_failed",
                "session_id": scan_session_id,
                "assignment_run_id": assignment_run_id,
                "reason": raised_reason,
            },
        )
        if progress_callback is not None:
            progress_callback("failed")
        if isinstance(exc, OnlineAssignmentError) and raised_reason == failure_reason:
            raise
        raise OnlineAssignmentError(raised_reason) from exc


def _create_assignment_run(
    *,
    workspace_context: WorkspaceContext,
    scan_session_id: int,
    params: AssignmentParams,
) -> int:
    connection = sqlite3.connect(workspace_context.library_db_path)
    try:
        now = utc_now_text()
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO assignment_runs (
                  scan_session_id,
                  algorithm_version,
                  status,
                  param_snapshot_json,
                  started_at,
                  updated_at
                )
                VALUES (?, 'immich_v6_online_v1', 'running', ?, ?, ?)
                """,
                (scan_session_id, json.dumps(params.to_snapshot(), ensure_ascii=False, sort_keys=True), now, now),
            )
            return int(cursor.lastrowid)
    except sqlite3.Error as exc:
        raise OnlineAssignmentError("assignment run 初始化失败。") from exc
    finally:
        connection.close()


def _load_assignment_faces(
    *,
    workspace_context: WorkspaceContext,
    params: AssignmentParams,
) -> tuple[list[AssignmentFace], list[str]]:
    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("ATTACH DATABASE ? AS embedding", (str(workspace_context.embedding_db_path),))
        orphan_rows = connection.execute(
            """
            SELECT
              embedding.face_embeddings.face_observation_id,
              embedding.face_embeddings.variant
            FROM embedding.face_embeddings
            LEFT JOIN main.face_observations
              ON main.face_observations.id = embedding.face_embeddings.face_observation_id
            WHERE embedding.face_embeddings.variant = ?
              AND main.face_observations.id IS NULL
            ORDER BY embedding.face_embeddings.face_observation_id ASC
            """,
            (params.embedding_variant,),
        ).fetchall()
        rows = connection.execute(
            """
            SELECT
              face_observations.id AS face_observation_id,
              library_sources.path AS source_path,
              assets.absolute_path AS absolute_path,
              face_observations.face_index AS face_index,
              embedding.face_embeddings.dimension AS embedding_dimension,
              embedding.face_embeddings.vector_blob AS vector_blob,
              person_face_assignments.person_id AS active_person_id
            FROM face_observations
            INNER JOIN assets
              ON assets.id = face_observations.asset_id
            INNER JOIN library_sources
              ON library_sources.id = assets.source_id
            LEFT JOIN embedding.face_embeddings
              ON embedding.face_embeddings.face_observation_id = face_observations.id
             AND embedding.face_embeddings.variant = ?
            LEFT JOIN person_face_assignments
              ON person_face_assignments.face_observation_id = face_observations.id
             AND person_face_assignments.active = 1
            WHERE assets.processing_status = 'succeeded'
            ORDER BY
              library_sources.path COLLATE NOCASE ASC,
              assets.absolute_path COLLATE NOCASE ASC,
              face_observations.face_index ASC
            """,
            (params.embedding_variant,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise OnlineAssignmentError("assignment 输入读取失败。") from exc
    finally:
        connection.close()

    faces: list[AssignmentFace] = []
    for row in rows:
        face_observation_id = int(row["face_observation_id"])
        if row["embedding_dimension"] is None:
            raise OnlineAssignmentError(f"候选 active face 缺少 main embedding：face_observation_id={face_observation_id}")
        if int(row["embedding_dimension"]) != 512:
            raise OnlineAssignmentError(
                f"候选 active face embedding 维度错误：face_observation_id={face_observation_id}, dimension={int(row['embedding_dimension'])}"
            )
        vector_blob = row["vector_blob"]
        if not isinstance(vector_blob, (bytes, bytearray, memoryview)):
            raise OnlineAssignmentError(f"候选 active face embedding 不可解码：face_observation_id={face_observation_id}")
        try:
            vector = np.frombuffer(bytes(vector_blob), dtype=np.float32)
        except ValueError as exc:
            raise OnlineAssignmentError(f"候选 active face embedding 不可解码：face_observation_id={face_observation_id}") from exc
        if vector.shape != (512,):
            raise OnlineAssignmentError(
                f"候选 active face embedding 不可解码：face_observation_id={face_observation_id}, decoded_shape={vector.shape}"
            )
        faces.append(
            AssignmentFace(
                face_id=str(face_observation_id),
                sort_key=(str(row["source_path"]), str(row["absolute_path"]), int(row["face_index"])),
                embedding=vector.copy(),
                person_id=str(row["active_person_id"]) if row["active_person_id"] is not None else None,
                candidate=row["active_person_id"] is None,
            )
        )

    orphan_keys = [
        f"face_observation_id={int(row['face_observation_id'])}:{str(row['variant'])}"
        for row in orphan_rows
    ]
    return faces, orphan_keys


def _commit_assignment_result(
    *,
    workspace_context: WorkspaceContext,
    assignment_run_id: int,
    result: AssignmentRunResult,
    orphan_keys: list[str],
) -> None:
    connection = sqlite3.connect(workspace_context.library_db_path)
    try:
        now = utc_now_text()
        with connection:
            existing_person_ids = {
                str(row[0])
                for row in connection.execute("SELECT id FROM person").fetchall()
            }
            for person_id in result.created_person_ids:
                if person_id in existing_person_ids:
                    continue
                connection.execute(
                    """
                    INSERT INTO person (
                      id,
                      display_name,
                      is_named,
                      status,
                      created_at,
                      updated_at
                    )
                    VALUES (?, NULL, 0, 'active', ?, ?)
                    """,
                    (person_id, now, now),
                )
            for decision in result.decisions:
                if decision.status != "assigned" or decision.person_id is None:
                    continue
                connection.execute(
                    """
                    INSERT INTO person_face_assignments (
                      person_id,
                      face_observation_id,
                      assignment_run_id,
                      assignment_source,
                      active,
                      evidence_json,
                      created_at,
                      updated_at
                    )
                    VALUES (?, ?, ?, 'online_v6', 1, ?, ?, ?)
                    """,
                    (
                        decision.person_id,
                        int(decision.face_id),
                        assignment_run_id,
                        json.dumps(
                            {
                                "matched_face_ids": decision.matched_face_ids,
                                "matched_distances": decision.matched_distances,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now,
                        now,
                    ),
                )
            connection.execute(
                """
                UPDATE assignment_runs
                SET status = 'completed',
                    candidate_count = ?,
                    assigned_count = ?,
                    new_person_count = ?,
                    deferred_count = ?,
                    skipped_count = ?,
                    failed_count = ?,
                    orphan_embedding_count = ?,
                    orphan_embedding_keys_json = ?,
                    completed_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    result.candidate_count,
                    result.assigned_count,
                    result.new_person_count,
                    result.deferred_count,
                    result.skipped_count,
                    result.failed_count,
                    len(orphan_keys),
                    json.dumps(orphan_keys, ensure_ascii=False),
                    now,
                    now,
                    assignment_run_id,
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise OnlineAssignmentError("active assignment 写入冲突，assignment 已回滚。") from exc
    except sqlite3.Error as exc:
        raise OnlineAssignmentError("assignment 结果提交失败。") from exc
    finally:
        connection.close()


def _mark_assignment_run_failed(
    *,
    workspace_context: WorkspaceContext,
    assignment_run_id: int,
    reason: str,
) -> None:
    connection = sqlite3.connect(workspace_context.library_db_path)
    try:
        now = utc_now_text()
        with connection:
            connection.execute(
                """
                UPDATE assignment_runs
                SET status = 'failed',
                    failure_reason = ?,
                    completed_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (reason, now, now, assignment_run_id),
            )
    except sqlite3.Error as exc:
        raise OnlineAssignmentError("assignment 失败状态落盘失败。") from exc
    finally:
        connection.close()


def _best_effort_mark_assignment_run_failed(
    *,
    workspace_context: WorkspaceContext,
    assignment_run_id: int,
    reason: str,
) -> str | None:
    try:
        _mark_assignment_run_failed(
            workspace_context=workspace_context,
            assignment_run_id=assignment_run_id,
            reason=reason,
        )
    except OnlineAssignmentError as exc:
        return str(exc) or "assignment 失败状态落盘失败。"
    return None


def _best_effort_append_assignment_log(
    *,
    append_log: Callable[[dict[str, object]], None],
    payload: dict[str, object],
) -> None:
    try:
        append_log(payload)
    except Exception:  # noqa: BLE001
        return


def _format_assignment_failure_reason(exc: Exception) -> str:
    message = str(exc).strip()
    if message:
        return message
    return "assignment 执行失败。"


def _normalized_iou(
    *,
    lhs_bbox: tuple[float, float, float, float],
    lhs_width: int,
    lhs_height: int,
    rhs_bbox: tuple[float, float, float, float],
    rhs_width: int,
    rhs_height: int,
) -> float:
    lhs = (
        lhs_bbox[0] / max(lhs_width, 1),
        lhs_bbox[1] / max(lhs_height, 1),
        lhs_bbox[2] / max(lhs_width, 1),
        lhs_bbox[3] / max(lhs_height, 1),
    )
    rhs = (
        rhs_bbox[0] / max(rhs_width, 1),
        rhs_bbox[1] / max(rhs_height, 1),
        rhs_bbox[2] / max(rhs_width, 1),
        rhs_bbox[3] / max(rhs_height, 1),
    )
    left = max(lhs[0], rhs[0])
    top = max(lhs[1], rhs[1])
    right = min(lhs[2], rhs[2])
    bottom = min(lhs[3], rhs[3])
    if right <= left or bottom <= top:
        return 0.0
    intersection = (right - left) * (bottom - top)
    lhs_area = max(lhs[2] - lhs[0], 0.0) * max(lhs[3] - lhs[1], 0.0)
    rhs_area = max(rhs[2] - rhs[0], 0.0) * max(rhs[3] - rhs[1], 0.0)
    union = lhs_area + rhs_area - intersection
    if union <= 1e-9:
        return 0.0
    return float(intersection / union)
