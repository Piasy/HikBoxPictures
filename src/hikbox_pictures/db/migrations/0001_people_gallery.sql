PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS library_source (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    root_path TEXT NOT NULL,
    root_fingerprint TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_library_source_root_path_active
    ON library_source(root_path)
    WHERE active = 1;

CREATE TABLE IF NOT EXISTS scan_session (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL CHECK (mode IN ('initial', 'incremental', 'resume')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'paused', 'interrupted', 'completed', 'failed', 'abandoned')),
    resume_from_session_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    stopped_at TEXT,
    finished_at TEXT,
    FOREIGN KEY (resume_from_session_id) REFERENCES scan_session(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_scan_session_running_singleton
    ON scan_session(status)
    WHERE status = 'running';

CREATE TABLE IF NOT EXISTS scan_session_source (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_session_id INTEGER NOT NULL,
    library_source_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'paused', 'interrupted', 'completed', 'failed', 'abandoned')),
    cursor_json TEXT,
    discovered_count INTEGER NOT NULL DEFAULT 0,
    metadata_done_count INTEGER NOT NULL DEFAULT 0,
    faces_done_count INTEGER NOT NULL DEFAULT 0,
    embeddings_done_count INTEGER NOT NULL DEFAULT 0,
    assignment_done_count INTEGER NOT NULL DEFAULT 0,
    last_checkpoint_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (scan_session_id) REFERENCES scan_session(id) ON DELETE CASCADE,
    FOREIGN KEY (library_source_id) REFERENCES library_source(id),
    UNIQUE (scan_session_id, library_source_id)
);

CREATE TABLE IF NOT EXISTS scan_checkpoint (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_session_source_id INTEGER NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('discover', 'metadata', 'faces', 'embeddings', 'assignment')),
    cursor_json TEXT,
    pending_asset_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (scan_session_source_id) REFERENCES scan_session_source(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS photo_asset (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    library_source_id INTEGER NOT NULL,
    primary_path TEXT NOT NULL,
    primary_fingerprint TEXT,
    file_size INTEGER,
    mtime REAL,
    capture_datetime TEXT,
    capture_month TEXT,
    width INTEGER,
    height INTEGER,
    is_heic INTEGER NOT NULL DEFAULT 0 CHECK (is_heic IN (0, 1)),
    live_mov_path TEXT,
    live_mov_fingerprint TEXT,
    processing_status TEXT NOT NULL DEFAULT 'discovered' CHECK (processing_status IN ('discovered', 'metadata_done', 'faces_done', 'embeddings_done', 'assignment_done', 'failed')),
    last_processed_session_id INTEGER,
    last_error TEXT,
    indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (library_source_id) REFERENCES library_source(id),
    FOREIGN KEY (last_processed_session_id) REFERENCES scan_session(id),
    UNIQUE (library_source_id, primary_path)
);

CREATE TABLE IF NOT EXISTS face_observation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_asset_id INTEGER NOT NULL,
    bbox_top REAL NOT NULL,
    bbox_right REAL NOT NULL,
    bbox_bottom REAL NOT NULL,
    bbox_left REAL NOT NULL,
    face_area_ratio REAL,
    sharpness_score REAL,
    pose_score REAL,
    quality_score REAL,
    crop_path TEXT,
    detector_key TEXT,
    detector_version TEXT,
    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (photo_asset_id) REFERENCES photo_asset(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS face_embedding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    face_observation_id INTEGER NOT NULL,
    feature_type TEXT NOT NULL DEFAULT 'face' CHECK (feature_type IN ('face')),
    model_key TEXT,
    dimension INTEGER,
    vector_blob BLOB,
    normalized INTEGER NOT NULL DEFAULT 1 CHECK (normalized IN (0, 1)),
    generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (face_observation_id) REFERENCES face_observation(id) ON DELETE CASCADE,
    UNIQUE (face_observation_id, feature_type)
);

CREATE TABLE IF NOT EXISTS auto_cluster_batch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_key TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS auto_cluster (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    confidence REAL,
    representative_observation_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (batch_id) REFERENCES auto_cluster_batch(id) ON DELETE CASCADE,
    FOREIGN KEY (representative_observation_id) REFERENCES face_observation(id)
);

CREATE TABLE IF NOT EXISTS auto_cluster_member (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id INTEGER NOT NULL,
    face_observation_id INTEGER NOT NULL,
    membership_score REAL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (cluster_id) REFERENCES auto_cluster(id) ON DELETE CASCADE,
    FOREIGN KEY (face_observation_id) REFERENCES face_observation(id) ON DELETE CASCADE,
    UNIQUE (cluster_id, face_observation_id)
);

CREATE TABLE IF NOT EXISTS person (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT NOT NULL,
    cover_observation_id INTEGER,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'merged', 'ignored')),
    notes TEXT,
    confirmed INTEGER NOT NULL DEFAULT 0 CHECK (confirmed IN (0, 1)),
    ignored INTEGER NOT NULL DEFAULT 0 CHECK (ignored IN (0, 1)),
    merged_into_person_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (cover_observation_id) REFERENCES face_observation(id),
    FOREIGN KEY (merged_into_person_id) REFERENCES person(id)
);

CREATE TABLE IF NOT EXISTS person_face_assignment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL,
    face_observation_id INTEGER NOT NULL,
    assignment_source TEXT NOT NULL CHECK (assignment_source IN ('auto', 'manual', 'merge', 'split')),
    confidence REAL,
    locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
    confirmed_at TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (person_id) REFERENCES person(id),
    FOREIGN KEY (face_observation_id) REFERENCES face_observation(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_person_face_assignment_active_observation
    ON person_face_assignment(face_observation_id)
    WHERE active = 1;

CREATE TABLE IF NOT EXISTS person_prototype (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL,
    prototype_type TEXT NOT NULL CHECK (prototype_type IN ('centroid', 'medoid', 'exemplar')),
    source_observation_id INTEGER,
    model_key TEXT,
    vector_blob BLOB,
    quality_score REAL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (person_id) REFERENCES person(id) ON DELETE CASCADE,
    FOREIGN KEY (source_observation_id) REFERENCES face_observation(id)
);

CREATE TABLE IF NOT EXISTS review_item (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_type TEXT NOT NULL CHECK (review_type IN ('new_person', 'possible_merge', 'possible_split', 'low_confidence_assignment')),
    primary_person_id INTEGER,
    secondary_person_id INTEGER,
    face_observation_id INTEGER,
    payload_json TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'dismissed')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT,
    FOREIGN KEY (primary_person_id) REFERENCES person(id),
    FOREIGN KEY (secondary_person_id) REFERENCES person(id),
    FOREIGN KEY (face_observation_id) REFERENCES face_observation(id)
);

CREATE TABLE IF NOT EXISTS export_template (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    output_root TEXT NOT NULL,
    include_group INTEGER NOT NULL DEFAULT 1 CHECK (include_group IN (0, 1)),
    export_live_mov INTEGER NOT NULL DEFAULT 0 CHECK (export_live_mov IN (0, 1)),
    start_datetime TEXT,
    end_datetime TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS export_template_person (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL,
    person_id INTEGER NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (template_id) REFERENCES export_template(id) ON DELETE CASCADE,
    FOREIGN KEY (person_id) REFERENCES person(id),
    UNIQUE (template_id, person_id),
    UNIQUE (template_id, position)
);

CREATE TABLE IF NOT EXISTS export_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL,
    spec_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    matched_only_count INTEGER NOT NULL DEFAULT 0,
    matched_group_count INTEGER NOT NULL DEFAULT 0,
    exported_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (template_id) REFERENCES export_template(id)
);

CREATE TABLE IF NOT EXISTS export_delivery (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL,
    spec_hash TEXT NOT NULL,
    photo_asset_id INTEGER NOT NULL,
    asset_variant TEXT NOT NULL CHECK (asset_variant IN ('primary', 'live_mov')),
    bucket TEXT NOT NULL CHECK (bucket IN ('only', 'group')),
    target_path TEXT NOT NULL,
    source_fingerprint TEXT,
    status TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'pending', 'skipped', 'failed', 'stale')),
    last_exported_at TEXT,
    last_verified_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (template_id) REFERENCES export_template(id) ON DELETE CASCADE,
    FOREIGN KEY (photo_asset_id) REFERENCES photo_asset(id) ON DELETE CASCADE,
    UNIQUE (template_id, spec_hash, photo_asset_id, asset_variant)
);

CREATE TABLE IF NOT EXISTS ops_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    level TEXT NOT NULL CHECK (level IN ('debug', 'info', 'warning', 'error')),
    component TEXT NOT NULL,
    event_type TEXT NOT NULL,
    run_kind TEXT,
    run_id TEXT,
    scan_session_id INTEGER,
    scan_session_source_id INTEGER,
    export_run_id INTEGER,
    photo_asset_id INTEGER,
    face_observation_id INTEGER,
    template_id INTEGER,
    message TEXT,
    detail_json TEXT,
    traceback_text TEXT,
    dedupe_key TEXT,
    repeat_count INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (scan_session_id) REFERENCES scan_session(id),
    FOREIGN KEY (scan_session_source_id) REFERENCES scan_session_source(id),
    FOREIGN KEY (export_run_id) REFERENCES export_run(id),
    FOREIGN KEY (photo_asset_id) REFERENCES photo_asset(id),
    FOREIGN KEY (face_observation_id) REFERENCES face_observation(id),
    FOREIGN KEY (template_id) REFERENCES export_template(id)
);

CREATE INDEX IF NOT EXISTS idx_ops_event_run
    ON ops_event(run_kind, run_id, occurred_at);

CREATE INDEX IF NOT EXISTS idx_ops_event_filters
    ON ops_event(level, event_type, occurred_at);
