CREATE TABLE IF NOT EXISTS embedding_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

INSERT INTO embedding_meta(key, value, updated_at)
VALUES ('schema_version', '1', CURRENT_TIMESTAMP)
ON CONFLICT(key) DO UPDATE SET
  value = excluded.value,
  updated_at = CURRENT_TIMESTAMP;

INSERT INTO embedding_meta(key, value, updated_at)
VALUES ('vector_dim', '512', CURRENT_TIMESTAMP)
ON CONFLICT(key) DO UPDATE SET
  value = excluded.value,
  updated_at = CURRENT_TIMESTAMP;

INSERT INTO embedding_meta(key, value, updated_at)
VALUES ('vector_dtype', 'float32', CURRENT_TIMESTAMP)
ON CONFLICT(key) DO UPDATE SET
  value = excluded.value,
  updated_at = CURRENT_TIMESTAMP;

CREATE TABLE IF NOT EXISTS face_embedding (
  id INTEGER PRIMARY KEY,
  face_observation_id INTEGER NOT NULL,
  feature_type TEXT NOT NULL CHECK (feature_type = 'face'),
  model_key TEXT NOT NULL,
  variant TEXT NOT NULL CHECK (variant IN ('main', 'flip')),
  dim INTEGER NOT NULL CHECK (dim = 512),
  dtype TEXT NOT NULL CHECK (dtype = 'float32'),
  vector_blob BLOB NOT NULL,
  created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_face_embedding_identity
ON face_embedding(face_observation_id, feature_type, model_key, variant);

CREATE INDEX IF NOT EXISTS idx_face_embedding_observation
ON face_embedding(face_observation_id);
