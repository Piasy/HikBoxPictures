CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

INSERT INTO schema_meta(key, value, updated_at)
VALUES ('schema_version', '1', CURRENT_TIMESTAMP)
ON CONFLICT(key) DO NOTHING;

INSERT INTO schema_meta(key, value, updated_at)
VALUES ('product_schema_name', 'people_gallery_v1', CURRENT_TIMESTAMP)
ON CONFLICT(key) DO NOTHING;

CREATE TABLE IF NOT EXISTS library_source (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  root_path TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deleted')),
  last_discovered_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_library_source_enabled ON library_source(enabled);
CREATE INDEX IF NOT EXISTS idx_library_source_status ON library_source(status);

CREATE TABLE IF NOT EXISTS scan_session (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_kind TEXT NOT NULL CHECK (run_kind IN ('scan_full', 'scan_incremental', 'scan_resume')),
  status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'aborting', 'interrupted', 'completed', 'abandoned', 'failed')),
  triggered_by TEXT NOT NULL CHECK (triggered_by IN ('manual_webui', 'manual_cli')),
  resume_from_session_id INTEGER REFERENCES scan_session(id),
  started_at TEXT,
  finished_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scan_session_status ON scan_session(status);
CREATE INDEX IF NOT EXISTS idx_scan_session_created_at ON scan_session(created_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_scan_session_single_active
ON scan_session((1))
WHERE status IN ('running', 'aborting');
