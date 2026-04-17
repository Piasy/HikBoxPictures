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


def _migration_name(version: int) -> str:
    names = {
        1: "people_gallery",
        2: "photo_asset_progress_index",
        3: "person_face_exclusion",
        4: "identity_rebuild_v3_schema",
    }
    return names[version]


def _migration_path(version: int) -> Path:
    return (
        _repo_root()
        / "src"
        / "hikbox_pictures"
        / "db"
        / "migrations"
        / f"{version:04d}_{_migration_name(version)}.sql"
    )


def _reset_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _apply_schema_v3(conn: sqlite3.Connection) -> None:
    for version in (1, 2, 3, 4):
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
    for version in (1, 2, 3, 4):
        conn.execute(
            "INSERT INTO schema_migration(version, name) VALUES (?, ?)",
            (version, _migration_name(version)),
        )


def _seed_data(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO library_source(id, name, root_path, root_fingerprint, active)
        VALUES (1, 'fixture-source', '/fixture/source', 'fixture-source-fp', 1)
        """
    )
    conn.execute(
        """
        INSERT INTO photo_asset(id, library_source_id, primary_path, processing_status, capture_datetime, capture_month)
        VALUES
            (1, 1, '/fixture/source/a.jpg', 'assignment_done', '2025-01-01T10:00:00+08:00', '2025-01'),
            (2, 1, '/fixture/source/b.jpg', 'assignment_done', '2025-01-02T10:00:00+08:00', '2025-01')
        """
    )
    conn.execute(
        """
        INSERT INTO face_observation(
            id, photo_asset_id, bbox_top, bbox_right, bbox_bottom, bbox_left, face_area_ratio, sharpness_score, pose_score, quality_score, active
        )
        VALUES
            (101, 1, 0.0, 0.5, 0.5, 0.0, 0.25, 0.8, 0.7, 0.83, 1),
            (102, 1, 0.5, 1.0, 1.0, 0.5, 0.20, 0.7, 0.6, 0.76, 1),
            (103, 2, 0.0, 0.5, 0.5, 0.0, 0.22, 0.75, 0.7, 0.79, 1)
        """
    )
    conn.execute(
        """
        INSERT INTO identity_threshold_profile(
            id,
            profile_name,
            profile_version,
            quality_formula_version,
            embedding_feature_type,
            embedding_model_key,
            embedding_distance_metric,
            embedding_schema_version,
            quality_area_weight,
            quality_sharpness_weight,
            quality_pose_weight,
            area_log_p10,
            area_log_p90,
            sharpness_log_p10,
            sharpness_log_p90,
            pose_score_p10,
            pose_score_p90,
            low_quality_threshold,
            high_quality_threshold,
            trusted_seed_quality_threshold,
            bootstrap_edge_accept_threshold,
            bootstrap_edge_candidate_threshold,
            bootstrap_margin_threshold,
            bootstrap_min_cluster_size,
            bootstrap_min_distinct_photo_count,
            bootstrap_min_high_quality_count,
            bootstrap_seed_min_count,
            bootstrap_seed_max_count,
            assignment_auto_min_quality,
            assignment_auto_distance_threshold,
            assignment_auto_margin_threshold,
            assignment_review_distance_threshold,
            assignment_require_photo_conflict_free,
            trusted_min_quality,
            trusted_centroid_distance_threshold,
            trusted_margin_threshold,
            trusted_block_exact_duplicate,
            trusted_block_burst_duplicate,
            burst_time_window_seconds,
            possible_merge_distance_threshold,
            possible_merge_margin_threshold,
            active,
            activated_at
        )
        VALUES (
            1,
            'phase1-default',
            'v3.0.0',
            'quality.v1',
            'face',
            'insightface',
            'cosine',
            'face.embedding.v1',
            0.4,
            0.4,
            0.2,
            -2.0,
            1.6,
            -3.0,
            1.2,
            0.2,
            0.95,
            0.35,
            0.65,
            0.7,
            0.35,
            0.25,
            0.08,
            3,
            2,
            1,
            1,
            6,
            0.35,
            0.28,
            0.08,
            0.33,
            1,
            0.45,
            0.24,
            0.07,
            1,
            1,
            30,
            0.22,
            0.05,
            1,
            CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        INSERT INTO auto_cluster_batch(id, model_key, algorithm_version, batch_type, threshold_profile_id)
        VALUES (11, 'insightface', 'identity.rebuild.v3', 'bootstrap', 1)
        """
    )
    conn.execute(
        """
        INSERT INTO auto_cluster(
            id,
            batch_id,
            representative_observation_id,
            cluster_status,
            resolved_person_id,
            diagnostic_json
        )
        VALUES
            (201, 11, 101, 'materialized', NULL, '{"legacy": true}'),
            (202, 11, 103, 'discarded', NULL, '{"legacy": true}')
        """
    )
    conn.execute(
        """
        INSERT INTO auto_cluster_member(
            id,
            cluster_id,
            face_observation_id,
            membership_score,
            quality_score_snapshot,
            is_seed_candidate
        )
        VALUES
            (301, 201, 101, 0.95, 0.83, 1),
            (302, 201, 102, 0.82, 0.76, 1),
            (303, 202, 103, 0.40, 0.79, 0)
        """
    )
    conn.execute(
        """
        INSERT INTO person(
            id,
            display_name,
            cover_observation_id,
            status,
            notes,
            confirmed,
            ignored,
            merged_into_person_id,
            origin_cluster_id
        )
        VALUES
            (1, 'Alice', 101, 'active', NULL, 1, 0, NULL, 201),
            (2, 'Bob', 102, 'active', NULL, 1, 0, NULL, 201),
            (3, 'Ghost', 103, 'ignored', NULL, 0, 1, NULL, 202)
        """
    )
    conn.execute(
        """
        INSERT INTO person_face_assignment(
            id,
            person_id,
            face_observation_id,
            assignment_source,
            diagnostic_json,
            threshold_profile_id,
            locked,
            confirmed_at,
            active
        )
        VALUES
            (1, 1, 101, 'bootstrap', '{"legacy": true}', 1, 1, CURRENT_TIMESTAMP, 1),
            (2, 2, 102, 'manual', '{"legacy": true}', 1, 1, CURRENT_TIMESTAMP, 1),
            (3, 3, 103, 'auto', '{"legacy": true}', 1, 0, NULL, 0)
        """
    )
    conn.execute(
        """
        INSERT INTO person_trusted_sample(
            id,
            person_id,
            face_observation_id,
            trust_source,
            trust_score,
            quality_score_snapshot,
            threshold_profile_id,
            source_review_id,
            source_auto_cluster_id,
            active
        )
        VALUES
            (1, 1, 101, 'bootstrap_seed', 0.98, 0.83, 1, NULL, 201, 1)
        """
    )


def build_fixture(output: Path) -> Path:
    conn = _reset_db(output)
    try:
        _apply_schema_v3(conn)
        _seed_data(conn)
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_violations:
            raise RuntimeError(f"fixture 外键校验失败: {fk_violations}")
        conn.commit()
    finally:
        conn.close()
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 identity v3 phase1 migration 测试所需的 v3 小型数据库 fixture")
    parser.add_argument(
        "--output",
        type=Path,
        default=_repo_root() / "tests" / "data" / "identity-v3-phase1-small.db",
        help="输出 SQLite 文件路径",
    )
    args = parser.parse_args()
    output = build_fixture(args.output.resolve())
    print(f"已生成: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
