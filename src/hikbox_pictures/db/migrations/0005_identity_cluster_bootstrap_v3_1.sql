PRAGMA foreign_keys = ON;
PRAGMA defer_foreign_keys = ON;

CREATE TABLE identity_observation_profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_name TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    embedding_feature_type TEXT NOT NULL,
    embedding_model_key TEXT NOT NULL,
    embedding_distance_metric TEXT NOT NULL,
    embedding_schema_version TEXT NOT NULL,
    quality_formula_version TEXT NOT NULL,
    quality_area_weight REAL NOT NULL,
    quality_sharpness_weight REAL NOT NULL,
    quality_pose_weight REAL NOT NULL,
    core_quality_threshold REAL NOT NULL,
    attachment_quality_threshold REAL NOT NULL,
    exact_duplicate_distance_threshold REAL NOT NULL,
    same_photo_keep_best TEXT NOT NULL,
    burst_window_seconds INTEGER NOT NULL,
    burst_duplicate_distance_threshold REAL NOT NULL,
    pool_exclusion_rules_version TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
    activated_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX uq_identity_observation_profile_active
ON identity_observation_profile(active)
WHERE active = 1;

CREATE TABLE identity_observation_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_profile_id INTEGER NOT NULL,
    dataset_hash TEXT NOT NULL,
    candidate_policy_hash TEXT NOT NULL,
    max_knn_supported INTEGER NOT NULL,
    algorithm_version TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'created' CHECK (status IN ('created', 'running', 'succeeded', 'failed', 'cancelled')),
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (observation_profile_id) REFERENCES identity_observation_profile(id)
);

CREATE INDEX idx_identity_observation_snapshot_profile_dataset
ON identity_observation_snapshot(observation_profile_id, dataset_hash, candidate_policy_hash, id DESC);

CREATE TABLE identity_observation_pool_entry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL,
    observation_id INTEGER NOT NULL,
    pool_kind TEXT NOT NULL CHECK (pool_kind IN ('core_discovery', 'attachment', 'excluded')),
    quality_score_snapshot REAL,
    dedup_group_key TEXT,
    representative_observation_id INTEGER,
    excluded_reason TEXT,
    diagnostic_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (snapshot_id) REFERENCES identity_observation_snapshot(id) ON DELETE CASCADE,
    FOREIGN KEY (observation_id) REFERENCES face_observation(id) ON DELETE CASCADE,
    FOREIGN KEY (representative_observation_id) REFERENCES face_observation(id),
    UNIQUE (snapshot_id, observation_id)
);

CREATE INDEX idx_identity_observation_pool_entry_snapshot_kind
ON identity_observation_pool_entry(snapshot_id, pool_kind, excluded_reason);

CREATE TABLE identity_cluster_profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_name TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    discovery_knn_k INTEGER NOT NULL,
    density_min_samples INTEGER NOT NULL,
    raw_cluster_min_size INTEGER NOT NULL,
    raw_cluster_min_distinct_photo_count INTEGER NOT NULL,
    intra_photo_conflict_policy_version TEXT NOT NULL,
    anchor_core_min_support_ratio REAL NOT NULL,
    anchor_core_radius_quantile REAL NOT NULL,
    core_min_support_ratio REAL NOT NULL,
    boundary_min_support_ratio REAL NOT NULL,
    boundary_radius_multiplier REAL NOT NULL,
    split_min_component_size INTEGER NOT NULL,
    split_min_medoid_gap REAL NOT NULL,
    existence_min_retained_count INTEGER NOT NULL,
    existence_min_anchor_core_count INTEGER NOT NULL,
    existence_min_distinct_photo_count INTEGER NOT NULL,
    existence_min_support_ratio_p50 REAL NOT NULL,
    existence_max_intra_photo_conflict_ratio REAL NOT NULL,
    attachment_max_distance REAL NOT NULL,
    attachment_candidate_knn_k INTEGER NOT NULL,
    attachment_min_support_ratio REAL NOT NULL,
    attachment_min_separation_gap REAL NOT NULL,
    materialize_min_anchor_core_count INTEGER NOT NULL,
    materialize_min_distinct_photo_count INTEGER NOT NULL,
    materialize_max_compactness_p90 REAL NOT NULL,
    materialize_min_separation_gap REAL NOT NULL,
    materialize_max_boundary_ratio REAL NOT NULL,
    trusted_seed_min_quality REAL NOT NULL,
    trusted_seed_min_count INTEGER NOT NULL,
    trusted_seed_max_count INTEGER NOT NULL,
    trusted_seed_allow_boundary INTEGER NOT NULL CHECK (trusted_seed_allow_boundary IN (0, 1)),
    active INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
    activated_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX uq_identity_cluster_profile_active
ON identity_cluster_profile(active)
WHERE active = 1;

CREATE TABLE identity_cluster_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_snapshot_id INTEGER NOT NULL,
    cluster_profile_id INTEGER NOT NULL,
    algorithm_version TEXT NOT NULL,
    run_status TEXT NOT NULL CHECK (run_status IN ('created', 'running', 'succeeded', 'failed', 'cancelled')),
    summary_json TEXT NOT NULL DEFAULT '{}',
    failure_json TEXT NOT NULL DEFAULT '{}',
    is_review_target INTEGER NOT NULL DEFAULT 0 CHECK (is_review_target IN (0, 1)),
    review_selected_at TEXT,
    is_materialization_owner INTEGER NOT NULL DEFAULT 0 CHECK (is_materialization_owner IN (0, 1)),
    supersedes_run_id INTEGER,
    started_at TEXT,
    finished_at TEXT,
    activated_at TEXT,
    prepared_artifact_root TEXT,
    prepared_ann_manifest_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (observation_snapshot_id) REFERENCES identity_observation_snapshot(id),
    FOREIGN KEY (cluster_profile_id) REFERENCES identity_cluster_profile(id),
    FOREIGN KEY (supersedes_run_id) REFERENCES identity_cluster_run(id)
);

CREATE UNIQUE INDEX ux_identity_cluster_run_single_review_target
ON identity_cluster_run(is_review_target)
WHERE is_review_target = 1;

CREATE UNIQUE INDEX ux_identity_cluster_run_single_materialization_owner
ON identity_cluster_run(is_materialization_owner)
WHERE is_materialization_owner = 1;

CREATE INDEX idx_identity_cluster_run_status
ON identity_cluster_run(run_status, id DESC);

CREATE TABLE identity_cluster (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    cluster_stage TEXT NOT NULL CHECK (cluster_stage IN ('raw', 'cleaned', 'final')),
    cluster_state TEXT NOT NULL CHECK (cluster_state IN ('active', 'discarded')),
    member_count INTEGER NOT NULL DEFAULT 0,
    retained_member_count INTEGER NOT NULL DEFAULT 0,
    anchor_core_count INTEGER NOT NULL DEFAULT 0,
    core_count INTEGER NOT NULL DEFAULT 0,
    boundary_count INTEGER NOT NULL DEFAULT 0,
    attachment_count INTEGER NOT NULL DEFAULT 0,
    excluded_count INTEGER NOT NULL DEFAULT 0,
    distinct_photo_count INTEGER NOT NULL DEFAULT 0,
    compactness_p50 REAL,
    compactness_p90 REAL,
    support_ratio_p10 REAL,
    support_ratio_p50 REAL,
    intra_photo_conflict_ratio REAL,
    nearest_cluster_distance REAL,
    separation_gap REAL,
    boundary_ratio REAL,
    discard_reason_code TEXT,
    representative_observation_id INTEGER,
    summary_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (run_id) REFERENCES identity_cluster_run(id) ON DELETE CASCADE,
    FOREIGN KEY (representative_observation_id) REFERENCES face_observation(id)
);

CREATE INDEX idx_identity_cluster_run_stage
ON identity_cluster(run_id, cluster_stage, cluster_state);

CREATE TABLE identity_cluster_lineage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_cluster_id INTEGER NOT NULL,
    child_cluster_id INTEGER NOT NULL,
    relation_kind TEXT NOT NULL,
    reason_code TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (parent_cluster_id) REFERENCES identity_cluster(id) ON DELETE CASCADE,
    FOREIGN KEY (child_cluster_id) REFERENCES identity_cluster(id) ON DELETE CASCADE,
    UNIQUE (parent_cluster_id, child_cluster_id, relation_kind)
);

CREATE TABLE identity_cluster_member (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id INTEGER NOT NULL,
    observation_id INTEGER NOT NULL,
    source_pool_kind TEXT NOT NULL CHECK (source_pool_kind IN ('core_discovery', 'attachment', 'excluded')),
    quality_score_snapshot REAL,
    member_role TEXT NOT NULL CHECK (member_role IN ('anchor_core', 'core', 'boundary', 'attachment', 'excluded')),
    decision_status TEXT NOT NULL CHECK (decision_status IN ('retained', 'rejected', 'deferred')),
    distance_to_medoid REAL,
    density_radius REAL,
    support_ratio REAL,
    attachment_support_ratio REAL,
    nearest_competing_cluster_distance REAL,
    separation_gap REAL,
    decision_reason_code TEXT,
    is_trusted_seed_candidate INTEGER NOT NULL DEFAULT 0 CHECK (is_trusted_seed_candidate IN (0, 1)),
    is_selected_trusted_seed INTEGER NOT NULL DEFAULT 0 CHECK (is_selected_trusted_seed IN (0, 1)),
    seed_rank INTEGER,
    is_representative INTEGER NOT NULL DEFAULT 0 CHECK (is_representative IN (0, 1)),
    diagnostic_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (cluster_id) REFERENCES identity_cluster(id) ON DELETE CASCADE,
    FOREIGN KEY (observation_id) REFERENCES face_observation(id) ON DELETE CASCADE,
    UNIQUE (cluster_id, observation_id)
);

CREATE INDEX idx_identity_cluster_member_cluster_role
ON identity_cluster_member(cluster_id, member_role, decision_status);

CREATE TABLE identity_cluster_resolution (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id INTEGER NOT NULL,
    resolution_state TEXT NOT NULL CHECK (resolution_state IN ('materialized', 'review_pending', 'discarded', 'unresolved')),
    resolution_reason TEXT,
    publish_state TEXT NOT NULL DEFAULT 'not_applicable' CHECK (publish_state IN ('not_applicable', 'prepared', 'published', 'publish_failed')),
    publish_failure_reason TEXT,
    person_id INTEGER,
    source_run_id INTEGER NOT NULL,
    trusted_seed_count INTEGER NOT NULL DEFAULT 0,
    trusted_seed_candidate_count INTEGER NOT NULL DEFAULT 0,
    trusted_seed_reject_distribution_json TEXT NOT NULL DEFAULT '{}',
    prepared_bundle_manifest_json TEXT NOT NULL DEFAULT '{}',
    prototype_status TEXT NOT NULL DEFAULT 'not_applicable' CHECK (prototype_status IN ('not_applicable', 'prepared', 'published', 'failed')),
    ann_status TEXT NOT NULL DEFAULT 'not_applicable' CHECK (ann_status IN ('not_applicable', 'prepared', 'published', 'failed')),
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (cluster_id) REFERENCES identity_cluster(id) ON DELETE CASCADE,
    FOREIGN KEY (person_id) REFERENCES person(id),
    FOREIGN KEY (source_run_id) REFERENCES identity_cluster_run(id),
    UNIQUE (cluster_id)
);

-- 回填 observation/cluster profile（优先 active，缺失则回退到最新）。
INSERT INTO identity_observation_profile(
    profile_name,
    profile_version,
    embedding_feature_type,
    embedding_model_key,
    embedding_distance_metric,
    embedding_schema_version,
    quality_formula_version,
    quality_area_weight,
    quality_sharpness_weight,
    quality_pose_weight,
    core_quality_threshold,
    attachment_quality_threshold,
    exact_duplicate_distance_threshold,
    same_photo_keep_best,
    burst_window_seconds,
    burst_duplicate_distance_threshold,
    pool_exclusion_rules_version,
    active,
    activated_at
)
SELECT
    profile_name,
    profile_version,
    embedding_feature_type,
    embedding_model_key,
    embedding_distance_metric,
    embedding_schema_version,
    quality_formula_version,
    quality_area_weight,
    quality_sharpness_weight,
    quality_pose_weight,
    high_quality_threshold,
    low_quality_threshold,
    0.005,
    'quality_then_observation_id',
    burst_time_window_seconds,
    0.012,
    'pool_exclusion.v1',
    active,
    activated_at
FROM identity_threshold_profile
ORDER BY active DESC, id DESC
LIMIT 1;

INSERT INTO identity_observation_profile(
    profile_name,
    profile_version,
    embedding_feature_type,
    embedding_model_key,
    embedding_distance_metric,
    embedding_schema_version,
    quality_formula_version,
    quality_area_weight,
    quality_sharpness_weight,
    quality_pose_weight,
    core_quality_threshold,
    attachment_quality_threshold,
    exact_duplicate_distance_threshold,
    same_photo_keep_best,
    burst_window_seconds,
    burst_duplicate_distance_threshold,
    pool_exclusion_rules_version,
    active,
    activated_at
)
SELECT
    'legacy-fallback-observation',
    'v3_1.default',
    'face',
    'insightface',
    'cosine',
    'face.embedding.v1',
    'quality.v1',
    0.40,
    0.40,
    0.20,
    0.55,
    0.35,
    0.005,
    'quality_then_observation_id',
    30,
    0.012,
    'pool_exclusion.v1',
    1,
    CURRENT_TIMESTAMP
WHERE NOT EXISTS (SELECT 1 FROM identity_observation_profile);

INSERT INTO identity_cluster_profile(
    profile_name,
    profile_version,
    discovery_knn_k,
    density_min_samples,
    raw_cluster_min_size,
    raw_cluster_min_distinct_photo_count,
    intra_photo_conflict_policy_version,
    anchor_core_min_support_ratio,
    anchor_core_radius_quantile,
    core_min_support_ratio,
    boundary_min_support_ratio,
    boundary_radius_multiplier,
    split_min_component_size,
    split_min_medoid_gap,
    existence_min_retained_count,
    existence_min_anchor_core_count,
    existence_min_distinct_photo_count,
    existence_min_support_ratio_p50,
    existence_max_intra_photo_conflict_ratio,
    attachment_max_distance,
    attachment_candidate_knn_k,
    attachment_min_support_ratio,
    attachment_min_separation_gap,
    materialize_min_anchor_core_count,
    materialize_min_distinct_photo_count,
    materialize_max_compactness_p90,
    materialize_min_separation_gap,
    materialize_max_boundary_ratio,
    trusted_seed_min_quality,
    trusted_seed_min_count,
    trusted_seed_max_count,
    trusted_seed_allow_boundary,
    active,
    activated_at
)
SELECT
    profile_name || '-cluster',
    profile_version || '.cluster.v3_1',
    24,
    4,
    3,
    2,
    'same_photo_conflict.v1',
    0.55,
    0.80,
    0.45,
    0.30,
    1.15,
    2,
    0.025,
    3,
    1,
    2,
    0.35,
    0.40,
    0.085,
    16,
    0.30,
    0.015,
    1,
    2,
    0.22,
    0.02,
    0.45,
    low_quality_threshold,
    1,
    6,
    0,
    active,
    activated_at
FROM identity_threshold_profile
ORDER BY active DESC, id DESC
LIMIT 1;

INSERT INTO identity_cluster_profile(
    profile_name,
    profile_version,
    discovery_knn_k,
    density_min_samples,
    raw_cluster_min_size,
    raw_cluster_min_distinct_photo_count,
    intra_photo_conflict_policy_version,
    anchor_core_min_support_ratio,
    anchor_core_radius_quantile,
    core_min_support_ratio,
    boundary_min_support_ratio,
    boundary_radius_multiplier,
    split_min_component_size,
    split_min_medoid_gap,
    existence_min_retained_count,
    existence_min_anchor_core_count,
    existence_min_distinct_photo_count,
    existence_min_support_ratio_p50,
    existence_max_intra_photo_conflict_ratio,
    attachment_max_distance,
    attachment_candidate_knn_k,
    attachment_min_support_ratio,
    attachment_min_separation_gap,
    materialize_min_anchor_core_count,
    materialize_min_distinct_photo_count,
    materialize_max_compactness_p90,
    materialize_min_separation_gap,
    materialize_max_boundary_ratio,
    trusted_seed_min_quality,
    trusted_seed_min_count,
    trusted_seed_max_count,
    trusted_seed_allow_boundary,
    active,
    activated_at
)
SELECT
    'legacy-fallback-cluster',
    'v3_1.default.cluster',
    24,
    4,
    3,
    2,
    'same_photo_conflict.v1',
    0.55,
    0.80,
    0.45,
    0.30,
    1.15,
    2,
    0.025,
    3,
    1,
    2,
    0.35,
    0.40,
    0.085,
    16,
    0.30,
    0.015,
    1,
    2,
    0.22,
    0.02,
    0.45,
    0.35,
    1,
    6,
    0,
    1,
    CURRENT_TIMESTAMP
WHERE NOT EXISTS (SELECT 1 FROM identity_cluster_profile);

-- migration 专用 legacy snapshot/run，供历史来源回填使用。
INSERT INTO identity_observation_snapshot(
    observation_profile_id,
    dataset_hash,
    candidate_policy_hash,
    max_knn_supported,
    algorithm_version,
    summary_json,
    status,
    started_at,
    finished_at
)
SELECT
    p.id,
    'legacy-migration-dataset-hash',
    'legacy-migration-candidate-policy',
    0,
    'identity.observation_snapshot.legacy_migration.v3_to_v3_1',
    '{"legacy_migration": true}',
    'succeeded',
    CURRENT_TIMESTAMP,
    CURRENT_TIMESTAMP
FROM identity_observation_profile AS p
ORDER BY p.active DESC, p.id DESC
LIMIT 1;

INSERT INTO identity_cluster_run(
    observation_snapshot_id,
    cluster_profile_id,
    algorithm_version,
    run_status,
    summary_json,
    failure_json,
    is_review_target,
    is_materialization_owner,
    started_at,
    finished_at
)
SELECT
    (
        SELECT id
        FROM identity_observation_snapshot
        WHERE algorithm_version = 'identity.observation_snapshot.legacy_migration.v3_to_v3_1'
        ORDER BY id DESC
        LIMIT 1
    ),
    (
        SELECT id
        FROM identity_cluster_profile
        ORDER BY active DESC, id DESC
        LIMIT 1
    ),
    'identity.cluster.legacy_migration.v3_to_v3_1',
    'succeeded',
    '{"legacy_migration": true}',
    '{}',
    0,
    0,
    CURRENT_TIMESTAMP,
    CURRENT_TIMESTAMP;

-- legacy auto_cluster -> final identity_cluster（id 对齐）。
INSERT INTO identity_cluster(
    id,
    run_id,
    cluster_stage,
    cluster_state,
    representative_observation_id,
    summary_json
)
SELECT
    ac.id,
    (
        SELECT id
        FROM identity_cluster_run
        WHERE algorithm_version = 'identity.cluster.legacy_migration.v3_to_v3_1'
          AND run_status = 'succeeded'
        ORDER BY id DESC
        LIMIT 1
    ),
    'final',
    CASE
        WHEN ac.cluster_status = 'discarded' THEN 'discarded'
        ELSE 'active'
    END,
    ac.representative_observation_id,
    ac.diagnostic_json
FROM auto_cluster AS ac;

-- legacy auto_cluster_member -> identity_cluster_member（保留观测归属与 seed 语义）。
INSERT INTO identity_cluster_member(
    cluster_id,
    observation_id,
    source_pool_kind,
    quality_score_snapshot,
    member_role,
    decision_status,
    support_ratio,
    is_trusted_seed_candidate,
    is_selected_trusted_seed,
    is_representative,
    diagnostic_json
)
SELECT
    acm.cluster_id,
    acm.face_observation_id,
    'core_discovery',
    acm.quality_score_snapshot,
    CASE
        WHEN acm.is_seed_candidate = 1 THEN 'anchor_core'
        ELSE 'core'
    END,
    CASE
        WHEN ac.cluster_status = 'discarded' THEN 'rejected'
        ELSE 'retained'
    END,
    acm.membership_score,
    acm.is_seed_candidate,
    acm.is_seed_candidate,
    CASE
        WHEN ac.representative_observation_id = acm.face_observation_id THEN 1
        ELSE 0
    END,
    ac.diagnostic_json
FROM auto_cluster_member AS acm
JOIN auto_cluster AS ac
  ON ac.id = acm.cluster_id;

-- legacy auto_cluster -> identity_cluster_resolution（保证每个 legacy cluster 均有 resolution 真值）。
INSERT INTO identity_cluster_resolution(
    cluster_id,
    resolution_state,
    resolution_reason,
    publish_state,
    person_id,
    source_run_id,
    trusted_seed_count,
    trusted_seed_candidate_count,
    detail_json
)
SELECT
    ac.id,
    CASE
        WHEN ac.cluster_status = 'materialized' THEN 'materialized'
        WHEN ac.cluster_status = 'discarded' THEN 'discarded'
        ELSE 'unresolved'
    END,
    CASE
        WHEN ac.cluster_status = 'discarded' THEN 'legacy_auto_cluster_discarded'
        ELSE 'legacy_auto_cluster_imported'
    END,
    'not_applicable',
    ac.resolved_person_id,
    (
        SELECT id
        FROM identity_cluster_run
        WHERE algorithm_version = 'identity.cluster.legacy_migration.v3_to_v3_1'
          AND run_status = 'succeeded'
        ORDER BY id DESC
        LIMIT 1
    ),
    (
        SELECT COUNT(*)
        FROM auto_cluster_member AS acm_seed
        WHERE acm_seed.cluster_id = ac.id
          AND acm_seed.is_seed_candidate = 1
    ),
    (
        SELECT COUNT(*)
        FROM auto_cluster_member AS acm_seed
        WHERE acm_seed.cluster_id = ac.id
          AND acm_seed.is_seed_candidate = 1
    ),
    ac.diagnostic_json
FROM auto_cluster AS ac;

-- 回填 legacy cluster 聚合统计，避免 member/resolution 真值与 cluster 计数脱节。
UPDATE identity_cluster
SET member_count = (
        SELECT COUNT(*)
        FROM identity_cluster_member AS m
        WHERE m.cluster_id = identity_cluster.id
    ),
    retained_member_count = (
        SELECT COUNT(*)
        FROM identity_cluster_member AS m
        WHERE m.cluster_id = identity_cluster.id
          AND m.decision_status = 'retained'
    ),
    anchor_core_count = (
        SELECT COUNT(*)
        FROM identity_cluster_member AS m
        WHERE m.cluster_id = identity_cluster.id
          AND m.member_role = 'anchor_core'
          AND m.decision_status = 'retained'
    ),
    core_count = (
        SELECT COUNT(*)
        FROM identity_cluster_member AS m
        WHERE m.cluster_id = identity_cluster.id
          AND m.member_role = 'core'
          AND m.decision_status = 'retained'
    ),
    distinct_photo_count = (
        SELECT COUNT(DISTINCT fo.photo_asset_id)
        FROM identity_cluster_member AS m
        JOIN face_observation AS fo
          ON fo.id = m.observation_id
        WHERE m.cluster_id = identity_cluster.id
          AND m.decision_status = 'retained'
    )
WHERE run_id = (
    SELECT id
    FROM identity_cluster_run
    WHERE algorithm_version = 'identity.cluster.legacy_migration.v3_to_v3_1'
      AND run_status = 'succeeded'
    ORDER BY id DESC
    LIMIT 1
);

ALTER TABLE person_face_assignment ADD COLUMN source_run_id INTEGER REFERENCES identity_cluster_run(id);
ALTER TABLE person_face_assignment ADD COLUMN source_cluster_id INTEGER REFERENCES identity_cluster(id);

ALTER TABLE person_trusted_sample ADD COLUMN source_run_id INTEGER REFERENCES identity_cluster_run(id);
ALTER TABLE person_trusted_sample ADD COLUMN source_cluster_id INTEGER REFERENCES identity_cluster(id);

CREATE TABLE person_cluster_origin (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL,
    origin_cluster_id INTEGER NOT NULL,
    source_run_id INTEGER NOT NULL,
    origin_kind TEXT NOT NULL CHECK (origin_kind IN ('bootstrap_materialize', 'review_materialize', 'merge_adopt')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (person_id) REFERENCES person(id),
    FOREIGN KEY (origin_cluster_id) REFERENCES identity_cluster(id),
    FOREIGN KEY (source_run_id) REFERENCES identity_cluster_run(id)
);

CREATE INDEX idx_person_cluster_origin_person_active
ON person_cluster_origin(person_id, active, source_run_id);

UPDATE person_face_assignment
SET source_cluster_id = (
        SELECT p.origin_cluster_id
        FROM person AS p
        WHERE p.id = person_face_assignment.person_id
    ),
    source_run_id = (
        SELECT id
        FROM identity_cluster_run
        WHERE algorithm_version = 'identity.cluster.legacy_migration.v3_to_v3_1'
          AND run_status = 'succeeded'
        ORDER BY id DESC
        LIMIT 1
    )
WHERE source_cluster_id IS NULL
  AND (
      SELECT p.origin_cluster_id
      FROM person AS p
      WHERE p.id = person_face_assignment.person_id
  ) IS NOT NULL;

UPDATE person_trusted_sample
SET source_cluster_id = COALESCE(source_auto_cluster_id, (
        SELECT p.origin_cluster_id
        FROM person AS p
        WHERE p.id = person_trusted_sample.person_id
    )),
    source_run_id = (
        SELECT id
        FROM identity_cluster_run
        WHERE algorithm_version = 'identity.cluster.legacy_migration.v3_to_v3_1'
          AND run_status = 'succeeded'
        ORDER BY id DESC
        LIMIT 1
    )
WHERE source_cluster_id IS NULL
  AND COALESCE(source_auto_cluster_id, (
      SELECT p.origin_cluster_id
      FROM person AS p
      WHERE p.id = person_trusted_sample.person_id
  )) IS NOT NULL;

INSERT INTO person_cluster_origin(
    person_id,
    origin_cluster_id,
    source_run_id,
    origin_kind,
    active
)
SELECT
    p.id,
    p.origin_cluster_id,
    (
        SELECT id
        FROM identity_cluster_run
        WHERE algorithm_version = 'identity.cluster.legacy_migration.v3_to_v3_1'
          AND run_status = 'succeeded'
        ORDER BY id DESC
        LIMIT 1
    ),
    'bootstrap_materialize',
    CASE
        WHEN p.status = 'active' AND p.ignored = 0 THEN 1
        ELSE 0
    END
FROM person AS p
JOIN auto_cluster AS ac
  ON ac.id = p.origin_cluster_id
WHERE p.origin_cluster_id IS NOT NULL;

ALTER TABLE person DROP COLUMN origin_cluster_id;
