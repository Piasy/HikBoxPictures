from __future__ import annotations

import numpy as np
import pytest

from hikbox_experiments.identity_v3_1.assignment_service import IdentityV31AssignmentService
from hikbox_experiments.identity_v3_1.models import (
    AssignParameters,
    BaseRunContext,
    ClusterMemberRecord,
    ClusterRecord,
    ObservationCandidateRecord,
    QueryContext,
    SeedBuildResult,
    SeedIdentityRecord,
    SnapshotContext,
)


def _normalize(values: list[float]) -> list[float]:
    vector = np.asarray(values, dtype=np.float64)
    norm = float(np.linalg.norm(vector))
    if norm <= 0.0:
        raise ValueError("向量范数必须大于 0")
    return (vector / norm).astype(np.float64).tolist()


def _l2(a: list[float], b: list[float]) -> float:
    delta = np.asarray(a, dtype=np.float64) - np.asarray(b, dtype=np.float64)
    return float(np.linalg.norm(delta))


def _member(
    *,
    cluster_id: int,
    observation_id: int,
    photo_id: int,
    decision_status: str = "retained",
    trusted: bool = False,
    representative: bool = False,
    embedding_vector: list[float] | None = None,
    embedding_dim: int | None = None,
) -> ClusterMemberRecord:
    return ClusterMemberRecord(
        cluster_id=cluster_id,
        observation_id=observation_id,
        photo_id=photo_id,
        source_pool_kind="review_pending",
        member_role="member",
        decision_status=decision_status,
        is_selected_trusted_seed=trusted,
        is_representative=representative,
        quality_score_snapshot=0.9,
        primary_path=f"/tmp/{observation_id}.jpg",
        embedding_vector=embedding_vector,
        embedding_dim=embedding_dim,
    )


def _cluster(
    *,
    cluster_id: int,
    resolution_state: str,
    members: list[ClusterMemberRecord],
    stage: str = "final",
    state: str = "active",
    representative_observation_id: int | None = None,
) -> ClusterRecord:
    return ClusterRecord(
        cluster_id=cluster_id,
        cluster_stage=stage,
        cluster_state=state,
        resolution_state=resolution_state,
        representative_observation_id=representative_observation_id,
        retained_member_count=sum(1 for item in members if item.decision_status == "retained"),
        distinct_photo_count=len({item.photo_id for item in members}),
        representative_count=sum(1 for item in members if item.is_representative),
        retained_count=sum(1 for item in members if item.decision_status == "retained"),
        excluded_count=sum(1 for item in members if item.decision_status == "rejected"),
        members=members,
    )


def _make_primary_cluster() -> ClusterRecord:
    return _cluster(
        cluster_id=101,
        resolution_state="materialized",
        representative_observation_id=10103,
        members=[
            _member(
                cluster_id=101,
                observation_id=10101,
                photo_id=50101,
                decision_status="retained",
                trusted=True,
                embedding_vector=[1.0, 0.0],
                embedding_dim=2,
            ),
            _member(
                cluster_id=101,
                observation_id=10102,
                photo_id=59999,
                decision_status="deferred",
                trusted=True,
                embedding_vector=[1.0, 1.0],
                embedding_dim=2,
            ),
            _member(
                cluster_id=101,
                observation_id=10103,
                photo_id=50103,
                decision_status="retained",
                representative=True,
                embedding_vector=[0.0, 1.0],
                embedding_dim=2,
            ),
        ],
    )


def _make_fallback_cluster() -> ClusterRecord:
    return _cluster(
        cluster_id=202,
        resolution_state="materialized",
        representative_observation_id=20203,
        members=[
            _member(
                cluster_id=202,
                observation_id=20201,
                photo_id=50201,
                decision_status="retained",
                embedding_vector=[0.0, 1.0],
                embedding_dim=2,
            ),
            _member(
                cluster_id=202,
                observation_id=20202,
                photo_id=50202,
                decision_status="deferred",
                embedding_vector=[-1.0, 1.0],
                embedding_dim=2,
            ),
            _member(
                cluster_id=202,
                observation_id=20203,
                photo_id=50203,
                decision_status="rejected",
                representative=True,
                embedding_vector=[1.0, 0.0],
                embedding_dim=2,
            ),
        ],
    )


def _make_pending_cluster() -> ClusterRecord:
    return _cluster(
        cluster_id=303,
        resolution_state="review_pending",
        representative_observation_id=30301,
        members=[
            _member(
                cluster_id=303,
                observation_id=30301,
                photo_id=50301,
                decision_status="retained",
                embedding_vector=[0.6, 0.8],
                embedding_dim=2,
            ),
            _member(
                cluster_id=303,
                observation_id=30302,
                photo_id=50302,
                decision_status="deferred",
                embedding_vector=[0.8, 0.6],
                embedding_dim=2,
            ),
        ],
    )


def _make_invalid_seed_cluster() -> ClusterRecord:
    return _cluster(
        cluster_id=404,
        resolution_state="materialized",
        representative_observation_id=40401,
        members=[
            _member(
                cluster_id=404,
                observation_id=40401,
                photo_id=50401,
                decision_status="retained",
                embedding_vector=None,
                embedding_dim=None,
            ),
            _member(
                cluster_id=404,
                observation_id=40402,
                photo_id=50402,
                decision_status="deferred",
                embedding_vector=None,
                embedding_dim=None,
            ),
        ],
    )


def _candidate(
    *,
    observation_id: int,
    photo_id: int,
    source_kind: str = "attachment",
    source_cluster_id: int | None = None,
    embedding_vector: list[float] | None,
    embedding_dim: int | None,
) -> ObservationCandidateRecord:
    return ObservationCandidateRecord(
        observation_id=observation_id,
        photo_id=photo_id,
        source_kind=source_kind,
        source_cluster_id=source_cluster_id,
        primary_path=f"/tmp/candidate-{observation_id}.jpg",
        embedding_vector=embedding_vector,
        embedding_dim=embedding_dim,
        embedding_model_key="mock-model",
    )


def _make_query_context(
    *,
    clusters: list[ClusterRecord],
    candidates: list[ObservationCandidateRecord],
) -> QueryContext:
    by_cluster: dict[int, set[int]] = {}
    for cluster in clusters:
        by_cluster[cluster.cluster_id] = {
            member.observation_id for member in cluster.members if member.decision_status != "rejected"
        }
    return QueryContext(
        base_run=BaseRunContext(
            id=1,
            run_status="succeeded",
            observation_snapshot_id=11,
            cluster_profile_id=22,
            is_review_target=True,
        ),
        snapshot=SnapshotContext(id=11, observation_profile_id=33, embedding_model_key="mock-model"),
        clusters=clusters,
        clusters_by_id={item.cluster_id: item for item in clusters},
        candidate_observations=candidates,
        non_rejected_member_observation_ids_by_cluster=by_cluster,
        source_candidate_observation_ids={
            "all": {item.observation_id for item in candidates},
            "review_pending": {
                item.observation_id for item in candidates if item.source_kind == "review_pending_retained"
            },
            "attachment": {item.observation_id for item in candidates if item.source_kind == "attachment"},
        },
        warnings=[],
    )


def test_build_seed_identities_prefers_trusted_then_fallback_and_uses_normalized_mean() -> None:
    service = IdentityV31AssignmentService()
    clusters = [_make_primary_cluster(), _make_fallback_cluster()]

    seed_result = service.build_seed_identities(
        clusters=clusters,
        promote_cluster_ids=set(),
        disable_seed_cluster_ids=set(),
    )

    primary = seed_result.valid_seeds_by_cluster[101]
    fallback = seed_result.valid_seeds_by_cluster[202]

    trusted_expected = _normalize([1.0, 0.5])
    fallback_expected = _normalize([-0.5, 1.0])
    assert primary.fallback_used is False
    assert fallback.fallback_used is True
    assert primary.prototype_vector == pytest.approx(trusted_expected, abs=1e-8)
    assert fallback.prototype_vector == pytest.approx(fallback_expected, abs=1e-8)

    assert primary.prototype_vector != pytest.approx([1.0, 0.0], abs=1e-8)
    assert primary.prototype_vector != pytest.approx([0.0, 1.0], abs=1e-8)
    assert primary.prototype_vector != pytest.approx([1.0, 0.5], abs=1e-8)


def test_evaluate_assignments_outputs_auto_review_reject_and_summary_invariants() -> None:
    service = IdentityV31AssignmentService()
    clusters = [_make_primary_cluster(), _make_fallback_cluster()]
    seed_result = service.build_seed_identities(
        clusters=clusters,
        promote_cluster_ids=set(),
        disable_seed_cluster_ids=set(),
    )

    candidates = [
        _candidate(
            observation_id=9001,
            photo_id=99001,
            embedding_vector=_normalize([0.88, 0.47]),
            embedding_dim=2,
        ),
        _candidate(
            observation_id=9002,
            photo_id=59999,
            embedding_vector=_normalize([0.86, 0.50]),
            embedding_dim=2,
        ),
        _candidate(
            observation_id=9003,
            photo_id=99003,
            embedding_vector=_normalize([0.30, 0.95]),
            embedding_dim=2,
        ),
        _candidate(
            observation_id=9004,
            photo_id=99004,
            embedding_vector=_normalize([0.70, 0.71]),
            embedding_dim=2,
        ),
        _candidate(
            observation_id=9005,
            photo_id=99005,
            embedding_vector=_normalize([-0.95, -0.31]),
            embedding_dim=2,
        ),
        _candidate(
            observation_id=9006,
            photo_id=99006,
            embedding_vector=None,
            embedding_dim=None,
        ),
        _candidate(
            observation_id=9007,
            photo_id=99007,
            embedding_vector=[1.0, 0.0, 0.0],
            embedding_dim=3,
        ),
        _candidate(
            observation_id=10101,
            photo_id=50101,
            embedding_vector=_normalize([0.88, 0.47]),
            embedding_dim=2,
        ),
    ]
    context = _make_query_context(clusters=clusters, candidates=candidates)

    params = AssignParameters(top_k=5, auto_max_distance=0.25, review_max_distance=0.90, min_margin=0.08)
    evaluation = service.evaluate_assignments(
        query_context=context,
        seed_result=seed_result,
        assign_parameters=params,
    )

    assert evaluation.summary.candidate_count == 5
    assert evaluation.summary.auto_assign_count == 1
    assert evaluation.summary.review_count == 3
    assert evaluation.summary.reject_count == 1
    assert evaluation.summary.same_photo_conflict_count == 1
    assert evaluation.summary.candidate_count == (
        evaluation.summary.auto_assign_count + evaluation.summary.review_count + evaluation.summary.reject_count
    )
    assert evaluation.summary.missing_embedding_count == 1
    assert evaluation.summary.dimension_mismatch_count == 1

    auto_item = evaluation.by_observation_id[9001]
    assert auto_item.decision == "auto_assign"
    assert auto_item.reason_code == "auto_threshold_pass"

    same_photo = evaluation.by_observation_id[9002]
    assert same_photo.decision == "review"
    assert same_photo.same_photo_conflict is True
    assert same_photo.reason_code == "same_photo_conflict"

    margin_item = evaluation.by_observation_id[9003]
    assert margin_item.decision == "review"
    assert margin_item.reason_code == "margin_below_threshold"

    distance_review = evaluation.by_observation_id[9004]
    assert distance_review.decision == "review"
    assert distance_review.reason_code == "distance_above_auto_threshold"
    assert seed_result.valid_seeds_by_cluster[101].prototype_vector is not None
    assert seed_result.valid_seeds_by_cluster[202].prototype_vector is not None
    expected_query = _normalize([0.70, 0.71])
    expected_best = _l2(seed_result.valid_seeds_by_cluster[101].prototype_vector, expected_query)
    expected_second = _l2(seed_result.valid_seeds_by_cluster[202].prototype_vector, expected_query)
    assert distance_review.best_distance == pytest.approx(expected_best, abs=1e-8)
    assert distance_review.second_best_distance == pytest.approx(expected_second, abs=1e-8)
    assert distance_review.distance_margin == pytest.approx(expected_second - expected_best, abs=1e-8)

    reject_item = evaluation.by_observation_id[9005]
    assert reject_item.decision == "reject"
    assert reject_item.reason_code == "distance_above_review_threshold"

    top_clusters = [item.cluster_id for item in distance_review.top_candidates]
    top_distances = [item.distance for item in distance_review.top_candidates]
    assert len(distance_review.top_candidates) == min(params.top_k, len(seed_result.valid_seeds_by_cluster))
    assert top_clusters == [101, 202]
    assert top_distances == pytest.approx(
        [
            _l2(seed_result.valid_seeds_by_cluster[101].prototype_vector, expected_query),
            _l2(seed_result.valid_seeds_by_cluster[202].prototype_vector, expected_query),
        ],
        abs=1e-8,
    )
    assert 10101 not in evaluation.by_observation_id


def test_single_seed_sets_second_best_distance_to_inf_and_only_one_top_candidate() -> None:
    service = IdentityV31AssignmentService()
    clusters = [_make_primary_cluster()]
    seed_result = service.build_seed_identities(
        clusters=clusters,
        promote_cluster_ids=set(),
        disable_seed_cluster_ids=set(),
    )
    context = _make_query_context(
        clusters=clusters,
        candidates=[
            _candidate(
                observation_id=9101,
                photo_id=99101,
                embedding_vector=_normalize([0.9, 0.43]),
                embedding_dim=2,
            )
        ],
    )

    evaluation = service.evaluate_assignments(
        query_context=context,
        seed_result=seed_result,
        assign_parameters=AssignParameters(top_k=5, auto_max_distance=0.25, review_max_distance=0.90, min_margin=0.08),
    )

    item = evaluation.by_observation_id[9101]
    assert item.second_best_distance == float("inf")
    assert len(item.top_candidates) == 1
    assert item.top_candidates[0].rank == 1
    assert item.top_candidates[0].cluster_id == 101


def test_override_validation_and_invalid_seed_recording() -> None:
    service = IdentityV31AssignmentService()
    clusters = [_make_primary_cluster(), _make_fallback_cluster(), _make_pending_cluster()]

    with pytest.raises(ValueError, match="promote cluster 不存在"):
        service.build_seed_identities(
            clusters=clusters,
            promote_cluster_ids={999999},
            disable_seed_cluster_ids=set(),
        )

    with pytest.raises(ValueError, match="只能 promote review_pending final"):
        service.build_seed_identities(
            clusters=clusters,
            promote_cluster_ids={101},
            disable_seed_cluster_ids=set(),
        )

    with pytest.raises(ValueError, match="disable 目标不是启用 seed cluster"):
        service.build_seed_identities(
            clusters=clusters,
            promote_cluster_ids=set(),
            disable_seed_cluster_ids={303},
        )

    seed_result = service.build_seed_identities(
        clusters=[_make_primary_cluster(), _make_invalid_seed_cluster()],
        promote_cluster_ids=set(),
        disable_seed_cluster_ids=set(),
    )

    assert len(seed_result.invalid_seeds) == 1
    invalid = seed_result.invalid_seeds[0]
    assert invalid.valid is False
    assert invalid.source_cluster_id == 404
    assert invalid.error_code == "invalid_seed_prototype"
    assert seed_result.errors == [
        {
            "code": "invalid_seed_prototype",
            "cluster_id": 404,
            "message": invalid.error_message,
        }
    ]


def test_build_seed_identities_raises_when_all_enabled_seeds_invalid() -> None:
    service = IdentityV31AssignmentService()

    with pytest.raises(ValueError, match="没有任何可用 seed identity"):
        service.build_seed_identities(
            clusters=[_make_invalid_seed_cluster()],
            promote_cluster_ids=set(),
            disable_seed_cluster_ids=set(),
        )


def test_promoted_pending_seed_excludes_its_members_from_assignments_and_candidate_count() -> None:
    service = IdentityV31AssignmentService()
    clusters = [_make_primary_cluster(), _make_fallback_cluster(), _make_pending_cluster()]
    seed_result = service.build_seed_identities(
        clusters=clusters,
        promote_cluster_ids={303},
        disable_seed_cluster_ids=set(),
    )
    context = _make_query_context(
        clusters=clusters,
        candidates=[
            _candidate(
                observation_id=30301,
                photo_id=50301,
                source_kind="review_pending_retained",
                source_cluster_id=303,
                embedding_vector=_normalize([0.60, 0.80]),
                embedding_dim=2,
            ),
            _candidate(
                observation_id=9201,
                photo_id=99201,
                embedding_vector=_normalize([0.88, 0.47]),
                embedding_dim=2,
            ),
        ],
    )

    evaluation = service.evaluate_assignments(
        query_context=context,
        seed_result=seed_result,
        assign_parameters=AssignParameters(top_k=3, auto_max_distance=0.25, review_max_distance=0.90, min_margin=0.08),
    )

    assert 30301 in evaluation.excluded_seed_observation_ids
    assert 30302 in evaluation.excluded_seed_observation_ids
    assert 30301 not in evaluation.by_observation_id
    assert evaluation.summary.candidate_count == 1


def test_raise_when_candidates_non_empty_but_all_missing_embedding() -> None:
    service = IdentityV31AssignmentService()
    clusters = [_make_primary_cluster(), _make_fallback_cluster()]
    seed_result = service.build_seed_identities(
        clusters=clusters,
        promote_cluster_ids=set(),
        disable_seed_cluster_ids=set(),
    )
    context = _make_query_context(
        clusters=clusters,
        candidates=[
            _candidate(
                observation_id=9301,
                photo_id=99301,
                embedding_vector=None,
                embedding_dim=None,
            )
        ],
    )

    with pytest.raises(ValueError, match="所有候选 observation 都缺少可用 embedding"):
        service.evaluate_assignments(
            query_context=context,
            seed_result=seed_result,
            assign_parameters=AssignParameters(top_k=3),
        )


def test_top_candidates_use_distance_then_cluster_id_order_with_true_l2() -> None:
    service = IdentityV31AssignmentService()
    seed_result = SeedBuildResult(
        valid_seeds_by_cluster={
            22: SeedIdentityRecord(
                identity_id="seed-cluster-22",
                source_cluster_id=22,
                resolution_state="materialized",
                seed_member_count=1,
                fallback_used=False,
                prototype_dimension=2,
                representative_observation_id=2201,
                member_observation_ids=[2201],
                valid=True,
                error_code=None,
                error_message=None,
                prototype_vector=[-1.0, 0.0],
            ),
            11: SeedIdentityRecord(
                identity_id="seed-cluster-11",
                source_cluster_id=11,
                resolution_state="materialized",
                seed_member_count=1,
                fallback_used=False,
                prototype_dimension=2,
                representative_observation_id=1101,
                member_observation_ids=[1101],
                valid=True,
                error_code=None,
                error_message=None,
                prototype_vector=[1.0, 0.0],
            ),
        },
        invalid_seeds=[],
        errors=[],
        prototype_dimension=2,
    )
    context = _make_query_context(
        clusters=[_make_primary_cluster(), _make_fallback_cluster()],
        candidates=[
            _candidate(
                observation_id=9401,
                photo_id=99401,
                embedding_vector=[0.0, 1.0],
                embedding_dim=2,
            )
        ],
    )

    evaluation = service.evaluate_assignments(
        query_context=context,
        seed_result=seed_result,
        assign_parameters=AssignParameters(top_k=5, auto_max_distance=0.5, review_max_distance=1.0, min_margin=0.05),
    )

    item = evaluation.by_observation_id[9401]
    assert [candidate.cluster_id for candidate in item.top_candidates] == [11, 22]
    assert [candidate.rank for candidate in item.top_candidates] == [1, 2]
    assert [candidate.distance for candidate in item.top_candidates] == pytest.approx(
        [np.sqrt(2.0), np.sqrt(2.0)],
        abs=1e-8,
    )


def test_reason_code_priority_distance_above_review_beats_same_photo_conflict() -> None:
    service = IdentityV31AssignmentService()
    clusters = [_make_primary_cluster(), _make_fallback_cluster()]
    seed_result = service.build_seed_identities(
        clusters=clusters,
        promote_cluster_ids=set(),
        disable_seed_cluster_ids=set(),
    )
    context = _make_query_context(
        clusters=clusters,
        candidates=[
            _candidate(
                observation_id=9501,
                photo_id=50201,
                embedding_vector=_normalize([-0.95, -0.31]),
                embedding_dim=2,
            )
        ],
    )

    evaluation = service.evaluate_assignments(
        query_context=context,
        seed_result=seed_result,
        assign_parameters=AssignParameters(top_k=3, auto_max_distance=0.25, review_max_distance=0.90, min_margin=0.08),
    )

    item = evaluation.by_observation_id[9501]
    assert item.same_photo_conflict is True
    assert item.best_distance is not None and item.best_distance > 0.90
    assert item.decision == "reject"
    assert item.reason_code == "distance_above_review_threshold"
