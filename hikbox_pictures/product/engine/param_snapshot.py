from __future__ import annotations

ALGORITHM_VERSION = "v5.2026-04-21"
FROZEN_V5_STAGE_SEQUENCE = (
    "ahc_pass_1",
    "ahc_pass_2",
    "person_consensus",
    "person_cluster_recall",
)
LATE_FUSION_MISSING_SIMILARITY = -1.0
PERSON_CONSENSUS_SIMILARITY_THRESHOLD = 0.80
AHC_PASS_2_TIE_BREAK = "similarity_desc,person_id_asc,assignment_source_lex_asc"
IGNORED_ASSIGNMENT_SOURCES = ("noise", "low_quality_ignored")
UNKNOWN_ASSIGNMENT_SOURCE_FALLBACK = "hdbscan"


def build_param_snapshot() -> dict[str, object]:
    return {
        "algorithm_version": ALGORITHM_VERSION,
        "preview_max_side": 480,
        "clusterer": "HDBSCAN",
        "person_clusterer": "AHC",
        "stage_sequence": list(FROZEN_V5_STAGE_SEQUENCE),
        "late_fusion_rule": "max(main,flip)",
        "late_fusion_missing_similarity": LATE_FUSION_MISSING_SIMILARITY,
        "person_consensus_similarity_threshold": PERSON_CONSENSUS_SIMILARITY_THRESHOLD,
        "ahc_pass_2_tie_break": AHC_PASS_2_TIE_BREAK,
        "ignored_assignment_sources": list(IGNORED_ASSIGNMENT_SOURCES),
        "unknown_assignment_source_fallback": UNKNOWN_ASSIGNMENT_SOURCE_FALLBACK,
    }
