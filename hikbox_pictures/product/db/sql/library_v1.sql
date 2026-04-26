CREATE TABLE schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1');

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
  file_fingerprint TEXT NOT NULL,
  live_photo_mov_path TEXT,
  processing_status TEXT NOT NULL CHECK (processing_status IN ('pending', 'succeeded', 'failed')),
  failure_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX idx_assets_source_id ON assets(source_id);
CREATE INDEX idx_assets_processing_status ON assets(processing_status);

CREATE TABLE scan_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_fingerprint TEXT NOT NULL UNIQUE,
  batch_size INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
  command TEXT NOT NULL,
  total_batches INTEGER NOT NULL DEFAULT 0,
  completed_batches INTEGER NOT NULL DEFAULT 0,
  failed_assets INTEGER NOT NULL DEFAULT 0,
  success_faces INTEGER NOT NULL DEFAULT 0,
  artifact_files INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE TABLE scan_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES scan_sessions(id),
  batch_index INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed')),
  item_count INTEGER NOT NULL,
  started_at TEXT,
  completed_at TEXT,
  failure_message TEXT,
  worker_pid INTEGER,
  UNIQUE(session_id, batch_index)
);

CREATE INDEX idx_scan_batches_session_status ON scan_batches(session_id, status, batch_index);

CREATE TABLE scan_batch_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL REFERENCES scan_batches(id),
  item_index INTEGER NOT NULL,
  source_id INTEGER NOT NULL REFERENCES library_sources(id),
  absolute_path TEXT NOT NULL,
  asset_id INTEGER REFERENCES assets(id),
  status TEXT NOT NULL CHECK (status IN ('pending', 'succeeded', 'failed')),
  failure_reason TEXT,
  face_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE(batch_id, item_index),
  UNIQUE(batch_id, absolute_path)
);

CREATE INDEX idx_scan_batch_items_batch_id ON scan_batch_items(batch_id, item_index);

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
  context_path TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(asset_id, face_index)
);

CREATE INDEX idx_face_observations_asset_id ON face_observations(asset_id, face_index);

CREATE TABLE person (
  id TEXT PRIMARY KEY,
  display_name TEXT,
  is_named INTEGER NOT NULL DEFAULT 0 CHECK (is_named IN (0, 1)),
  status TEXT NOT NULL CHECK (status IN ('active', 'inactive')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX idx_person_status ON person(status, is_named, created_at);
CREATE UNIQUE INDEX idx_person_unique_active_display_name
  ON person(display_name)
  WHERE status = 'active' AND is_named = 1;

CREATE TABLE person_name_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id TEXT NOT NULL REFERENCES person(id),
  event_type TEXT NOT NULL CHECK (event_type IN ('person_named', 'person_renamed')),
  old_display_name TEXT,
  new_display_name TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX idx_person_name_events_person_id
  ON person_name_events(person_id, id);

CREATE TABLE assignment_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_session_id INTEGER NOT NULL REFERENCES scan_sessions(id),
  algorithm_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
  param_snapshot_json TEXT NOT NULL,
  candidate_count INTEGER NOT NULL DEFAULT 0,
  assigned_count INTEGER NOT NULL DEFAULT 0,
  new_person_count INTEGER NOT NULL DEFAULT 0,
  deferred_count INTEGER NOT NULL DEFAULT 0,
  skipped_count INTEGER NOT NULL DEFAULT 0,
  failed_count INTEGER NOT NULL DEFAULT 0,
  orphan_embedding_count INTEGER NOT NULL DEFAULT 0,
  orphan_embedding_keys_json TEXT NOT NULL DEFAULT '[]',
  failure_reason TEXT,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE INDEX idx_assignment_runs_session_id ON assignment_runs(scan_session_id, id);

CREATE TABLE person_face_assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id TEXT NOT NULL REFERENCES person(id),
  face_observation_id INTEGER NOT NULL REFERENCES face_observations(id),
  assignment_run_id INTEGER NOT NULL REFERENCES assignment_runs(id),
  assignment_source TEXT NOT NULL CHECK (assignment_source IN ('online_v6')),
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  evidence_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX idx_person_face_assignments_person_id
  ON person_face_assignments(person_id, active, face_observation_id);
CREATE INDEX idx_person_face_assignments_face_id
  ON person_face_assignments(face_observation_id, active, assignment_run_id);
CREATE UNIQUE INDEX idx_person_face_assignments_unique_active_face
  ON person_face_assignments(face_observation_id)
  WHERE active = 1;
