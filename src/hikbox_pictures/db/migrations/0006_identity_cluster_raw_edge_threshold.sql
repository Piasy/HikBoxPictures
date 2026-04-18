PRAGMA foreign_keys = ON;
PRAGMA defer_foreign_keys = ON;

ALTER TABLE identity_cluster_profile
ADD COLUMN raw_edge_max_distance REAL NOT NULL DEFAULT 0.35;

UPDATE identity_cluster_profile
SET discovery_knn_k = 7,
    updated_at = CURRENT_TIMESTAMP
WHERE discovery_knn_k = 24
  AND (
      profile_version LIKE '%.cluster.v3_1'
      OR profile_version = 'v3_1.default.cluster'
  );
