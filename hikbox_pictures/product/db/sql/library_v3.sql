-- library v3: 新增全局放弃导出标记

CREATE TABLE export_abandoned_asset (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id INTEGER NOT NULL REFERENCES assets(id),
  triggered_template_id TEXT NOT NULL REFERENCES export_template(template_id),
  group_key TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(asset_id)
);

CREATE INDEX idx_export_abandoned_asset_template
  ON export_abandoned_asset(triggered_template_id, created_at);
