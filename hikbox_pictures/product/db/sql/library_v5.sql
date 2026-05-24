-- library v5: 连拍挑选结果按算法版本失效

ALTER TABLE export_burst_pick_run
  ADD COLUMN algorithm_version TEXT NOT NULL DEFAULT 'visual_fingerprint_v1';

CREATE INDEX idx_export_burst_pick_run_template_algorithm_started
  ON export_burst_pick_run(template_id, algorithm_version, started_at);
