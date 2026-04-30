-- library v3 migration: export_plan table and export_delivery.plan_id column
-- Feature Slice 2: 导出计划持久化与同名冲突消解

CREATE TABLE IF NOT EXISTS export_plan (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  template_id TEXT NOT NULL REFERENCES export_template(template_id),
  asset_id INTEGER NOT NULL REFERENCES assets(id),
  bucket TEXT NOT NULL,
  month TEXT NOT NULL,
  file_name TEXT NOT NULL,
  mov_file_name TEXT,
  source_label TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(template_id, asset_id)
);

CREATE INDEX IF NOT EXISTS idx_export_plan_template_bucket_month
  ON export_plan(template_id, bucket, month, file_name);

ALTER TABLE export_delivery ADD COLUMN plan_id INTEGER REFERENCES export_plan(id);
