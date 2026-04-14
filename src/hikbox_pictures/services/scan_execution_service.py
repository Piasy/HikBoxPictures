from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.repositories import AssetRepo, ScanRepo
from hikbox_pictures.scanner import iter_candidate_photos
from hikbox_pictures.services.asset_pipeline import statuses_at_or_above
from hikbox_pictures.services.asset_stage_runner import AssetStageRunner

_SOURCE_TERMINAL_STATUSES = {"completed", "failed", "abandoned"}
_PIPELINE_STAGES: tuple[str, ...] = ("metadata", "faces", "embeddings", "assignment")


class ScanExecutionService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        checkpoint_writer: Callable[[int, str, str | None, int], int] | None = None,
        progress_reporter: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.conn = conn
        self.scan_repo = ScanRepo(conn)
        self.asset_repo = AssetRepo(conn)
        self._checkpoint_writer = checkpoint_writer
        self._progress_reporter = progress_reporter

    def run_session(self, session_id: int) -> dict[str, int]:
        total_new_assets = 0
        completed_sources = 0
        failed_sources = 0
        session_sources = self.scan_repo.list_session_sources(session_id)

        for source in session_sources:
            session_source_id = int(source["id"])
            live_source = self.scan_repo.get_session_source(session_source_id)
            if self._is_terminal_source(live_source):
                continue

            updated = self.scan_repo.mark_session_source_running(session_source_id)
            self.conn.commit()
            if updated == 0:
                continue
            self._report_progress(session_source_id)

            try:
                live_source = self.scan_repo.get_session_source(session_source_id)
                if self._is_terminal_source(live_source):
                    continue
                if live_source is None:
                    continue

                source_id = int(live_source["library_source_id"])
                source_root = Path(str(source["source_root_path"])).expanduser().resolve()
                discovered_new = self._discover_source_assets(
                    session_source_id=session_source_id,
                    library_source_id=source_id,
                    source_root=source_root,
                )
                total_new_assets += discovered_new

                for stage in _PIPELINE_STAGES:
                    live_source = self.scan_repo.get_session_source(session_source_id)
                    if self._is_terminal_source(live_source):
                        break
                    progress = AssetStageRunner(self.conn).run_stage(session_source_id, stage)
                    self._write_checkpoint(
                        session_source_id,
                        phase=stage,
                        cursor_json=None,
                        pending_asset_count=self._stage_pending_count(progress, stage),
                    )

                live_source = self.scan_repo.get_session_source(session_source_id)
                if self._is_terminal_source(live_source):
                    continue

                self.scan_repo.mark_session_source_completed(session_source_id)
                self.conn.commit()
                completed_sources += 1
                self._report_progress(session_source_id)
            except Exception as exc:
                if self.conn.in_transaction:
                    self.conn.rollback()
                marked_failed = self.scan_repo.mark_session_source_failed(
                    session_source_id,
                    cursor_json=json.dumps({"error": str(exc)}, ensure_ascii=False),
                )
                self.conn.commit()
                if marked_failed > 0:
                    failed_sources += 1
                    self._report_progress(session_source_id)

        final_status = self.scan_repo.finalize_session_if_all_sources_terminal(session_id)
        self.conn.commit()
        return {
            "session_id": int(session_id),
            "new_asset_count": int(total_new_assets),
            "completed_source_count": int(completed_sources),
            "failed_source_count": int(failed_sources),
            "session_completed": 1 if final_status == "completed" else 0,
            "session_failed": 1 if final_status == "failed" else 0,
        }

    def _discover_source_assets(self, *, session_source_id: int, library_source_id: int, source_root: Path) -> int:
        if not source_root.exists() or not source_root.is_dir():
            raise FileNotFoundError(f"扫描源目录不存在或不可访问: {source_root}")

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            new_assets = 0
            for candidate in iter_candidate_photos(source_root):
                _, created = self.asset_repo.upsert_photo_asset_from_scan(
                    library_source_id=library_source_id,
                    primary_path=str(candidate.path),
                    is_heic=candidate.path.suffix.lower() == ".heic",
                    live_mov_path=str(candidate.live_photo_video) if candidate.live_photo_video is not None else None,
                )
                if created:
                    new_assets += 1

            discover_progress = self._refresh_discover_progress(session_source_id, library_source_id)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        self._write_checkpoint(
            session_source_id,
            phase="discover",
            cursor_json=None,
            pending_asset_count=discover_progress["discovered_count"],
        )
        return new_assets

    def _refresh_discover_progress(self, session_source_id: int, library_source_id: int) -> dict[str, int]:
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

    def _write_checkpoint(
        self,
        session_source_id: int,
        *,
        phase: str,
        cursor_json: str | None,
        pending_asset_count: int,
    ) -> None:
        if self._checkpoint_writer is None:
            self.scan_repo.insert_checkpoint(
                session_source_id,
                phase=phase,
                cursor_json=cursor_json,
                pending_asset_count=pending_asset_count,
            )
            self.conn.commit()
        else:
            self._checkpoint_writer(session_source_id, phase, cursor_json, pending_asset_count)
        self._report_progress(
            session_source_id,
            phase=phase,
            pending_asset_count=pending_asset_count,
        )

    def _stage_pending_count(self, progress: dict[str, int], stage: str) -> int:
        discovered = int(progress.get("discovered_count", 0))
        if stage == "metadata":
            return max(0, discovered - int(progress.get("metadata_done_count", 0)))
        if stage == "faces":
            return max(0, discovered - int(progress.get("faces_done_count", 0)))
        if stage == "embeddings":
            return max(0, discovered - int(progress.get("embeddings_done_count", 0)))
        return max(0, discovered - int(progress.get("assignment_done_count", 0)))

    def _is_terminal_source(self, source_row: dict[str, object] | None) -> bool:
        if source_row is None:
            return True
        return str(source_row["status"]) in _SOURCE_TERMINAL_STATUSES

    def _report_progress(
        self,
        session_source_id: int,
        *,
        phase: str | None = None,
        pending_asset_count: int | None = None,
    ) -> None:
        if self._progress_reporter is None:
            return

        source_row = self.scan_repo.get_session_source(session_source_id)
        if source_row is None:
            return

        discovered_count = int(source_row.get("discovered_count") or 0)
        metadata_done_count = int(source_row.get("metadata_done_count") or 0)
        faces_done_count = int(source_row.get("faces_done_count") or 0)
        embeddings_done_count = int(source_row.get("embeddings_done_count") or 0)
        assignment_done_count = int(source_row.get("assignment_done_count") or 0)

        payload: dict[str, Any] = {
            "session_id": int(source_row["scan_session_id"]),
            "session_source_id": int(source_row["id"]),
            "library_source_id": int(source_row["library_source_id"]),
            "status": str(source_row["status"]),
            "discovered_count": discovered_count,
            "metadata_done_count": metadata_done_count,
            "faces_done_count": faces_done_count,
            "embeddings_done_count": embeddings_done_count,
            "assignment_done_count": assignment_done_count,
            "pending_asset_count": (
                int(pending_asset_count)
                if pending_asset_count is not None
                else self._infer_pending_asset_count(
                    discovered_count=discovered_count,
                    metadata_done_count=metadata_done_count,
                    faces_done_count=faces_done_count,
                    embeddings_done_count=embeddings_done_count,
                    assignment_done_count=assignment_done_count,
                )
            ),
        }
        if phase is not None:
            payload["phase"] = phase
        self._progress_reporter(payload)

    def _infer_pending_asset_count(
        self,
        *,
        discovered_count: int,
        metadata_done_count: int,
        faces_done_count: int,
        embeddings_done_count: int,
        assignment_done_count: int,
    ) -> int:
        if metadata_done_count < discovered_count:
            return max(0, discovered_count - metadata_done_count)
        if faces_done_count < discovered_count:
            return max(0, discovered_count - faces_done_count)
        if embeddings_done_count < discovered_count:
            return max(0, discovered_count - embeddings_done_count)
        return max(0, discovered_count - assignment_done_count)
