from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from hikbox_pictures.product.db.connection import connect_sqlite

from .fingerprint import sha256_for_file
from .models import AssetFileState, DiscoverRunSummary, DiscoverSourceSummary

SUPPORTED_PHOTO_EXTENSIONS = {".heic", ".heif", ".jpg", ".jpeg", ".png"}


class DiscoverStage:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    @staticmethod
    def should_rerun(old: AssetFileState, new: AssetFileState) -> bool:
        return old.file_size != new.file_size or old.mtime_ns != new.mtime_ns

    def run(self, *, source_id: int, source_root: Path) -> DiscoverSourceSummary:
        discovered_assets = 0
        rerun_assets = 0
        unchanged_assets = 0

        for file_path in self._iter_photo_files(source_root):
            discovered_assets += 1
            relative_path = file_path.relative_to(source_root).as_posix()
            stat_result = file_path.stat()
            state = AssetFileState(
                file_size=stat_result.st_size,
                mtime_ns=stat_result.st_mtime_ns,
            )
            if self._upsert_asset(
                source_id=source_id,
                primary_path=relative_path,
                state=state,
                file_path=file_path,
            ):
                rerun_assets += 1
            else:
                unchanged_assets += 1

        return DiscoverSourceSummary(
            source_id=source_id,
            discovered_assets=discovered_assets,
            rerun_assets=rerun_assets,
            unchanged_assets=unchanged_assets,
            failed_assets=0,
        )

    def run_for_sources(self, *, scan_session_id: int, sources: dict[int, Path]) -> DiscoverRunSummary:
        by_source: dict[int, DiscoverSourceSummary] = {}
        for source_id, source_root in sources.items():
            summary = self.run(source_id=source_id, source_root=source_root)
            by_source[source_id] = summary
            self._upsert_scan_session_source(
                scan_session_id=scan_session_id,
                source_id=source_id,
                summary=summary,
            )
        return DiscoverRunSummary(by_source=by_source)

    def _iter_photo_files(self, source_root: Path) -> list[Path]:
        files: list[Path] = []
        for path in source_root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in SUPPORTED_PHOTO_EXTENSIONS:
                continue
            if path.name.startswith("."):
                continue
            files.append(path)
        files.sort()
        return files

    def _upsert_asset(
        self,
        *,
        source_id: int,
        primary_path: str,
        state: AssetFileState,
        file_path: Path,
    ) -> bool:
        now = _utc_now()
        with connect_sqlite(self._db_path) as conn:
            row = conn.execute(
                """
                SELECT file_size, mtime_ns
                FROM photo_asset
                WHERE library_source_id=? AND primary_path=?
                """,
                (source_id, primary_path),
            ).fetchone()

            if row is None:
                fingerprint = sha256_for_file(file_path)
                conn.execute(
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
                    VALUES (?, ?, ?, 'sha256', ?, ?, NULL, NULL, 0, NULL, NULL, NULL, 'active', ?, ?)
                    """,
                    (source_id, primary_path, fingerprint, state.file_size, state.mtime_ns, now, now),
                )
                conn.commit()
                return True

            old = AssetFileState(file_size=int(row[0]), mtime_ns=int(row[1]))
            rerun = self.should_rerun(old, state)
            if rerun:
                fingerprint = sha256_for_file(file_path)
                conn.execute(
                    """
                    UPDATE photo_asset
                    SET primary_fingerprint=?,
                        fingerprint_algo='sha256',
                        file_size=?,
                        mtime_ns=?,
                        asset_status='active',
                        updated_at=?
                    WHERE library_source_id=? AND primary_path=?
                    """,
                    (fingerprint, state.file_size, state.mtime_ns, now, source_id, primary_path),
                )
            else:
                conn.execute(
                    """
                    UPDATE photo_asset
                    SET file_size=?,
                        mtime_ns=?,
                        asset_status='active',
                        updated_at=?
                    WHERE library_source_id=? AND primary_path=?
                    """,
                    (state.file_size, state.mtime_ns, now, source_id, primary_path),
                )
            conn.commit()
            return rerun

    def _upsert_scan_session_source(
        self,
        *,
        scan_session_id: int,
        source_id: int,
        summary: DiscoverSourceSummary,
    ) -> None:
        now = _utc_now()
        with connect_sqlite(self._db_path) as conn:
            existing = conn.execute(
                """
                SELECT stage_status_json
                FROM scan_session_source
                WHERE scan_session_id=? AND library_source_id=?
                """,
                (scan_session_id, source_id),
            ).fetchone()
            stage_status: dict[str, str]
            if existing is None or not existing[0]:
                stage_status = {}
            else:
                stage_status = json.loads(str(existing[0]))
            stage_status["discover"] = "completed"

            conn.execute(
                """
                INSERT INTO scan_session_source(
                  scan_session_id,
                  library_source_id,
                  stage_status_json,
                  processed_assets,
                  failed_assets,
                  updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(scan_session_id, library_source_id)
                DO UPDATE SET
                  stage_status_json=excluded.stage_status_json,
                  processed_assets=excluded.processed_assets,
                  failed_assets=excluded.failed_assets,
                  updated_at=excluded.updated_at
                """,
                (
                    scan_session_id,
                    source_id,
                    json.dumps(stage_status, ensure_ascii=False),
                    summary.discovered_assets,
                    summary.failed_assets,
                    now,
                ),
            )
            conn.commit()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
