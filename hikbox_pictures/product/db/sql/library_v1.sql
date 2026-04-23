CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

INSERT INTO schema_meta(key, value, updated_at)
VALUES ('schema_version', '1', CURRENT_TIMESTAMP)
ON CONFLICT(key) DO UPDATE SET
  value = excluded.value,
  updated_at = CURRENT_TIMESTAMP;

INSERT INTO schema_meta(key, value, updated_at)
VALUES ('product_schema_name', 'people_gallery_v1', CURRENT_TIMESTAMP)
ON CONFLICT(key) DO UPDATE SET
  value = excluded.value,
  updated_at = CURRENT_TIMESTAMP;

CREATE TABLE IF NOT EXISTS library_source (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  root_path TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
  removed_at TEXT,
  last_discovered_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_library_source_enabled ON library_source(enabled);

CREATE TABLE IF NOT EXISTS scan_session (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_kind TEXT NOT NULL CHECK (run_kind IN ('scan_full', 'scan_incremental', 'scan_resume')),
  status TEXT NOT NULL CHECK (status IN (
    'pending', 'running', 'aborting', 'interrupted', 'completed', 'abandoned', 'failed'
  )),
  triggered_by TEXT NOT NULL CHECK (triggered_by IN ('manual_webui', 'manual_cli')),
  resume_from_session_id INTEGER REFERENCES scan_session(id),
  started_at TEXT,
  finished_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_scan_session_status ON scan_session(status);
CREATE INDEX IF NOT EXISTS idx_scan_session_created_at ON scan_session(created_at);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_scan_session_single_active
ON scan_session((1))
WHERE status IN ('running', 'aborting');

CREATE TABLE IF NOT EXISTS scan_checkpoint (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_session_id INTEGER NOT NULL REFERENCES scan_session(id),
  stage TEXT NOT NULL CHECK (stage IN ('discover', 'metadata', 'detect', 'embed', 'cluster', 'assignment')),
  cursor_json TEXT NOT NULL,
  processed_count INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(scan_session_id, stage)
);

CREATE TABLE IF NOT EXISTS scan_session_source (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_session_id INTEGER NOT NULL REFERENCES scan_session(id),
  library_source_id INTEGER NOT NULL REFERENCES library_source(id),
  stage_status_json TEXT NOT NULL,
  processed_assets INTEGER NOT NULL DEFAULT 0,
  failed_assets INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(scan_session_id, library_source_id)
);

CREATE INDEX IF NOT EXISTS idx_scan_session_source_session
ON scan_session_source(scan_session_id);

CREATE TABLE IF NOT EXISTS photo_asset (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  library_source_id INTEGER NOT NULL REFERENCES library_source(id),
  primary_path TEXT NOT NULL,
  primary_fingerprint TEXT NOT NULL,
  fingerprint_algo TEXT NOT NULL CHECK (fingerprint_algo='sha256'),
  file_size INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL,
  capture_datetime TEXT,
  capture_month TEXT,
  is_live_photo INTEGER NOT NULL DEFAULT 0 CHECK (is_live_photo IN (0,1)),
  live_mov_path TEXT,
  live_mov_size INTEGER,
  live_mov_mtime_ns INTEGER,
  asset_status TEXT NOT NULL DEFAULT 'active' CHECK (asset_status IN ('active','deleted','missing')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(library_source_id, primary_path)
);

CREATE INDEX IF NOT EXISTS idx_photo_asset_fingerprint
ON photo_asset(primary_fingerprint);

CREATE INDEX IF NOT EXISTS idx_photo_asset_capture_month
ON photo_asset(capture_month);

CREATE TABLE IF NOT EXISTS scan_batch (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_session_id INTEGER NOT NULL REFERENCES scan_session(id),
  stage TEXT NOT NULL CHECK (stage='detect'),
  worker_slot INTEGER NOT NULL,
  claim_token TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('claimed','running','acked','failed')),
  retry_count INTEGER NOT NULL DEFAULT 0,
  claimed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at TEXT,
  acked_at TEXT,
  error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_scan_batch_session
ON scan_batch(scan_session_id);

CREATE INDEX IF NOT EXISTS idx_scan_batch_status
ON scan_batch(status);

CREATE TABLE IF NOT EXISTS scan_batch_item (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_batch_id INTEGER NOT NULL REFERENCES scan_batch(id),
  photo_asset_id INTEGER NOT NULL REFERENCES photo_asset(id),
  item_order INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','running','done','failed')),
  error_message TEXT,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(scan_batch_id, item_order)
);

CREATE INDEX IF NOT EXISTS idx_scan_batch_item_asset
ON scan_batch_item(photo_asset_id);

CREATE TABLE IF NOT EXISTS face_observation (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  photo_asset_id INTEGER NOT NULL REFERENCES photo_asset(id),
  face_index INTEGER NOT NULL,
  crop_relpath TEXT NOT NULL,
  aligned_relpath TEXT NOT NULL,
  context_relpath TEXT NOT NULL,
  bbox_x1 REAL NOT NULL,
  bbox_y1 REAL NOT NULL,
  bbox_x2 REAL NOT NULL,
  bbox_y2 REAL NOT NULL,
  detector_confidence REAL NOT NULL,
  face_area_ratio REAL NOT NULL,
  magface_quality REAL NOT NULL,
  quality_score REAL NOT NULL,
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
  inactive_reason TEXT CHECK (inactive_reason IN ('asset_deleted','re_detect_replaced','manual_drop') OR inactive_reason IS NULL),
  pending_reassign INTEGER NOT NULL DEFAULT 0 CHECK (pending_reassign IN (0,1)),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(photo_asset_id, face_index),
  CHECK (bbox_x2 > bbox_x1 AND bbox_y2 > bbox_y1)
);

CREATE INDEX IF NOT EXISTS idx_face_observation_asset
ON face_observation(photo_asset_id);

CREATE INDEX IF NOT EXISTS idx_face_observation_pending_reassign
ON face_observation(pending_reassign);

CREATE TABLE IF NOT EXISTS person (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_uuid TEXT NOT NULL UNIQUE,
  display_name TEXT,
  is_named INTEGER NOT NULL DEFAULT 0 CHECK (is_named IN (0, 1)),
  status TEXT NOT NULL CHECK (status IN ('active', 'merged')),
  merged_into_person_id INTEGER REFERENCES person(id),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS assignment_run (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_session_id INTEGER NOT NULL REFERENCES scan_session(id),
  algorithm_version TEXT NOT NULL,
  param_snapshot_json TEXT NOT NULL,
  run_kind TEXT NOT NULL CHECK (run_kind IN ('scan_full', 'scan_incremental', 'scan_resume')),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_assignment_run_started_at
ON assignment_run(started_at);

CREATE INDEX IF NOT EXISTS idx_assignment_run_scan_session
ON assignment_run(scan_session_id, started_at);

CREATE TABLE IF NOT EXISTS face_cluster (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cluster_uuid TEXT NOT NULL UNIQUE,
  person_id INTEGER NOT NULL REFERENCES person(id),
  status TEXT NOT NULL CHECK (status IN ('active', 'replaced')),
  rebuild_scope TEXT NOT NULL CHECK (rebuild_scope IN ('full', 'local')),
  created_assignment_run_id INTEGER NOT NULL REFERENCES assignment_run(id),
  updated_assignment_run_id INTEGER NOT NULL REFERENCES assignment_run(id),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_face_cluster_status
ON face_cluster(status, person_id);

CREATE TABLE IF NOT EXISTS face_cluster_member (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  face_cluster_id INTEGER NOT NULL REFERENCES face_cluster(id),
  face_observation_id INTEGER NOT NULL REFERENCES face_observation(id),
  assignment_run_id INTEGER NOT NULL REFERENCES assignment_run(id),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(face_cluster_id, face_observation_id)
);

CREATE INDEX IF NOT EXISTS idx_face_cluster_member_face
ON face_cluster_member(face_observation_id);

CREATE TABLE IF NOT EXISTS face_cluster_rep_face (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  face_cluster_id INTEGER NOT NULL REFERENCES face_cluster(id),
  face_observation_id INTEGER NOT NULL REFERENCES face_observation(id),
  rep_rank INTEGER NOT NULL,
  assignment_run_id INTEGER NOT NULL REFERENCES assignment_run(id),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(face_cluster_id, rep_rank)
);

CREATE INDEX IF NOT EXISTS idx_face_cluster_rep_face_obs
ON face_cluster_rep_face(face_observation_id);

CREATE TABLE IF NOT EXISTS person_face_assignment (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id INTEGER NOT NULL REFERENCES person(id),
  face_observation_id INTEGER NOT NULL REFERENCES face_observation(id),
  assignment_run_id INTEGER NOT NULL REFERENCES assignment_run(id),
  assignment_source TEXT NOT NULL CHECK (assignment_source IN ('hdbscan', 'person_consensus', 'merge', 'undo')),
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  confidence REAL,
  margin REAL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_assignment_face_active
ON person_face_assignment(face_observation_id)
WHERE active = 1;

CREATE INDEX IF NOT EXISTS idx_assignment_person
ON person_face_assignment(person_id, active);

CREATE INDEX IF NOT EXISTS idx_assignment_run
ON person_face_assignment(assignment_run_id);

CREATE TABLE IF NOT EXISTS person_face_exclusion (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id INTEGER NOT NULL REFERENCES person(id),
  face_observation_id INTEGER NOT NULL REFERENCES face_observation(id),
  reason TEXT NOT NULL CHECK (reason='manual_exclude'),
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_person_face_exclusion_active
ON person_face_exclusion(person_id, face_observation_id)
WHERE active = 1;

CREATE INDEX IF NOT EXISTS idx_exclusion_face
ON person_face_exclusion(face_observation_id, active);

CREATE TABLE IF NOT EXISTS merge_operation (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  selected_person_ids_json TEXT NOT NULL,
  winner_person_id INTEGER NOT NULL REFERENCES person(id),
  winner_person_uuid TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('applied', 'undone')),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  undone_at TEXT
);

CREATE TABLE IF NOT EXISTS merge_operation_person_delta (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  merge_operation_id INTEGER NOT NULL REFERENCES merge_operation(id),
  person_id INTEGER NOT NULL REFERENCES person(id),
  before_snapshot_json TEXT NOT NULL,
  after_snapshot_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_merge_operation_person_delta_merge_operation
ON merge_operation_person_delta(merge_operation_id);

CREATE TABLE IF NOT EXISTS merge_operation_assignment_delta (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  merge_operation_id INTEGER NOT NULL REFERENCES merge_operation(id),
  face_observation_id INTEGER NOT NULL REFERENCES face_observation(id),
  before_assignment_json TEXT NOT NULL,
  after_assignment_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_merge_operation_assignment_delta_merge_operation
ON merge_operation_assignment_delta(merge_operation_id);

CREATE TABLE IF NOT EXISTS merge_operation_exclusion_delta (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  merge_operation_id INTEGER NOT NULL REFERENCES merge_operation(id),
  person_id INTEGER NOT NULL REFERENCES person(id),
  face_observation_id INTEGER NOT NULL REFERENCES face_observation(id),
  before_exclusion_json TEXT NOT NULL,
  after_exclusion_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_merge_operation_exclusion_delta_merge_operation
ON merge_operation_exclusion_delta(merge_operation_id);

CREATE TABLE IF NOT EXISTS export_template (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  output_root TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS export_template_person (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  template_id INTEGER NOT NULL REFERENCES export_template(id),
  person_id INTEGER NOT NULL REFERENCES person(id),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(export_run_id, media_kind, destination_path)
);

CREATE INDEX IF NOT EXISTS idx_export_delivery_status
ON export_delivery(delivery_status);

CREATE TABLE IF NOT EXISTS ops_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
  scan_session_id INTEGER REFERENCES scan_session(id),
  export_run_id INTEGER REFERENCES export_run(id),
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ops_event_type_created
ON ops_event(event_type, created_at);

CREATE INDEX IF NOT EXISTS idx_ops_event_scan
ON ops_event(scan_session_id);

CREATE INDEX IF NOT EXISTS idx_ops_event_export_run
ON ops_event(export_run_id);

CREATE TABLE IF NOT EXISTS scan_audit_item (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scan_session_id INTEGER NOT NULL REFERENCES scan_session(id),
  assignment_run_id INTEGER NOT NULL REFERENCES assignment_run(id),
  audit_type TEXT NOT NULL CHECK (audit_type IN (
    'low_margin_auto_assign', 'reassign_after_exclusion', 'new_anonymous_person'
  )),
  face_observation_id INTEGER NOT NULL REFERENCES face_observation(id),
  person_id INTEGER REFERENCES person(id),
  evidence_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_scan_audit_session
ON scan_audit_item(scan_session_id, audit_type);
