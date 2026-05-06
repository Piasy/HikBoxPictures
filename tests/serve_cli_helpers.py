"""test_hikbox_serve_cli 子文件共享 helper 函数与常量。"""

from __future__ import annotations

import json
from pathlib import Path
import socket
import sqlite3
import time
from typing import Any

import httpx

from tests.helpers import (
    FIXTURE_DIR,
    fetch_all,
    find_free_port,
)


# ---------------------------------------------------------------------------
# SQL 常量：用于构造旧版 / 不完整 schema 的 workspace
# ---------------------------------------------------------------------------

OLD_SLICE_A_LIBRARY_SQL = """
CREATE TABLE schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

INSERT INTO schema_meta (key, value) VALUES ('schema_version', '3');

CREATE TABLE library_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  created_at TEXT NOT NULL
);
""".strip()

OLD_SLICE_A_EMBEDDING_SQL = """
CREATE TABLE schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1');
""".strip()

BROKEN_WEBUI_LIBRARY_SQL = """
CREATE TABLE schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

INSERT INTO schema_meta (key, value) VALUES ('schema_version', '3');

CREATE TABLE library_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  created_at TEXT NOT NULL
);

CREATE TABLE assets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL REFERENCES library_sources(id),
  absolute_path TEXT NOT NULL UNIQUE,
  file_name TEXT NOT NULL,
  file_extension TEXT NOT NULL,
  capture_month TEXT NOT NULL,
  processing_status TEXT NOT NULL,
  failure_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE scan_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_size INTEGER NOT NULL,
  status TEXT NOT NULL,
  command TEXT NOT NULL,
  total_batches INTEGER NOT NULL DEFAULT 0,
  completed_batches INTEGER NOT NULL DEFAULT 0,
  failed_assets INTEGER NOT NULL DEFAULT 0,
  success_faces INTEGER NOT NULL DEFAULT 0,
  artifact_files INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE TABLE face_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id INTEGER NOT NULL REFERENCES assets(id),
  face_index INTEGER NOT NULL,
  bbox_x1 REAL NOT NULL,
  bbox_y1 REAL NOT NULL,
  bbox_x2 REAL NOT NULL,
  bbox_y2 REAL NOT NULL,
  image_width INTEGER NOT NULL,
  image_height INTEGER NOT NULL,
  score REAL NOT NULL,
  crop_path TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE person (
  id TEXT PRIMARY KEY,
  display_name TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE person_name_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id TEXT NOT NULL REFERENCES person(id),
  event_type TEXT NOT NULL,
  new_display_name TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE person_face_assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id TEXT NOT NULL REFERENCES person(id),
  face_observation_id INTEGER NOT NULL REFERENCES face_observations(id),
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE person_face_exclusions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  face_observation_id INTEGER NOT NULL REFERENCES face_observations(id),
  excluded_person_id TEXT NOT NULL REFERENCES person(id),
  source_assignment_id INTEGER REFERENCES person_face_assignments(id),
  created_at TEXT NOT NULL
);

CREATE TABLE person_merge_operations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  winner_person_id TEXT NOT NULL REFERENCES person(id),
  loser_person_id TEXT NOT NULL REFERENCES person(id),
  winner_display_name_before TEXT,
  winner_is_named_before INTEGER NOT NULL,
  winner_status_before TEXT NOT NULL,
  loser_display_name_before TEXT,
  loser_is_named_before INTEGER NOT NULL,
  loser_status_before TEXT NOT NULL,
  merged_at TEXT NOT NULL
);

CREATE TABLE person_merge_operation_assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  merge_operation_id INTEGER NOT NULL REFERENCES person_merge_operations(id),
  assignment_id INTEGER NOT NULL REFERENCES person_face_assignments(id),
  person_role TEXT NOT NULL
);

CREATE TABLE export_template (
  template_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  output_root TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active', 'invalid')),
  created_at TEXT NOT NULL,
  dedup_key TEXT NOT NULL UNIQUE
);

CREATE TABLE export_template_person (
  template_id TEXT NOT NULL REFERENCES export_template(template_id),
  person_id TEXT NOT NULL REFERENCES person(id),
  PRIMARY KEY (template_id, person_id)
);

CREATE TABLE export_run (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  template_id TEXT NOT NULL REFERENCES export_template(template_id),
  status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
  started_at TEXT NOT NULL,
  completed_at TEXT,
  copied_count INTEGER NOT NULL DEFAULT 0,
  skipped_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE export_delivery (
  delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES export_run(run_id),
  asset_id INTEGER NOT NULL REFERENCES assets(id),
  target_path TEXT NOT NULL,
  result TEXT NOT NULL CHECK (result IN ('copied', 'skipped_exists')),
  mov_result TEXT NOT NULL CHECK (mov_result IN ('copied', 'skipped_missing', 'not_applicable'))
);
""".strip()

BROKEN_WEBUI_LIBRARY_SQL_MISSING_ASSIGNMENT_UPDATED_AT = """
CREATE TABLE schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

INSERT INTO schema_meta (key, value) VALUES ('schema_version', '3');

CREATE TABLE library_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  created_at TEXT NOT NULL
);

CREATE TABLE assets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL REFERENCES library_sources(id),
  absolute_path TEXT NOT NULL UNIQUE,
  file_name TEXT NOT NULL,
  file_extension TEXT NOT NULL,
  capture_month TEXT NOT NULL,
  processing_status TEXT NOT NULL,
  failure_reason TEXT,
  live_photo_mov_path TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE scan_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_size INTEGER NOT NULL,
  status TEXT NOT NULL,
  command TEXT NOT NULL,
  total_batches INTEGER NOT NULL DEFAULT 0,
  completed_batches INTEGER NOT NULL DEFAULT 0,
  failed_assets INTEGER NOT NULL DEFAULT 0,
  success_faces INTEGER NOT NULL DEFAULT 0,
  artifact_files INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE TABLE face_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id INTEGER NOT NULL REFERENCES assets(id),
  face_index INTEGER NOT NULL,
  bbox_x1 REAL NOT NULL,
  bbox_y1 REAL NOT NULL,
  bbox_x2 REAL NOT NULL,
  bbox_y2 REAL NOT NULL,
  image_width INTEGER NOT NULL,
  image_height INTEGER NOT NULL,
  score REAL NOT NULL,
  crop_path TEXT NOT NULL,
  context_path TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE person (
  id TEXT PRIMARY KEY,
  display_name TEXT,
  is_named INTEGER NOT NULL DEFAULT 0 CHECK (is_named IN (0, 1)),
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE person_name_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id TEXT NOT NULL REFERENCES person(id),
  event_type TEXT NOT NULL,
  old_display_name TEXT,
  new_display_name TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE person_face_assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id TEXT NOT NULL REFERENCES person(id),
  face_observation_id INTEGER NOT NULL REFERENCES face_observations(id),
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE TABLE person_face_exclusions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  face_observation_id INTEGER NOT NULL REFERENCES face_observations(id),
  excluded_person_id TEXT NOT NULL REFERENCES person(id),
  source_assignment_id INTEGER REFERENCES person_face_assignments(id),
  created_at TEXT NOT NULL
);

CREATE TABLE person_merge_operations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  winner_person_id TEXT NOT NULL REFERENCES person(id),
  loser_person_id TEXT NOT NULL REFERENCES person(id),
  winner_display_name_before TEXT,
  winner_is_named_before INTEGER NOT NULL,
  winner_status_before TEXT NOT NULL,
  loser_display_name_before TEXT,
  loser_is_named_before INTEGER NOT NULL,
  loser_status_before TEXT NOT NULL,
  merged_at TEXT NOT NULL
);

CREATE TABLE person_merge_operation_assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  merge_operation_id INTEGER NOT NULL REFERENCES person_merge_operations(id),
  assignment_id INTEGER NOT NULL REFERENCES person_face_assignments(id),
  person_role TEXT NOT NULL
);

CREATE TABLE export_template (
  template_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  output_root TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active', 'invalid')),
  created_at TEXT NOT NULL,
  dedup_key TEXT NOT NULL UNIQUE
);

CREATE TABLE export_template_person (
  template_id TEXT NOT NULL REFERENCES export_template(template_id),
  person_id TEXT NOT NULL REFERENCES person(id),
  PRIMARY KEY (template_id, person_id)
);

CREATE TABLE export_run (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  template_id TEXT NOT NULL REFERENCES export_template(template_id),
  status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
  started_at TEXT NOT NULL,
  completed_at TEXT,
  copied_count INTEGER NOT NULL DEFAULT 0,
  skipped_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE export_delivery (
  delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES export_run(run_id),
  asset_id INTEGER NOT NULL REFERENCES assets(id),
  target_path TEXT NOT NULL,
  result TEXT NOT NULL CHECK (result IN ('copied', 'skipped_exists')),
  mov_result TEXT NOT NULL CHECK (mov_result IN ('copied', 'skipped_missing', 'not_applicable'))
);
""".strip()


# ---------------------------------------------------------------------------
# 构造旧版 / 不完整 schema 的 workspace
# ---------------------------------------------------------------------------


def create_slice_a_only_workspace(workspace: Path, external_root: Path, source_dir: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    hikbox_dir = workspace / ".hikbox"
    hikbox_dir.mkdir(parents=True, exist_ok=True)
    (external_root / "artifacts" / "crops").mkdir(parents=True, exist_ok=True)
    (external_root / "artifacts" / "context").mkdir(parents=True, exist_ok=True)
    (external_root / "logs").mkdir(parents=True, exist_ok=True)
    (hikbox_dir / "config.json").write_text(
        json.dumps(
            {
                "config_version": 1,
                "external_root": str(external_root.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    library_db = hikbox_dir / "library.db"
    embedding_db = hikbox_dir / "embedding.db"
    library_conn = sqlite3.connect(library_db)
    try:
        with library_conn:
            library_conn.executescript(OLD_SLICE_A_LIBRARY_SQL)
            library_conn.execute(
                """
                INSERT INTO library_sources (path, label, active, created_at)
                VALUES (?, 'legacy-source', 1, '2026-04-24T00:00:00Z')
                """,
                (str(source_dir.resolve()),),
            )
    finally:
        library_conn.close()

    embedding_conn = sqlite3.connect(embedding_db)
    try:
        with embedding_conn:
            embedding_conn.executescript(OLD_SLICE_A_EMBEDDING_SQL)
    finally:
        embedding_conn.close()


def create_broken_webui_workspace(workspace: Path, external_root: Path, source_dir: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    hikbox_dir = workspace / ".hikbox"
    hikbox_dir.mkdir(parents=True, exist_ok=True)
    (external_root / "artifacts" / "crops").mkdir(parents=True, exist_ok=True)
    (external_root / "artifacts" / "context").mkdir(parents=True, exist_ok=True)
    (external_root / "logs").mkdir(parents=True, exist_ok=True)
    (hikbox_dir / "config.json").write_text(
        json.dumps(
            {
                "config_version": 1,
                "external_root": str(external_root.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    library_db = hikbox_dir / "library.db"
    embedding_db = hikbox_dir / "embedding.db"
    library_conn = sqlite3.connect(library_db)
    try:
        with library_conn:
            library_conn.executescript(BROKEN_WEBUI_LIBRARY_SQL)
            library_conn.execute(
                """
                INSERT INTO library_sources (path, label, active, created_at)
                VALUES (?, 'broken-source', 1, '2026-04-24T00:00:00Z')
                """,
                (str(source_dir.resolve()),),
            )
    finally:
        library_conn.close()

    embedding_conn = sqlite3.connect(embedding_db)
    try:
        with embedding_conn:
            embedding_conn.executescript(OLD_SLICE_A_EMBEDDING_SQL)
    finally:
        embedding_conn.close()


def create_broken_webui_workspace_missing_assignment_updated_at(
    workspace: Path,
    external_root: Path,
    source_dir: Path,
) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    hikbox_dir = workspace / ".hikbox"
    hikbox_dir.mkdir(parents=True, exist_ok=True)
    (external_root / "artifacts" / "crops").mkdir(parents=True, exist_ok=True)
    (external_root / "artifacts" / "context").mkdir(parents=True, exist_ok=True)
    (external_root / "logs").mkdir(parents=True, exist_ok=True)
    (hikbox_dir / "config.json").write_text(
        json.dumps(
            {
                "config_version": 1,
                "external_root": str(external_root.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    library_db = hikbox_dir / "library.db"
    embedding_db = hikbox_dir / "embedding.db"
    library_conn = sqlite3.connect(library_db)
    try:
        with library_conn:
            library_conn.executescript(BROKEN_WEBUI_LIBRARY_SQL_MISSING_ASSIGNMENT_UPDATED_AT)
            library_conn.execute(
                """
                INSERT INTO library_sources (path, label, active, created_at)
                VALUES (?, 'broken-source', 1, '2026-04-24T00:00:00Z')
                """,
                (str(source_dir.resolve()),),
            )
    finally:
        library_conn.close()

    embedding_conn = sqlite3.connect(embedding_db)
    try:
        with embedding_conn:
            embedding_conn.executescript(OLD_SLICE_A_EMBEDDING_SQL)
    finally:
        embedding_conn.close()


# ---------------------------------------------------------------------------
# 端口 / 进程辅助
# ---------------------------------------------------------------------------


def port_is_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_for_batch_status(db_path: Path, *, batch_index: int, expected_status: str) -> None:
    deadline = time.time() + 90
    while time.time() < deadline:
        connection = sqlite3.connect(db_path)
        try:
            row = connection.execute(
                "SELECT status FROM scan_batches WHERE batch_index = ?",
                (batch_index,),
            ).fetchone()
        finally:
            connection.close()
        if row is not None and str(row[0]) == expected_status:
            return
        time.sleep(0.2)
    raise AssertionError(f"等待 batch_index={batch_index} 进入 {expected_status} 超时")


# ---------------------------------------------------------------------------
# 合并 / 撤销 / 排除：DB 快照与读取辅助
# ---------------------------------------------------------------------------


def read_merge_slice_db_snapshot(library_db: Path) -> dict[str, Any]:
    return {
        "people": fetch_all(
            library_db,
            """
            SELECT id, display_name, is_named, status, write_revision, updated_at
            FROM person
            ORDER BY id ASC
            """,
        ),
        "active_assignments": fetch_all(
            library_db,
            """
            SELECT id, person_id, face_observation_id, active, updated_at
            FROM person_face_assignments
            ORDER BY id ASC
            """,
        ),
        "merge_operations": fetch_all(
            library_db,
            """
            SELECT
              id,
              winner_person_id,
              loser_person_id,
              winner_write_revision_after_merge,
              loser_write_revision_after_merge,
              undone_at
            FROM person_merge_operations
            ORDER BY id ASC
            """,
        ),
        "merge_assignments": fetch_all(
            library_db,
            """
            SELECT merge_operation_id, assignment_id, person_role
            FROM person_merge_operation_assignments
            ORDER BY id ASC
            """,
        ),
        "exclusions": fetch_all(
            library_db,
            """
            SELECT id, face_observation_id, excluded_person_id, source_assignment_id, created_at
            FROM person_face_exclusions
            ORDER BY id ASC
            """,
        ),
    }


def read_person_page_status(base_url: str, person_id: str) -> int:
    return int(httpx.get(f"{base_url}/people/{person_id}", timeout=5.0).status_code)


def read_person_write_revision(library_db: Path, person_id: str) -> int:
    return int(
        fetch_all(
            library_db,
            """
            SELECT write_revision
            FROM person
            WHERE id = ?
            """,
            (person_id,),
        )[0][0]
    )


def read_person_face_exclusions(
    library_db: Path,
    *,
    face_observation_id: int | None = None,
    excluded_person_id: str | None = None,
) -> list[tuple[object, ...]]:
    sql = """
        SELECT id, face_observation_id, excluded_person_id, source_assignment_id, created_at
        FROM person_face_exclusions
    """
    params: list[object] = []
    clauses: list[str] = []
    if face_observation_id is not None:
        clauses.append("face_observation_id = ?")
        params.append(face_observation_id)
    if excluded_person_id is not None:
        clauses.append("excluded_person_id = ?")
        params.append(excluded_person_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id ASC"
    return fetch_all(library_db, sql, tuple(params))
