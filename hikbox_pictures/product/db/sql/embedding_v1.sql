CREATE TABLE schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1');

CREATE TABLE face_embeddings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  face_observation_id INTEGER NOT NULL,
  variant TEXT NOT NULL CHECK (variant IN ('main')),
  dimension INTEGER NOT NULL,
  l2_norm REAL NOT NULL,
  vector_blob BLOB NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(face_observation_id, variant)
);

CREATE INDEX idx_face_embeddings_face_id ON face_embeddings(face_observation_id, variant);
