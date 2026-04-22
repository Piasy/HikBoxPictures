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
