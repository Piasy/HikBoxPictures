from __future__ import annotations

from dataclasses import dataclass, field

ASSIGN_SOURCE_CHOICES: tuple[str, str, str] = ("all", "review_pending", "attachment")


@dataclass(slots=True)
class AssignParameters:
    base_run_id: int | None = None
    assign_source: str = "all"
    top_k: int = 5
    auto_max_distance: float = 0.25
    review_max_distance: float = 0.35
    min_margin: float = 0.08
    promote_cluster_ids: tuple[int, ...] = ()
    disable_seed_cluster_ids: tuple[int, ...] = ()

    def validate(self) -> AssignParameters:
        if self.assign_source not in ASSIGN_SOURCE_CHOICES:
            raise ValueError(f"不支持的 assign_source: {self.assign_source}")
        if self.top_k <= 0:
            raise ValueError("top_k 必须大于 0")
        if self.auto_max_distance > self.review_max_distance:
            raise ValueError("auto_max_distance 不能大于 review_max_distance")
        if self.min_margin < 0:
            raise ValueError("min_margin 不能小于 0")
        return self


@dataclass(slots=True, frozen=True)
class BaseRunContext:
    id: int
    run_status: str
    observation_snapshot_id: int
    cluster_profile_id: int
    is_review_target: bool


@dataclass(slots=True, frozen=True)
class SnapshotContext:
    id: int
    observation_profile_id: int
    embedding_model_key: str


@dataclass(slots=True, frozen=True)
class TopCandidateRecord:
    rank: int
    identity_id: str
    cluster_id: int
    distance: float


@dataclass(slots=True, frozen=True)
class ClusterMemberRecord:
    cluster_id: int
    observation_id: int
    photo_id: int
    source_pool_kind: str
    member_role: str
    decision_status: str
    is_selected_trusted_seed: bool
    is_representative: bool
    quality_score_snapshot: float | None
    primary_path: str | None
    embedding_vector: list[float] | None
    embedding_dim: int | None


@dataclass(slots=True, frozen=True)
class ClusterRecord:
    cluster_id: int
    cluster_stage: str
    cluster_state: str
    resolution_state: str
    representative_observation_id: int | None
    retained_member_count: int
    distinct_photo_count: int
    representative_count: int
    retained_count: int
    excluded_count: int
    members: list[ClusterMemberRecord]


@dataclass(slots=True, frozen=True)
class ObservationCandidateRecord:
    observation_id: int
    photo_id: int
    source_kind: str
    source_cluster_id: int | None
    primary_path: str | None
    embedding_vector: list[float] | None
    embedding_dim: int | None
    embedding_model_key: str | None


@dataclass(slots=True, frozen=True)
class SeedIdentityRecord:
    identity_id: str
    source_cluster_id: int
    resolution_state: str
    seed_member_count: int
    fallback_used: bool
    prototype_dimension: int | None
    representative_observation_id: int | None
    member_observation_ids: list[int]
    valid: bool
    error_code: str | None
    error_message: str | None
    prototype_vector: list[float] | None


@dataclass(slots=True, frozen=True)
class SeedBuildResult:
    valid_seeds_by_cluster: dict[int, SeedIdentityRecord]
    invalid_seeds: list[SeedIdentityRecord]
    errors: list[dict[str, object]]
    prototype_dimension: int | None


@dataclass(slots=True, frozen=True)
class AssignmentRecord:
    observation_id: int
    photo_id: int
    source_kind: str
    source_cluster_id: int | None
    best_identity_id: str | None
    best_cluster_id: int | None
    best_distance: float | None
    second_best_distance: float | None
    distance_margin: float | None
    same_photo_conflict: bool
    decision: str
    reason_code: str
    top_candidates: list[TopCandidateRecord]
    assets: dict[str, str | None]
    missing_assets: list[str]


@dataclass(slots=True, frozen=True)
class AssignmentSummary:
    candidate_count: int
    auto_assign_count: int
    review_count: int
    reject_count: int
    same_photo_conflict_count: int
    missing_embedding_count: int
    dimension_mismatch_count: int


@dataclass(slots=True, frozen=True)
class AssignmentEvaluation:
    assignments: list[AssignmentRecord]
    by_observation_id: dict[int, AssignmentRecord]
    excluded_seed_observation_ids: set[int]
    summary: AssignmentSummary


@dataclass(slots=True)
class QueryContext:
    base_run: BaseRunContext
    snapshot: SnapshotContext
    clusters: list[ClusterRecord]
    clusters_by_id: dict[int, ClusterRecord]
    candidate_observations: list[ObservationCandidateRecord]
    non_rejected_member_observation_ids_by_cluster: dict[int, set[int]]
    source_candidate_observation_ids: dict[str, set[int]]
    warnings: list[str] = field(default_factory=list)

