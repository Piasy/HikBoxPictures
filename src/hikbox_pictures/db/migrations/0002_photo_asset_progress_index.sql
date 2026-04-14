CREATE INDEX IF NOT EXISTS idx_photo_asset_source_status
ON photo_asset(library_source_id, processing_status);
