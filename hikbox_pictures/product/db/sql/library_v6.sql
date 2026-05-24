-- library v6: 连拍挑选多特征强边 evidence

ALTER TABLE export_burst_pick_group_edge
  ADD COLUMN edge_type TEXT;

ALTER TABLE export_burst_pick_group_edge
  ADD COLUMN confidence REAL;

ALTER TABLE export_burst_pick_group_edge
  ADD COLUMN phash_hamming INTEGER;

ALTER TABLE export_burst_pick_group_edge
  ADD COLUMN center_phash_hamming INTEGER;

ALTER TABLE export_burst_pick_group_edge
  ADD COLUMN block_match_ratio REAL;
