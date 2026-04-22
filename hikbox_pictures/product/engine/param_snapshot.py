"""冻结参数快照。"""

from __future__ import annotations

from copy import deepcopy

FROZEN_V5_PARAM_SNAPSHOT: dict[str, object] = {
    "det_size": 640,
    "preview_max_side": 480,
    "min_cluster_size": 2,
    "min_samples": 1,
    "person_merge_threshold": 0.26,
    "person_linkage": "single",
    "person_rep_top_k": 3,
    "person_knn_k": 8,
    "person_enable_same_photo_cannot_link": False,
    "embedding_enable_flip": True,
    "person_consensus_distance_threshold": 0.24,
    "person_consensus_margin_threshold": 0.04,
    "person_consensus_rep_top_k": 3,
    "face_min_quality_for_assignment": 0.25,
    "low_quality_micro_cluster_max_size": 3,
    "low_quality_micro_cluster_top2_weight": 0.5,
    "low_quality_micro_cluster_min_quality_evidence": 0.72,
    "person_cluster_recall_distance_threshold": 0.32,
    "person_cluster_recall_margin_threshold": 0.04,
    "person_cluster_recall_top_n": 5,
    "person_cluster_recall_min_votes": 3,
    "person_cluster_recall_source_max_cluster_size": 20,
    "person_cluster_recall_source_max_person_faces": 8,
    "person_cluster_recall_target_min_person_faces": 40,
    "person_cluster_recall_max_rounds": 2,
}


def build_frozen_v5_param_snapshot() -> dict[str, object]:
    """返回可安全修改的参数快照副本。"""
    return deepcopy(FROZEN_V5_PARAM_SNAPSHOT)
