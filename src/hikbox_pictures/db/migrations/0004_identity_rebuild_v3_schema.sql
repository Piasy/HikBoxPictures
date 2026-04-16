PRAGMA foreign_keys = ON;
PRAGMA defer_foreign_keys = ON;

CREATE TABLE identity_threshold_profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_name TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    quality_formula_version TEXT NOT NULL,
    embedding_feature_type TEXT NOT NULL,
    embedding_model_key TEXT NOT NULL,
    embedding_distance_metric TEXT NOT NULL,
    embedding_schema_version TEXT NOT NULL,
    quality_area_weight REAL NOT NULL,
    quality_sharpness_weight REAL NOT NULL,
    quality_pose_weight REAL NOT NULL,
    area_log_p10 REAL NOT NULL,
    area_log_p90 REAL NOT NULL,
    sharpness_log_p10 REAL NOT NULL,
    sharpness_log_p90 REAL NOT NULL,
    pose_score_p10 REAL,
    pose_score_p90 REAL,
    low_quality_threshold REAL NOT NULL,
    high_quality_threshold REAL NOT NULL,
    trusted_seed_quality_threshold REAL NOT NULL,
    bootstrap_edge_accept_threshold REAL NOT NULL,
    bootstrap_edge_candidate_threshold REAL NOT NULL,
    bootstrap_margin_threshold REAL NOT NULL,
    bootstrap_min_cluster_size INTEGER NOT NULL,
    bootstrap_min_distinct_photo_count INTEGER NOT NULL,
    bootstrap_min_high_quality_count INTEGER NOT NULL,
    bootstrap_seed_min_count INTEGER NOT NULL,
    bootstrap_seed_max_count INTEGER NOT NULL,
    assignment_auto_min_quality REAL NOT NULL,
    assignment_auto_distance_threshold REAL NOT NULL,
    assignment_auto_margin_threshold REAL NOT NULL,
    assignment_review_distance_threshold REAL NOT NULL,
    assignment_require_photo_conflict_free INTEGER NOT NULL CHECK (assignment_require_photo_conflict_free IN (0, 1)),
    trusted_min_quality REAL NOT NULL,
    trusted_centroid_distance_threshold REAL NOT NULL,
    trusted_margin_threshold REAL NOT NULL,
    trusted_block_exact_duplicate INTEGER NOT NULL CHECK (trusted_block_exact_duplicate IN (0, 1)),
    trusted_block_burst_duplicate INTEGER NOT NULL CHECK (trusted_block_burst_duplicate IN (0, 1)),
    burst_time_window_seconds INTEGER NOT NULL,
    possible_merge_distance_threshold REAL,
    possible_merge_margin_threshold REAL,
    active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
    activated_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX uq_identity_threshold_profile_active
    ON identity_threshold_profile(active)
    WHERE active = 1;

ALTER TABLE auto_cluster_member RENAME TO auto_cluster_member_old;
ALTER TABLE auto_cluster RENAME TO auto_cluster_old;
ALTER TABLE auto_cluster_batch RENAME TO auto_cluster_batch_old;

CREATE TABLE auto_cluster_batch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_key TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    batch_type TEXT NOT NULL CHECK (batch_type IN ('bootstrap', 'incremental')),
    threshold_profile_id INTEGER,
    scan_session_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (threshold_profile_id) REFERENCES identity_threshold_profile(id),
    FOREIGN KEY (scan_session_id) REFERENCES scan_session(id)
);

CREATE TABLE auto_cluster (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    representative_observation_id INTEGER,
    cluster_status TEXT NOT NULL CHECK (cluster_status IN ('materialized', 'review_pending', 'review_resolved', 'ignored', 'discarded')),
    resolved_person_id INTEGER,
    diagnostic_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (batch_id) REFERENCES auto_cluster_batch(id) ON DELETE CASCADE,
    FOREIGN KEY (representative_observation_id) REFERENCES face_observation(id),
    FOREIGN KEY (resolved_person_id) REFERENCES person(id)
);

CREATE TABLE auto_cluster_member (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id INTEGER NOT NULL,
    face_observation_id INTEGER NOT NULL,
    membership_score REAL,
    quality_score_snapshot REAL,
    is_seed_candidate INTEGER NOT NULL DEFAULT 0 CHECK (is_seed_candidate IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (cluster_id) REFERENCES auto_cluster(id) ON DELETE CASCADE,
    FOREIGN KEY (face_observation_id) REFERENCES face_observation(id) ON DELETE CASCADE,
    UNIQUE (cluster_id, face_observation_id)
);

INSERT INTO auto_cluster_batch(id, model_key, algorithm_version, batch_type, threshold_profile_id, scan_session_id, created_at)
SELECT id, model_key, algorithm_version, 'bootstrap', NULL, NULL, created_at
FROM auto_cluster_batch_old;

INSERT INTO auto_cluster(id, batch_id, representative_observation_id, cluster_status, resolved_person_id, diagnostic_json, created_at)
SELECT id, batch_id, representative_observation_id, 'discarded', NULL, '{}', created_at
FROM auto_cluster_old;

INSERT INTO auto_cluster_member(id, cluster_id, face_observation_id, membership_score, quality_score_snapshot, is_seed_candidate, created_at)
SELECT id, cluster_id, face_observation_id, membership_score, NULL, 0, created_at
FROM auto_cluster_member_old;

DROP TABLE auto_cluster_member_old;
DROP TABLE auto_cluster_old;
DROP TABLE auto_cluster_batch_old;

ALTER TABLE person
    ADD COLUMN origin_cluster_id INTEGER REFERENCES auto_cluster(id);

CREATE TABLE person_trusted_sample (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL,
    face_observation_id INTEGER NOT NULL,
    trust_source TEXT NOT NULL CHECK (trust_source IN ('bootstrap_seed', 'manual_confirm')),
    trust_score REAL NOT NULL CHECK (trust_score >= 0.0 AND trust_score <= 1.0),
    quality_score_snapshot REAL NOT NULL,
    threshold_profile_id INTEGER NOT NULL,
    source_review_id INTEGER,
    source_auto_cluster_id INTEGER,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (person_id) REFERENCES person(id),
    FOREIGN KEY (face_observation_id) REFERENCES face_observation(id),
    FOREIGN KEY (threshold_profile_id) REFERENCES identity_threshold_profile(id),
    FOREIGN KEY (source_review_id) REFERENCES review_item(id),
    FOREIGN KEY (source_auto_cluster_id) REFERENCES auto_cluster(id)
);

CREATE UNIQUE INDEX uq_person_trusted_sample_active_observation
    ON person_trusted_sample(face_observation_id)
    WHERE active = 1;

ALTER TABLE person_face_exclusion RENAME TO person_face_exclusion_old;
ALTER TABLE person_face_assignment RENAME TO person_face_assignment_old;
DROP INDEX IF EXISTS uq_person_face_assignment_active_observation;

CREATE TABLE person_face_assignment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL,
    face_observation_id INTEGER NOT NULL,
    assignment_source TEXT NOT NULL CHECK (assignment_source IN ('bootstrap', 'auto', 'manual', 'merge')),
    diagnostic_json TEXT NOT NULL DEFAULT '{}',
    threshold_profile_id INTEGER,
    locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
    confirmed_at TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (person_id) REFERENCES person(id),
    FOREIGN KEY (face_observation_id) REFERENCES face_observation(id),
    FOREIGN KEY (threshold_profile_id) REFERENCES identity_threshold_profile(id)
);

CREATE UNIQUE INDEX uq_person_face_assignment_active_observation
    ON person_face_assignment(face_observation_id)
    WHERE active = 1;

INSERT INTO person_face_assignment(
    id,
    person_id,
    face_observation_id,
    assignment_source,
    diagnostic_json,
    threshold_profile_id,
    locked,
    confirmed_at,
    active,
    created_at,
    updated_at
)
SELECT
    id,
    person_id,
    face_observation_id,
    CASE WHEN assignment_source = 'split' THEN 'manual' ELSE assignment_source END,
    '{}',
    NULL,
    locked,
    confirmed_at,
    active,
    created_at,
    updated_at
FROM person_face_assignment_old;

CREATE TABLE person_face_exclusion (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL,
    face_observation_id INTEGER NOT NULL,
    assignment_id INTEGER,
    reason TEXT NOT NULL DEFAULT 'manual_exclude',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (person_id) REFERENCES person(id),
    FOREIGN KEY (face_observation_id) REFERENCES face_observation(id),
    FOREIGN KEY (assignment_id) REFERENCES person_face_assignment(id),
    UNIQUE (person_id, face_observation_id)
);

INSERT INTO person_face_exclusion(
    id,
    person_id,
    face_observation_id,
    assignment_id,
    reason,
    active,
    created_at,
    updated_at
)
SELECT
    id,
    person_id,
    face_observation_id,
    assignment_id,
    reason,
    active,
    created_at,
    updated_at
FROM person_face_exclusion_old;

DROP TABLE person_face_exclusion_old;
DROP TABLE person_face_assignment_old;

CREATE INDEX idx_person_face_exclusion_observation_active
    ON person_face_exclusion(face_observation_id, active);

CREATE INDEX idx_person_face_exclusion_person_active
    ON person_face_exclusion(person_id, active);
