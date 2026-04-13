from __future__ import annotations

from pathlib import Path

import numpy as np

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.deepface_engine import embedding_to_blob
from hikbox_pictures.metadata import resolve_capture_fields
from hikbox_pictures.repositories import AssetRepo, ScanRepo
from hikbox_pictures.services.asset_pipeline import (
    done_status_for_stage,
    ensure_stage,
    previous_status_for_stage,
    statuses_at_or_above,
)


class AssetStageRunner:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.asset_repo = AssetRepo(conn)
        self.scan_repo = ScanRepo(conn)

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

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            required_status = previous_status_for_stage(stage_name)
            assets = self.asset_repo.list_assets_for_source_with_status(library_source_id, required_status)

            for asset in assets:
                asset_id = int(asset["id"])
                if stage_name == "metadata":
                    self._run_metadata_stage(asset_id, Path(str(asset["primary_path"])), scan_session_id)
                elif stage_name == "faces":
                    self._run_faces_stage(asset_id, scan_session_id)
                elif stage_name == "embeddings":
                    self._run_embeddings_stage(asset_id, scan_session_id)
                else:
                    self._run_assignment_stage(asset_id, scan_session_id)

            progress = self.refresh_source_progress(session_source_id, library_source_id)
            self.conn.commit()
            return progress
        except Exception:
            self.conn.rollback()
            raise

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

        self.scan_repo.update_source_progress_counts(
            session_source_id,
            discovered_count=discovered_count,
            metadata_done_count=metadata_done_count,
            faces_done_count=faces_done_count,
            embeddings_done_count=embeddings_done_count,
            assignment_done_count=assignment_done_count,
        )
        return {
            "discovered_count": discovered_count,
            "metadata_done_count": metadata_done_count,
            "faces_done_count": faces_done_count,
            "embeddings_done_count": embeddings_done_count,
            "assignment_done_count": assignment_done_count,
        }

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
        self.asset_repo.ensure_face_observation(asset_id)
        self.asset_repo.mark_stage_done_if_current(
            asset_id,
            from_status=previous_status_for_stage("faces"),
            to_status=done_status_for_stage("faces"),
            last_processed_session_id=scan_session_id,
        )

    def _run_embeddings_stage(self, asset_id: int, scan_session_id: int) -> None:
        observation_ids = self.asset_repo.list_active_face_observation_ids(asset_id)
        if not observation_ids:
            observation_ids = [self.asset_repo.ensure_face_observation(asset_id)]

        for observation_id in observation_ids:
            embedding = np.asarray(
                [float(asset_id), float(observation_id), 0.0, 1.0],
                dtype=np.float32,
            )
            self.asset_repo.ensure_face_embedding(
                observation_id,
                vector_blob=embedding_to_blob(embedding),
                dimension=int(embedding.size),
            )

        self.asset_repo.mark_stage_done_if_current(
            asset_id,
            from_status=previous_status_for_stage("embeddings"),
            to_status=done_status_for_stage("embeddings"),
            last_processed_session_id=scan_session_id,
        )

    def _run_assignment_stage(self, asset_id: int, scan_session_id: int) -> None:
        observation_ids = self.asset_repo.list_active_face_observation_ids(asset_id)
        person_id = self._pick_default_person_id()
        if person_id is not None:
            for observation_id in observation_ids:
                self._ensure_active_assignment(person_id, observation_id)

        self.asset_repo.mark_stage_done_if_current(
            asset_id,
            from_status=previous_status_for_stage("assignment"),
            to_status=done_status_for_stage("assignment"),
            last_processed_session_id=scan_session_id,
        )

    def _pick_default_person_id(self) -> int | None:
        row = self.conn.execute(
            """
            SELECT id
            FROM person
            WHERE status = 'active' AND ignored = 0
            ORDER BY confirmed DESC, id ASC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return int(row["id"])

    def _ensure_active_assignment(self, person_id: int, observation_id: int) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO person_face_assignment(
                person_id,
                face_observation_id,
                assignment_source,
                confidence,
                locked,
                active
            )
            VALUES (?, ?, 'auto', 1.0, 0, 1)
            """,
            (int(person_id), int(observation_id)),
        )
