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
