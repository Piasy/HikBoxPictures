-- library v4: 连拍挑选后台任务与持久化结果

CREATE TABLE export_burst_pick_run (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  template_id TEXT NOT NULL REFERENCES export_template(template_id),
  status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  error_message TEXT,
  total_candidate_count INTEGER NOT NULL DEFAULT 0,
  processed_candidate_count INTEGER NOT NULL DEFAULT 0,
  skipped_missing_or_unreadable_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_export_burst_pick_run_template_started
  ON export_burst_pick_run(template_id, started_at);

CREATE TABLE export_burst_pick_group (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES export_burst_pick_run(id),
  group_key TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  submitted_at TEXT,
  UNIQUE(run_id, group_key)
);

CREATE INDEX idx_export_burst_pick_group_run
  ON export_burst_pick_group(run_id, ordinal);

CREATE TABLE export_burst_pick_group_asset (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id INTEGER NOT NULL REFERENCES export_burst_pick_group(id),
  asset_id INTEGER NOT NULL REFERENCES assets(id),
  position INTEGER NOT NULL,
  file_name TEXT NOT NULL,
  bucket TEXT NOT NULL,
  month TEXT NOT NULL,
  context_url TEXT NOT NULL,
  is_live INTEGER NOT NULL CHECK (is_live IN (0, 1)),
  UNIQUE(group_id, asset_id)
);

CREATE INDEX idx_export_burst_pick_group_asset_group
  ON export_burst_pick_group_asset(group_id, position);

CREATE INDEX idx_export_burst_pick_group_asset_asset
  ON export_burst_pick_group_asset(asset_id);

CREATE TABLE export_burst_pick_group_edge (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id INTEGER NOT NULL REFERENCES export_burst_pick_group(id),
  asset_id_first INTEGER NOT NULL REFERENCES assets(id),
  asset_id_second INTEGER NOT NULL REFERENCES assets(id),
  threshold TEXT NOT NULL,
  metadata_assisted INTEGER NOT NULL CHECK (metadata_assisted IN (0, 1)),
  dhash_hamming INTEGER NOT NULL,
  luminance_cosine REAL NOT NULL,
  color_histogram_intersection REAL NOT NULL,
  capture_time_delta_seconds REAL,
  normalized_device_match INTEGER CHECK (normalized_device_match IN (0, 1))
);

CREATE INDEX idx_export_burst_pick_group_edge_group
  ON export_burst_pick_group_edge(group_id, asset_id_first, asset_id_second);
