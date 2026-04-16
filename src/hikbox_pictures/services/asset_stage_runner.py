from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.deepface_engine import (
    DEFAULT_FACE_DETECTOR_BACKEND,
    DEFAULT_FACE_MODEL_NAME,
    DeepFaceEngine,
    DetectedFace,
    embedding_to_blob,
    resolve_ann_distance_threshold,
)
from hikbox_pictures.ann import AnnIndexStore
from hikbox_pictures.image_io import load_oriented_image
from hikbox_pictures.metadata import resolve_capture_fields
from hikbox_pictures.repositories import AssetRepo, IdentityRepo, PersonRepo, ReviewRepo, ScanRepo
from hikbox_pictures.services.ann_assignment_service import AnnAssignmentService
from hikbox_pictures.services.asset_pipeline import (
    DEFAULT_AUTO_ASSIGN_THRESHOLD,
    DEFAULT_REVIEW_THRESHOLD,
    done_status_for_stage,
    ensure_stage,
    previous_status_for_stage,
    statuses_at_or_above,
)
from hikbox_pictures.services.observation_quality_backfill_service import ObservationQualityBackfillService
from hikbox_pictures.workspace import load_workspace_paths_from_db_path

_PROGRESS_FLUSH_INTERVAL_SECONDS = 5.0
_STAGE_PROGRESS_COUNT_KEY = {
    "metadata": "metadata_done_count",
    "faces": "faces_done_count",
    "embeddings": "embeddings_done_count",
    "assignment": "assignment_done_count",
}


@dataclass
class _ProgressTracker:
    progress: dict[str, int]
    last_flush_at: float
    dirty: bool = False

    def advance_stage(self, stage_name: str) -> None:
        key = _STAGE_PROGRESS_COUNT_KEY[stage_name]
        self.progress[key] = int(self.progress.get(key, 0)) + 1
        self.dirty = True

    def should_flush(self, now: float) -> bool:
        return self.dirty and (now - self.last_flush_at) >= _PROGRESS_FLUSH_INTERVAL_SECONDS

    def mark_flushed(self, now: float) -> None:
        self.last_flush_at = now
        self.dirty = False

    def replace(self, progress: dict[str, int], *, now: float) -> None:
        self.progress = dict(progress)
        self.last_flush_at = now
        self.dirty = False


class AssetStageRunner:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.asset_repo = AssetRepo(conn)
        self.identity_repo = IdentityRepo(conn)
        self.person_repo = PersonRepo(conn)
        self.review_repo = ReviewRepo(conn)
        self.scan_repo = ScanRepo(conn)
        self.db_path = self._resolve_db_path()
        self.paths = load_workspace_paths_from_db_path(self.db_path)
        self.workspace = self.paths.root
        self.face_crop_dir = self.paths.artifacts_dir / "face-crops" / "scan"
        self._face_engine: DeepFaceEngine | None = None
        self._ann_assignment_service: AnnAssignmentService | None = None

    def run_stage(self, session_source_id: int, stage: str) -> dict[str, int]:
        stage_name = ensure_stage(stage)
        source_state = self.scan_repo.get_session_source(session_source_id)
        if source_state is None:
            raise ValueError(f"scan_session_source 不存在: {session_source_id}")

        library_source_id = int(source_state["library_source_id"])
        scan_session_id = int(source_state["scan_session_id"])

        # 统一由 run_stage 自管事务，确保阶段写入以 IMMEDIATE 锁串行执行。
        # 若调用方已开启事务，无法保证锁语义一致，直接报错避免隐性不一致。
        if self.conn.in_transaction:
            raise RuntimeError("run_stage 不支持在外部事务中调用，请在无事务上下文调用")

        required_status = previous_status_for_stage(stage_name)
        assets = self.asset_repo.list_assets_for_source_with_status(library_source_id, required_status)
        tracker = _ProgressTracker(
            progress=self._reconcile_source_progress(session_source_id, library_source_id),
            last_flush_at=time.monotonic(),
        )

        for asset in assets:
            asset_id = int(asset["id"])
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                live_asset = self.asset_repo.get_asset(asset_id)
                if live_asset is None:
                    raise LookupError(f"photo_asset 不存在: {asset_id}")
                if str(live_asset["processing_status"]) != required_status:
                    progress = self.refresh_source_progress(session_source_id, library_source_id)
                    tracker.replace(progress, now=time.monotonic())
                    self.conn.commit()
                    continue

                if stage_name == "metadata":
                    self._run_metadata_stage(asset_id, Path(str(asset["primary_path"])), scan_session_id)
                elif stage_name == "faces":
                    self._run_faces_stage(asset_id, scan_session_id)
                elif stage_name == "embeddings":
                    self._run_embeddings_stage(asset_id, scan_session_id)
                else:
                    self._run_assignment_stage(asset_id, scan_session_id)

                tracker.advance_stage(stage_name)
                now = time.monotonic()
                if tracker.should_flush(now):
                    self._write_source_progress_snapshot(session_source_id, tracker.progress)
                    tracker.mark_flushed(now)
                self.conn.commit()
            except Exception as exc:
                self.conn.rollback()
                try:
                    tracker.replace(
                        self._reconcile_source_progress(session_source_id, library_source_id),
                        now=time.monotonic(),
                    )
                except Exception as reconcile_exc:
                    exc.add_note(f"进度校准失败: {reconcile_exc}")
                raise

        final_progress = self._reconcile_source_progress(session_source_id, library_source_id)
        tracker.replace(final_progress, now=time.monotonic())
        return final_progress

    def refresh_source_progress(self, session_source_id: int, library_source_id: int) -> dict[str, int]:
        discovered_count = self.asset_repo.count_assets_for_source(library_source_id)
        metadata_done_count = self.asset_repo.count_assets_for_source_with_statuses(
            library_source_id,
            tuple(statuses_at_or_above("metadata_done")),
        )
        faces_done_count = self.asset_repo.count_assets_for_source_with_statuses(
            library_source_id,
            tuple(statuses_at_or_above("faces_done")),
        )
        embeddings_done_count = self.asset_repo.count_assets_for_source_with_statuses(
            library_source_id,
            tuple(statuses_at_or_above("embeddings_done")),
        )
        assignment_done_count = self.asset_repo.count_assets_for_source_with_statuses(
            library_source_id,
            tuple(statuses_at_or_above("assignment_done")),
        )

        return self._write_source_progress_snapshot(
            session_source_id,
            {
                "discovered_count": discovered_count,
                "metadata_done_count": metadata_done_count,
                "faces_done_count": faces_done_count,
                "embeddings_done_count": embeddings_done_count,
                "assignment_done_count": assignment_done_count,
            },
        )

    def _write_source_progress_snapshot(
        self,
        session_source_id: int,
        progress: dict[str, int],
    ) -> dict[str, int]:
        self.scan_repo.update_source_progress_counts(
            session_source_id,
            discovered_count=int(progress["discovered_count"]),
            metadata_done_count=int(progress["metadata_done_count"]),
            faces_done_count=int(progress["faces_done_count"]),
            embeddings_done_count=int(progress["embeddings_done_count"]),
            assignment_done_count=int(progress["assignment_done_count"]),
        )
        return dict(progress)

    def _reconcile_source_progress(self, session_source_id: int, library_source_id: int) -> dict[str, int]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            progress = self.refresh_source_progress(session_source_id, library_source_id)
            self.conn.commit()
            return progress
        except Exception:
            self.conn.rollback()
            raise

    def _run_metadata_stage(self, asset_id: int, primary_path: Path, scan_session_id: int) -> None:
        capture_datetime, capture_month = resolve_capture_fields(primary_path)
        self.asset_repo.mark_metadata_done_if_current(
            asset_id,
            expected_status=previous_status_for_stage("metadata"),
            capture_datetime=capture_datetime,
            capture_month=capture_month,
            last_processed_session_id=scan_session_id,
        )

    def _run_faces_stage(self, asset_id: int, scan_session_id: int) -> None:
        asset = self.asset_repo.get_asset(asset_id)
        if asset is None:
            raise LookupError(f"photo_asset 不存在: {asset_id}")

        primary_path = Path(str(asset["primary_path"]))
        faces = self.face_engine.detect_faces(primary_path)
        observations = [
            self._build_face_observation_payload(primary_path, face)
            for face in faces
        ]
        self.asset_repo.replace_face_observations(
            asset_id,
            observations=observations,
            detector_key=self.face_engine.detector_backend,
            detector_version=self.face_engine.model_name,
        )
        self.asset_repo.mark_stage_done_if_current(
            asset_id,
            from_status=previous_status_for_stage("faces"),
            to_status=done_status_for_stage("faces"),
            last_processed_session_id=scan_session_id,
        )

    def _run_embeddings_stage(self, asset_id: int, scan_session_id: int) -> None:
        observations = self.asset_repo.list_active_face_observations(asset_id)
        observation_ids: list[int] = []
        for observation in observations:
            observation_id = int(observation["id"])
            crop_path = self._ensure_face_crop(observation_id)
            embedding = self.face_engine.represent_face(crop_path)
            self.asset_repo.ensure_face_embedding(
                observation_id,
                vector_blob=embedding_to_blob(embedding),
                model_key=self.face_engine.model_key,
                dimension=int(embedding.size),
            )
            observation_ids.append(observation_id)

        if observation_ids:
            active_profile = self.identity_repo.get_active_profile()
            profile_id = int(active_profile["id"]) if active_profile is not None else None
            ObservationQualityBackfillService(self.conn).backfill_observations(
                observation_ids=observation_ids,
                profile_id=profile_id,
                update_profile_quantiles=False,
            )

        self.asset_repo.mark_stage_done_if_current(
            asset_id,
            from_status=previous_status_for_stage("embeddings"),
            to_status=done_status_for_stage("embeddings"),
            last_processed_session_id=scan_session_id,
        )

    def _run_assignment_stage(self, asset_id: int, scan_session_id: int) -> None:
        observations = self.asset_repo.list_active_face_observations(asset_id)
        prototype_rows = self._load_active_prototype_rows()
        prototype_vectors = self._group_prototype_vectors(prototype_rows)
        for observation in observations:
            observation_id = int(observation["id"])
            active_assignment = self.asset_repo.get_active_assignment_for_observation(observation_id)
            if active_assignment is not None and int(active_assignment["locked"]) == 1:
                continue

            embedding = self._load_observation_embedding(observation_id)
            if embedding is None:
                self._queue_new_person_review(observation_id, [])
                continue

            candidates = self._recall_candidates(
                embedding,
                prototype_rows=prototype_rows,
                prototype_vectors=prototype_vectors,
            )
            excluded_person_ids = set(self.asset_repo.list_excluded_person_ids_for_observation(observation_id))
            if excluded_person_ids:
                candidates = [
                    candidate
                    for candidate in candidates
                    if int(candidate["person_id"]) not in excluded_person_ids
                ]
            if not candidates:
                self._queue_new_person_review(observation_id, [])
                continue

            best_candidate = candidates[0]
            decision = self.ann_assignment_service.classify_distance(float(best_candidate["distance"]))
            if decision == "auto_assign":
                self._upsert_auto_assignment(
                    observation_id,
                    person_id=int(best_candidate["person_id"]),
                    distance=float(best_candidate["distance"]),
                )
                continue
            if decision == "review":
                self._queue_low_confidence_review(observation_id, candidates)
                continue
            self._queue_new_person_review(observation_id, candidates)

        self.asset_repo.mark_stage_done_if_current(
            asset_id,
            from_status=previous_status_for_stage("assignment"),
            to_status=done_status_for_stage("assignment"),
            last_processed_session_id=scan_session_id,
        )

    @property
    def face_engine(self) -> DeepFaceEngine:
        if self._face_engine is None:
            self._face_engine = DeepFaceEngine.create(
                model_name=DEFAULT_FACE_MODEL_NAME,
                detector_backend=DEFAULT_FACE_DETECTOR_BACKEND,
            )
        return self._face_engine

    @property
    def ann_assignment_service(self) -> AnnAssignmentService:
        if self._ann_assignment_service is None:
            ann_store = AnnIndexStore(self.paths.artifacts_dir / "ann" / "prototype_index.npz")
            review_threshold = max(
                DEFAULT_REVIEW_THRESHOLD,
                resolve_ann_distance_threshold(
                    float(self.face_engine.distance_threshold),
                    distance_metric=self.face_engine.distance_metric,
                    threshold_source=self.face_engine.threshold_source,
                ),
            )
            auto_assign_threshold = min(
                review_threshold,
                max(DEFAULT_AUTO_ASSIGN_THRESHOLD, review_threshold * 0.75),
            )
            self._ann_assignment_service = AnnAssignmentService(
                ann_store,
                auto_assign_threshold=auto_assign_threshold,
                review_threshold=review_threshold,
            )
        return self._ann_assignment_service

    def _resolve_db_path(self) -> Path:
        rows = self.conn.execute("PRAGMA database_list").fetchall()
        for row in rows:
            name = row["name"] if isinstance(row, sqlite3.Row) else row[1]
            if str(name) != "main":
                continue
            raw_path = row["file"] if isinstance(row, sqlite3.Row) else row[2]
            if raw_path:
                return Path(str(raw_path)).resolve()
        raise RuntimeError("无法解析当前连接对应的数据库路径")

    def _build_face_observation_payload(self, primary_path: Path, face: DetectedFace) -> dict[str, float | None]:
        width, height = self._resolve_image_size(primary_path, face)
        bbox_top, bbox_right, bbox_bottom, bbox_left = self._normalize_bbox(
            face.bbox,
            width=width,
            height=height,
        )
        return {
            "bbox_top": bbox_top,
            "bbox_right": bbox_right,
            "bbox_bottom": bbox_bottom,
            "bbox_left": bbox_left,
            "face_area_ratio": max(0.0, (bbox_bottom - bbox_top) * (bbox_right - bbox_left)),
            "crop_path": None,
        }

    def _resolve_image_size(self, primary_path: Path, face: DetectedFace) -> tuple[int, int]:
        if face.image_size is not None:
            width, height = face.image_size
            if int(width) > 0 and int(height) > 0:
                return int(width), int(height)
        image = load_oriented_image(primary_path)
        width, height = image.size
        if width <= 0 or height <= 0:
            raise ValueError(f"图片尺寸非法: {primary_path}")
        return int(width), int(height)

    def _normalize_bbox(self, bbox: tuple[int, int, int, int], *, width: int, height: int) -> tuple[float, float, float, float]:
        top, right, bottom, left = (int(value) for value in bbox)
        clamped_left = max(0, min(width - 1, left))
        clamped_top = max(0, min(height - 1, top))
        clamped_right = max(clamped_left + 1, min(width, right))
        clamped_bottom = max(clamped_top + 1, min(height, bottom))
        return (
            float(clamped_top) / float(height),
            float(clamped_right) / float(width),
            float(clamped_bottom) / float(height),
            float(clamped_left) / float(width),
        )

    def _ensure_face_crop(self, observation_id: int) -> Path:
        row = self.asset_repo.get_observation_with_source(observation_id)
        if row is None:
            raise LookupError(f"observation 不存在: {observation_id}")

        crop_path_raw = row.get("crop_path")
        if crop_path_raw:
            existing = Path(str(crop_path_raw))
            if existing.exists() and existing.is_file():
                return existing

        source_path = Path(str(row["primary_path"]))
        if not source_path.exists() or not source_path.is_file():
            raise FileNotFoundError(f"媒体文件不存在: {source_path}")

        self.face_crop_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.face_crop_dir / f"obs-{observation_id}.jpg"

        image = load_oriented_image(source_path)
        width, height = image.size
        left = max(0, min(width - 1, int(float(row["bbox_left"]) * width)))
        top = max(0, min(height - 1, int(float(row["bbox_top"]) * height)))
        right = max(left + 1, min(width, int(float(row["bbox_right"]) * width)))
        bottom = max(top + 1, min(height, int(float(row["bbox_bottom"]) * height)))
        image.crop((left, top, right, bottom)).convert("RGB").save(out_path, format="JPEG")

        self.asset_repo.update_observation_crop_path(observation_id, str(out_path))
        return out_path

    def _load_observation_embedding(self, observation_id: int) -> np.ndarray | None:
        row = self.asset_repo.get_face_embedding(
            observation_id,
            model_key=self.face_engine.model_key,
        )
        if row is None:
            row = self.asset_repo.get_face_embedding(observation_id)
        if row is None:
            return None

        vector_blob = row.get("vector_blob")
        if not isinstance(vector_blob, (bytes, bytearray, memoryview)):
            raise ValueError(f"observation {observation_id} 的 embedding 非法")
        vector = np.frombuffer(vector_blob, dtype=np.float32).copy()
        if vector.ndim != 1 or vector.size == 0:
            raise ValueError(f"observation {observation_id} 的 embedding 为空或维度非法")
        return vector

    def _load_active_prototype_rows(self) -> list[dict[str, object]]:
        return self.person_repo.list_active_prototypes(
            prototype_type="centroid",
            model_key=self.face_engine.model_key,
        )

    def _group_prototype_vectors(
        self,
        prototype_rows: list[dict[str, object]],
    ) -> dict[int, list[np.ndarray]]:
        grouped: dict[int, list[np.ndarray]] = {}
        for row in prototype_rows:
            vector_blob = row.get("vector_blob")
            if not isinstance(vector_blob, (bytes, bytearray, memoryview)):
                continue
            vector = np.frombuffer(vector_blob, dtype=np.float32).copy()
            if vector.ndim != 1 or vector.size == 0:
                continue
            person_id = int(row["person_id"])
            grouped.setdefault(person_id, []).append(vector)
        return grouped

    def _recall_candidates(
        self,
        embedding: np.ndarray,
        *,
        prototype_rows: list[dict[str, object]],
        prototype_vectors: dict[int, list[np.ndarray]],
        top_k: int = 5,
    ) -> list[dict[str, float | int]]:
        if not prototype_vectors:
            return []

        recalled_candidates: list[dict[str, float | int]] = []
        if self.ann_assignment_service.ann_index_store.size > 0:
            try:
                recalled_candidates = self.ann_assignment_service.recall_person_candidates(embedding, top_k=top_k)
            except ValueError as exc:
                if "维度不匹配" not in str(exc):
                    raise
                self.ann_assignment_service.ann_index_store.rebuild_from_prototypes(prototype_rows)
                try:
                    recalled_candidates = self.ann_assignment_service.recall_person_candidates(embedding, top_k=top_k)
                except ValueError as retry_exc:
                    if "维度不匹配" not in str(retry_exc):
                        raise
                    recalled_candidates = []

        candidates = self._resolve_candidate_distances(
            embedding,
            recalled_candidates=recalled_candidates,
            prototype_vectors=prototype_vectors,
        )
        if candidates:
            return candidates
        return self._manual_candidate_distances(
            embedding,
            prototype_vectors=prototype_vectors,
            top_k=top_k,
        )

    def _resolve_candidate_distances(
        self,
        embedding: np.ndarray,
        *,
        recalled_candidates: list[dict[str, float | int]],
        prototype_vectors: dict[int, list[np.ndarray]],
    ) -> list[dict[str, float | int]]:
        resolved: list[dict[str, float | int]] = []
        for candidate in recalled_candidates:
            person_id = int(candidate["person_id"])
            references = prototype_vectors.get(person_id)
            if not references:
                continue
            compatible_references = [reference for reference in references if int(reference.size) == int(embedding.size)]
            if not compatible_references:
                continue
            distance = self.face_engine.min_distance(embedding, compatible_references)
            resolved.append({"person_id": person_id, "distance": float(distance)})
        resolved.sort(key=lambda item: (float(item["distance"]), int(item["person_id"])))
        return resolved

    def _manual_candidate_distances(
        self,
        embedding: np.ndarray,
        *,
        prototype_vectors: dict[int, list[np.ndarray]],
        top_k: int,
    ) -> list[dict[str, float | int]]:
        resolved: list[dict[str, float | int]] = []
        for person_id, references in prototype_vectors.items():
            compatible_references = [reference for reference in references if int(reference.size) == int(embedding.size)]
            if not compatible_references:
                continue
            distance = self.face_engine.min_distance(embedding, compatible_references)
            resolved.append({"person_id": int(person_id), "distance": float(distance)})
        resolved.sort(key=lambda item: (float(item["distance"]), int(item["person_id"])))
        return resolved[: max(0, int(top_k))]

    def _upsert_auto_assignment(self, observation_id: int, *, person_id: int, distance: float) -> None:
        active_assignment = self.asset_repo.get_active_assignment_for_observation(observation_id)
        confidence = max(0.0, 1.0 - float(distance))
        if active_assignment is None:
            self.asset_repo.create_assignment(
                person_id=int(person_id),
                face_observation_id=int(observation_id),
                assignment_source="auto",
                confidence=confidence,
                locked=False,
            )
            return
        self.asset_repo.update_assignment(
            int(active_assignment["id"]),
            person_id=int(person_id),
            assignment_source="auto",
            confidence=confidence,
        )

    def _queue_low_confidence_review(
        self,
        observation_id: int,
        candidates: list[dict[str, float | int]],
    ) -> None:
        payload = json.dumps(
            {
                "face_observation_id": int(observation_id),
                "candidates": candidates,
                "model_key": self.face_engine.model_key,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        self.review_repo.create_review_item(
            "low_confidence_assignment",
            payload_json=payload,
            priority=20,
            face_observation_id=int(observation_id),
        )

    def _queue_new_person_review(
        self,
        observation_id: int,
        candidates: list[dict[str, float | int]],
    ) -> None:
        payload = json.dumps(
            {
                "face_observation_id": int(observation_id),
                "candidates": candidates,
                "model_key": self.face_engine.model_key,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        self.review_repo.create_review_item(
            "new_person",
            payload_json=payload,
            priority=15,
            face_observation_id=int(observation_id),
        )
