from __future__ import annotations

import sqlite3


class ExportServiceError(RuntimeError):
    """导出模块基础异常。"""


class ExportValidationError(ExportServiceError):
    """导出模板或参数校验失败。"""


class ExportRunLockError(ExportServiceError):
    """导出运行锁：导出进行中，禁止人物归属/合并写。"""

    def __init__(self, export_run_id: int) -> None:
        self.export_run_id = int(export_run_id)
        super().__init__(f"导出进行中，禁止人物归属/合并写: export_run_id={self.export_run_id}")


def ensure_export_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS export_template (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          output_root TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS export_template_person (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          template_id INTEGER NOT NULL REFERENCES export_template(id),
          person_id INTEGER NOT NULL REFERENCES person(id),
          created_at TEXT NOT NULL,
          UNIQUE(template_id, person_id)
        );

        CREATE TABLE IF NOT EXISTS export_run (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          template_id INTEGER NOT NULL REFERENCES export_template(id),
          status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed', 'aborted')),
          summary_json TEXT NOT NULL,
          started_at TEXT NOT NULL,
          finished_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_export_run_status
        ON export_run(status);

        CREATE INDEX IF NOT EXISTS idx_export_run_template
        ON export_run(template_id, started_at);

        CREATE TABLE IF NOT EXISTS export_delivery (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          export_run_id INTEGER NOT NULL REFERENCES export_run(id),
          photo_asset_id INTEGER NOT NULL REFERENCES photo_asset(id),
          media_kind TEXT NOT NULL CHECK (media_kind IN ('photo', 'live_mov')),
          bucket TEXT NOT NULL CHECK (bucket IN ('only', 'group')),
          month_key TEXT NOT NULL,
          destination_path TEXT NOT NULL,
          delivery_status TEXT NOT NULL CHECK (delivery_status IN ('exported', 'skipped_exists', 'failed')),
          error_message TEXT,
          created_at TEXT NOT NULL,
          UNIQUE(export_run_id, media_kind, destination_path)
        );

        CREATE INDEX IF NOT EXISTS idx_export_delivery_status
        ON export_delivery(delivery_status);
        """
    )


__all__ = [
    "ExportRunLockError",
    "ExportServiceError",
    "ExportValidationError",
    "ensure_export_schema",
]
