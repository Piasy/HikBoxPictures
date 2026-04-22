"""discover 阶段：按 source 扫描资产并登记入库。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hikbox_pictures.product.db.connection import connect_sqlite
from hikbox_pictures.product.scan.errors import StageSchemaMissingError
from hikbox_pictures.product.scan.fingerprint import sha256_file
from hikbox_pictures.product.scan.models import DiscoverSourceSummary, DiscoverStageSummary

DISCOVER_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
REQUIRED_TABLES = {"library_source", "photo_asset", "scan_session_source", "scan_session"}


def should_rerun_asset_stages(*, old_file_size: int, old_mtime_ns: int, new_file_size: int, new_mtime_ns: int) -> bool:
    """文件大小或 mtime 变化时，要求后续阶段全量重跑该资产。"""
    return old_file_size != new_file_size or old_mtime_ns != new_mtime_ns


class DiscoverStageService:
    """discover 阶段服务。"""

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)

    def run(self, *, scan_session_id: int) -> DiscoverStageSummary:
        conn = connect_sqlite(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            self._assert_required_tables(conn)
            source_rows = conn.execute(
                """
                SELECT id, root_path
                FROM library_source
                WHERE enabled = 1 AND removed_at IS NULL
                ORDER BY id ASC
                """
            ).fetchall()

            by_source: dict[int, DiscoverSourceSummary] = {}
            for source_row in source_rows:
                source_id = int(source_row[0])
                source_root = Path(str(source_row[1]))
                summary = self._process_source(conn, source_id=source_id, source_root=source_root)
                self._upsert_source_progress(conn, scan_session_id=scan_session_id, summary=summary)
                by_source[source_id] = summary

                conn.execute(
                    """
                    UPDATE library_source
                    SET last_discovered_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (source_id,),
                )

            conn.commit()
            return DiscoverStageSummary(by_source=by_source)
        finally:
            conn.close()

    def _process_source(self, conn: sqlite3.Connection, *, source_id: int, source_root: Path) -> DiscoverSourceSummary:
        should_rerun = False
        failed_assets = 0
        processed_assets = 0
        discovered_files = self._list_discoverable_files(source_root)
        seen_relpaths: set[str] = set()

        for file_path in discovered_files:
            rel_path = file_path.relative_to(source_root).as_posix()
            seen_relpaths.add(rel_path)
            try:
                stat = file_path.stat()
                file_size = int(stat.st_size)
                mtime_ns = int(stat.st_mtime_ns)

                existing = conn.execute(
                    """
                    SELECT id, file_size, mtime_ns, asset_status
                    FROM photo_asset
                    WHERE library_source_id = ? AND primary_path = ?
                    """,
                    (source_id, rel_path),
                ).fetchone()

                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO photo_asset(
                          library_source_id, primary_path, primary_fingerprint, fingerprint_algo,
                          file_size, mtime_ns, asset_status, created_at, updated_at
                        ) VALUES (?, ?, ?, 'sha256', ?, ?, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        """,
                        (source_id, rel_path, sha256_file(file_path), file_size, mtime_ns),
                    )
                    should_rerun = True
                    processed_assets += 1
                    continue

                old_file_size = int(existing[1])
                old_mtime_ns = int(existing[2])
                old_status = str(existing[3])
                changed = should_rerun_asset_stages(
                    old_file_size=old_file_size,
                    old_mtime_ns=old_mtime_ns,
                    new_file_size=file_size,
                    new_mtime_ns=mtime_ns,
                )

                if changed:
                    conn.execute(
                        """
                        UPDATE photo_asset
                        SET primary_fingerprint = ?,
                            file_size = ?,
                            mtime_ns = ?,
                            capture_datetime = NULL,
                            capture_month = NULL,
                            is_live_photo = 0,
                            live_mov_path = NULL,
                            live_mov_size = NULL,
                            live_mov_mtime_ns = NULL,
                            asset_status = 'active',
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (sha256_file(file_path), file_size, mtime_ns, int(existing[0])),
                    )
                    should_rerun = True
                    processed_assets += 1
                    continue

                if old_status != "active":
                    conn.execute(
                        """
                        UPDATE photo_asset
                        SET asset_status = 'active',
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (int(existing[0]),),
                    )
                    should_rerun = True
                processed_assets += 1
            except OSError:
                failed_assets += 1

        missing_rows = conn.execute(
            """
            SELECT id, primary_path
            FROM photo_asset
            WHERE library_source_id = ? AND asset_status = 'active'
            """,
            (source_id,),
        ).fetchall()
        for row in missing_rows:
            if str(row[1]) in seen_relpaths:
                continue
            conn.execute(
                """
                UPDATE photo_asset
                SET asset_status = 'missing',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (int(row[0]),),
            )
            should_rerun = True

        return DiscoverSourceSummary(
            source_id=source_id,
            discovered_assets=len(discovered_files),
            processed_assets=processed_assets,
            failed_assets=failed_assets,
            should_rerun=should_rerun,
        )

    def _list_discoverable_files(self, source_root: Path) -> list[Path]:
        files: list[Path] = []
        for path in sorted(source_root.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in DISCOVER_IMAGE_SUFFIXES:
                continue
            files.append(path)
        return files

    def _upsert_source_progress(
        self,
        conn: sqlite3.Connection,
        *,
        scan_session_id: int,
        summary: DiscoverSourceSummary,
    ) -> None:
        stage_status = {
            "discover": "done",
            "metadata": "pending" if summary.should_rerun else "done",
        }
        conn.execute(
            """
            INSERT INTO scan_session_source(
              scan_session_id, library_source_id, stage_status_json, processed_assets, failed_assets, updated_at
            ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(scan_session_id, library_source_id)
            DO UPDATE SET
              stage_status_json = excluded.stage_status_json,
              processed_assets = excluded.processed_assets,
              failed_assets = excluded.failed_assets,
              updated_at = CURRENT_TIMESTAMP
            """,
            (
                scan_session_id,
                summary.source_id,
                json.dumps(stage_status, ensure_ascii=False, sort_keys=True),
                summary.processed_assets,
                summary.failed_assets,
            ),
        )

    def _assert_required_tables(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        existing = {str(row[0]) for row in rows}
        missing = sorted(REQUIRED_TABLES - existing)
        if missing:
            raise StageSchemaMissingError(stage="discover", missing_tables=missing)
