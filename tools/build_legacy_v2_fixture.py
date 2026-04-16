#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _migration_path(version: int) -> Path:
    return _repo_root() / "src" / "hikbox_pictures" / "db" / "migrations" / f"{version:04d}_{_migration_name(version)}.sql"


def _migration_name(version: int) -> str:
    names = {
        1: "people_gallery",
        2: "photo_asset_progress_index",
        3: "person_face_exclusion",
    }
    return names[version]


def _reset_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _apply_legacy_schema(conn: sqlite3.Connection) -> None:
    for version in (1, 2, 3):
        conn.executescript(_migration_path(version).read_text(encoding="utf-8"))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migration (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    for version in (1, 2, 3):
        conn.execute(
            "INSERT INTO schema_migration(version, name) VALUES (?, ?)",
            (version, _migration_name(version)),
        )


def _seed_legacy_data(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO library_source(id, name, root_path, root_fingerprint, active)
        VALUES (1, 'legacy-source', '/legacy/source', 'legacy-fp-1', 1)
        """
    )
    conn.execute(
        """
        INSERT INTO scan_session(id, mode, status, started_at, finished_at)
        VALUES (1, 'initial', 'completed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    conn.execute(
        """
        INSERT INTO photo_asset(id, library_source_id, primary_path, processing_status, capture_datetime, capture_month)
        VALUES
            (1, 1, '/legacy/source/a.jpg', 'assignment_done', '2025-01-01T10:00:00+08:00', '2025-01'),
            (2, 1, '/legacy/source/b.jpg', 'assignment_done', '2025-01-02T10:00:00+08:00', '2025-01')
        """
    )
    conn.execute(
        """
        INSERT INTO face_observation(
            id, photo_asset_id, bbox_top, bbox_right, bbox_bottom, bbox_left, face_area_ratio, active
        )
        VALUES
            (101, 1, 0.0, 0.5, 0.5, 0.0, 0.25, 1),
            (102, 1, 0.5, 1.0, 1.0, 0.5, 0.20, 1),
            (103, 2, 0.0, 0.5, 0.5, 0.0, 0.22, 1),
            (104, 2, 0.5, 1.0, 1.0, 0.5, 0.18, 1)
        """
    )
    conn.execute(
        """
        INSERT INTO auto_cluster_batch(id, model_key, algorithm_version)
        VALUES (11, 'pipeline-stub-v1', 'hdbscan-v1')
        """
    )
    conn.execute(
        """
        INSERT INTO auto_cluster(id, batch_id, confidence, representative_observation_id)
        VALUES (21, 11, 0.93, 101)
        """
    )
    conn.execute(
        """
        INSERT INTO auto_cluster_member(id, cluster_id, face_observation_id, membership_score)
        VALUES
            (31, 21, 101, 0.97),
            (32, 21, 102, 0.81)
        """
    )
    conn.execute(
        """
        INSERT INTO person(
            id, display_name, cover_observation_id, status, confirmed, ignored, merged_into_person_id
        )
        VALUES
            (1, 'Penny', 101, 'active', 1, 0, NULL),
            (2, 'Piasy', 102, 'active', 1, 0, NULL),
            (3, 'Legacy-03', NULL, 'active', 1, 0, NULL)
        """
    )
    conn.execute(
        """
        INSERT INTO person_face_assignment(
            id, person_id, face_observation_id, assignment_source, confidence, locked, confirmed_at, active
        )
        VALUES
            (1, 1, 101, 'manual', 1.0, 1, NULL, 1),
            (2, 2, 102, 'manual', 0.9, 1, NULL, 1),
            (3, 1, 103, 'auto', 0.6, 0, NULL, 1),
            (4, 3, 104, 'split', 0.4, 0, NULL, 0)
        """
    )
    conn.execute(
        """
        INSERT INTO person_face_exclusion(
            id, person_id, face_observation_id, assignment_id, reason, active
        )
        VALUES (1, 2, 103, 3, 'manual_exclude', 1)
        """
    )
    conn.execute(
        """
        INSERT INTO review_item(
            id, review_type, primary_person_id, secondary_person_id, face_observation_id, payload_json, priority, status
        )
        VALUES
            (1, 'new_person', 1, NULL, 103, '{}', 10, 'open')
        """
    )
    conn.execute(
        """
        INSERT INTO export_template(
            id, name, output_root, include_group, export_live_mov, enabled
        )
        VALUES (1, 'legacy-template', '/tmp/export', 1, 0, 1)
        """
    )
    conn.execute(
        """
        INSERT INTO export_template_person(id, template_id, person_id, position)
        VALUES
            (1, 1, 1, 0),
            (2, 1, 2, 1)
        """
    )


def build_fixture(output: Path) -> Path:
    conn = _reset_db(output)
    try:
        _apply_legacy_schema(conn)
        _seed_legacy_data(conn)
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_violations:
            raise RuntimeError(f"legacy fixture 外键校验失败: {fk_violations}")
        conn.commit()
    finally:
        conn.close()
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="生成用于真实升级测试的 legacy v2 小型数据库 fixture")
    parser.add_argument(
        "--output",
        type=Path,
        default=_repo_root() / "tests" / "data" / "legacy-v2-small.db",
        help="输出 SQLite 文件路径",
    )
    args = parser.parse_args()
    output = build_fixture(args.output.resolve())
    print(f"已生成: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
