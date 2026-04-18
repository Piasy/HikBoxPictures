from __future__ import annotations

from pathlib import Path

import pytest

from hikbox_experiments.identity_v3_1.models import AssignParameters
from hikbox_experiments.identity_v3_1.query_service import IdentityV31QueryService

from .fixtures_identity_v3_1_export import build_identity_v3_1_export_workspace


def _candidate_ids(payload: object) -> set[int]:
    return {item.observation_id for item in payload.candidate_observations}


def _cluster_ids(payload: object) -> set[int]:
    return {cluster.cluster_id for cluster in payload.clusters}


def test_query_service_defaults_to_review_target_run_not_latest_run(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "query-default-run")
    try:
        payload = IdentityV31QueryService(ws.root).load_report_context(
            base_run_id=None,
            assign_parameters=AssignParameters(),
        )
        assert payload.base_run.id == ws.base_run_id
        assert payload.base_run.id != ws.latest_non_target_run_id
    finally:
        ws.close()


def test_query_service_fails_without_review_target_when_base_run_id_omitted(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "query-no-review-target")
    try:
        ws.conn.execute("UPDATE identity_cluster_run SET is_review_target = 0")
        ws.conn.commit()
        with pytest.raises(ValueError, match="默认 review target run 不存在"):
            IdentityV31QueryService(ws.root).load_report_context(
                base_run_id=None,
                assign_parameters=AssignParameters(),
            )
    finally:
        ws.close()


@pytest.mark.parametrize(
    ("mutate", "error_type", "pattern"),
    [
        (
            lambda root: (root / ".hikbox" / "config.json").unlink(),
            FileNotFoundError,
            "workspace 配置不存在",
        ),
        (
            lambda root: (root / ".hikbox" / "config.json").write_text(
                '{"version": 1, "external_root": ""}\n',
                encoding="utf-8",
            ),
            ValueError,
            "workspace 配置缺少 external_root",
        ),
        (
            lambda root: (root / ".hikbox" / "config.json").write_text("{broken-json\n", encoding="utf-8"),
            ValueError,
            "(Expecting|JSON|property|double quotes|delimiter)",
        ),
    ],
)
def test_query_service_propagates_workspace_load_errors(
    tmp_path: Path,
    mutate,
    error_type: type[Exception],
    pattern: str,
) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "query-broken-workspace")
    try:
        mutate(ws.root)
        with pytest.raises(error_type, match=pattern):
            IdentityV31QueryService(ws.root).load_report_context(
                base_run_id=None,
                assign_parameters=AssignParameters(),
            )
    finally:
        ws.close()


def test_query_service_rejects_missing_or_non_succeeded_run(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "query-run-validation")
    try:
        service = IdentityV31QueryService(ws.root)
        with pytest.raises(ValueError, match="cluster run 不存在"):
            service.load_report_context(base_run_id=999999, assign_parameters=AssignParameters())
        with pytest.raises(ValueError, match="run_status 必须为 succeeded"):
            service.load_report_context(base_run_id=ws.failed_run_id, assign_parameters=AssignParameters())
    finally:
        ws.close()


def test_query_service_honors_explicit_base_run_id_and_calls_validate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "query-explicit-run")
    calls = {"validate": 0}

    def _validate(self: AssignParameters) -> AssignParameters:
        calls["validate"] += 1
        return self

    monkeypatch.setattr(AssignParameters, "validate", _validate)
    try:
        payload = IdentityV31QueryService(ws.root).load_report_context(
            base_run_id=ws.latest_non_target_run_id,
            assign_parameters=AssignParameters(assign_source="attachment", top_k=7),
        )
        assert payload.base_run.id == ws.latest_non_target_run_id
        assert calls["validate"] == 1
    finally:
        ws.close()


def test_query_service_dedupes_overlap_candidate_and_prefers_review_pending_source(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "query-overlap-dedupe")
    try:
        payload = IdentityV31QueryService(ws.root).load_report_context(
            base_run_id=None,
            assign_parameters=AssignParameters(assign_source="all"),
        )
        overlap_items = [
            item
            for item in payload.candidate_observations
            if item.observation_id == ws.observation_ids["pending_attachment_overlap"]
        ]
        assert len(overlap_items) == 1
        assert overlap_items[0].source_kind == "review_pending_retained"
    finally:
        ws.close()


def test_query_service_returns_exact_cluster_and_candidate_scope_for_selected_run(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "query-scope-lock")
    try:
        payload = IdentityV31QueryService(ws.root).load_report_context(
            base_run_id=None,
            assign_parameters=AssignParameters(assign_source="all"),
        )
        assert _cluster_ids(payload) == ws.expected_cluster_ids_by_run_id[ws.base_run_id]
        assert _candidate_ids(payload) == ws.expected_candidate_ids_by_run_id_and_source[(ws.base_run_id, "all")]

        # cluster 顺序必须稳定：materialized 在前；同状态 retained_member_count DESC；再 cluster_id ASC。
        actual_cluster_order = [cluster.cluster_id for cluster in payload.clusters]
        expected_cluster_order = [
            cluster_id
            for cluster_id, _ in sorted(
                (
                    (
                        cluster.cluster_id,
                        (
                            0 if cluster.resolution_state == "materialized" else 1,
                            -cluster.retained_member_count,
                            cluster.cluster_id,
                        ),
                    )
                    for cluster in payload.clusters
                ),
                key=lambda item: item[1],
            )
        ]
        assert actual_cluster_order == expected_cluster_order

        assert ws.cluster_ids["other_run_materialized"] not in _cluster_ids(payload)
        assert ws.observation_ids["other_snapshot_attachment"] not in _candidate_ids(payload)
        assert ws.observation_ids["warmup_active"] not in _candidate_ids(payload)
    finally:
        ws.close()


def test_query_service_cluster_order_follows_resolution_then_retained_count_then_cluster_id(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "query-cluster-order-strength")
    try:
        # 人为制造“只按 cluster_id 排序会失败”的场景：
        # materialized 内 retained_member_count 明确拉开，且 pending 仍应排在 materialized 之后。
        ws.conn.execute(
            """
            UPDATE identity_cluster
            SET retained_member_count = CASE id
                WHEN ? THEN 1
                WHEN ? THEN 9
                WHEN ? THEN 5
                ELSE retained_member_count
            END
            WHERE id IN (?, ?, ?)
            """,
            (
                int(ws.cluster_ids["seed_primary"]),
                int(ws.cluster_ids["seed_fallback"]),
                int(ws.cluster_ids["seed_invalid"]),
                int(ws.cluster_ids["seed_primary"]),
                int(ws.cluster_ids["seed_fallback"]),
                int(ws.cluster_ids["seed_invalid"]),
            ),
        )
        ws.conn.commit()

        payload = IdentityV31QueryService(ws.root).load_report_context(
            base_run_id=None,
            assign_parameters=AssignParameters(assign_source="all"),
        )
        actual_cluster_order = [cluster.cluster_id for cluster in payload.clusters]
        assert actual_cluster_order == [
            ws.cluster_ids["seed_fallback"],
            ws.cluster_ids["seed_invalid"],
            ws.cluster_ids["seed_primary"],
            ws.cluster_ids["pending_promotable"],
        ]
    finally:
        ws.close()


def test_query_service_uses_selected_snapshot_profile_embedding_model_key(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "query-snapshot-profile-binding")
    try:
        default_payload = IdentityV31QueryService(ws.root).load_report_context(
            base_run_id=None,
            assign_parameters=AssignParameters(),
        )
        latest_payload = IdentityV31QueryService(ws.root).load_report_context(
            base_run_id=ws.latest_non_target_run_id,
            assign_parameters=AssignParameters(),
        )
        assert default_payload.snapshot.embedding_model_key == ws.selected_snapshot_embedding_model_key
        assert default_payload.snapshot.embedding_model_key != ws.latest_visible_profile_embedding_model_key
        assert latest_payload.snapshot.embedding_model_key == ws.latest_visible_profile_embedding_model_key
        assert latest_payload.snapshot.embedding_model_key != ws.selected_snapshot_embedding_model_key
    finally:
        ws.close()


def test_query_service_selects_only_matching_normalized_embedding_row(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "query-embedding-filter")
    try:
        payload = IdentityV31QueryService(ws.root).load_report_context(
            base_run_id=None,
            assign_parameters=AssignParameters(),
        )
        probe = next(
            item for item in payload.candidate_observations if item.observation_id == ws.observation_ids["embedding_probe"]
        )
        assert probe.embedding_vector is not None
        assert probe.embedding_dim == ws.embedding_probe_expected_dim
        assert probe.embedding_model_key == ws.embedding_probe_expected_model_key
    finally:
        ws.close()


def test_query_service_assign_source_variants_are_exact_and_all_is_union(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "query-source-variants")
    try:
        service = IdentityV31QueryService(ws.root)
        payload_all = service.load_report_context(base_run_id=None, assign_parameters=AssignParameters(assign_source="all"))
        payload_review_pending = service.load_report_context(
            base_run_id=None,
            assign_parameters=AssignParameters(assign_source="review_pending"),
        )
        payload_attachment = service.load_report_context(
            base_run_id=None,
            assign_parameters=AssignParameters(assign_source="attachment"),
        )

        all_ids = _candidate_ids(payload_all)
        review_pending_ids = _candidate_ids(payload_review_pending)
        attachment_ids = _candidate_ids(payload_attachment)
        review_pending_id_list = [item.observation_id for item in payload_review_pending.candidate_observations]
        attachment_id_list = [item.observation_id for item in payload_attachment.candidate_observations]

        assert review_pending_ids == ws.expected_candidate_ids_by_run_id_and_source[(ws.base_run_id, "review_pending")]
        assert attachment_ids == ws.expected_candidate_ids_by_run_id_and_source[(ws.base_run_id, "attachment")]
        assert all_ids == ws.expected_candidate_ids_by_run_id_and_source[(ws.base_run_id, "all")]
        assert all_ids == review_pending_ids | attachment_ids
        assert review_pending_id_list == sorted(review_pending_id_list)
        assert attachment_id_list == sorted(attachment_id_list)
        assert review_pending_id_list == sorted(
            ws.expected_candidate_ids_by_run_id_and_source[(ws.base_run_id, "review_pending")]
        )
        assert attachment_id_list == sorted(ws.expected_candidate_ids_by_run_id_and_source[(ws.base_run_id, "attachment")])

        # candidate 输出顺序稳定：all 结果按 observation_id 升序输出。
        all_id_list = [item.observation_id for item in payload_all.candidate_observations]
        assert all_id_list == sorted(all_id_list)

        # overlap observation 去重后只保留 review_pending_retained，且在 all 列表顺序中稳定可定位。
        overlap_id = ws.observation_ids["pending_attachment_overlap"]
        overlap_positions = [idx for idx, item in enumerate(payload_all.candidate_observations) if item.observation_id == overlap_id]
        assert overlap_positions == [all_id_list.index(overlap_id)]
        assert payload_all.candidate_observations[overlap_positions[0]].source_kind == "review_pending_retained"
    finally:
        ws.close()


@pytest.mark.parametrize(
    "params,pattern",
    [
        (AssignParameters(assign_source="bogus"), "不支持的 assign_source: bogus"),
        (AssignParameters(top_k=0), "top_k 必须大于 0"),
        (AssignParameters(auto_max_distance=0.5, review_max_distance=0.4), "auto_max_distance 不能大于 review_max_distance"),
        (AssignParameters(min_margin=-0.1), "min_margin 不能小于 0"),
    ],
)
def test_query_service_validates_assign_parameters_at_entry(
    tmp_path: Path,
    params: AssignParameters,
    pattern: str,
) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "query-assign-params-validation")
    try:
        with pytest.raises(ValueError, match=pattern):
            IdentityV31QueryService(ws.root).load_report_context(base_run_id=None, assign_parameters=params)
    finally:
        ws.close()


def test_query_service_keeps_missing_embedding_and_dimension_mismatch_candidates_in_context(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "query-missing-and-dim")
    try:
        payload = IdentityV31QueryService(ws.root).load_report_context(
            base_run_id=None,
            assign_parameters=AssignParameters(assign_source="all"),
        )
        by_observation_id = {item.observation_id: item for item in payload.candidate_observations}

        missing = by_observation_id[ws.observation_ids["attachment_missing_embedding"]]
        mismatch = by_observation_id[ws.observation_ids["attachment_dim_mismatch"]]

        assert missing.embedding_vector is None
        assert missing.embedding_dim is None
        assert mismatch.embedding_vector is not None
        assert mismatch.embedding_dim == 3
    finally:
        ws.close()


def test_query_context_exposes_non_rejected_members_and_source_candidate_sets(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "query-context-maps")
    try:
        payload = IdentityV31QueryService(ws.root).load_report_context(
            base_run_id=None,
            assign_parameters=AssignParameters(assign_source="all"),
        )

        assert payload.non_rejected_member_observation_ids_by_cluster
        assert payload.source_candidate_observation_ids
        assert payload.source_candidate_observation_ids["review_pending_retained"] == ws.expected_candidate_ids_by_run_id_and_source[
            (ws.base_run_id, "review_pending")
        ]
        assert payload.source_candidate_observation_ids["attachment"] == ws.expected_candidate_ids_by_run_id_and_source[
            (ws.base_run_id, "attachment")
        ]
        assert payload.source_candidate_observation_ids["all"] == ws.expected_candidate_ids_by_run_id_and_source[
            (ws.base_run_id, "all")
        ]

        cluster = payload.clusters_by_id[ws.cluster_ids["pending_promotable"]]
        expected_non_rejected_ids = {
            member.observation_id for member in cluster.members if member.decision_status != "rejected"
        }
        assert (
            payload.non_rejected_member_observation_ids_by_cluster[ws.cluster_ids["pending_promotable"]]
            == expected_non_rejected_ids
        )
    finally:
        ws.close()
