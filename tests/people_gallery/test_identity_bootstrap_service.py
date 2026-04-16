from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from hikbox_pictures.ann import AnnIndexStore
from hikbox_pictures.repositories.identity_repo import IdentityRepo
from hikbox_pictures.services.identity_bootstrap_service import IdentityBootstrapService

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace_identity_bootstrap", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_identity_seed_workspace = _MODULE.build_identity_seed_workspace


def test_bootstrap_persists_edge_reject_counts_and_cluster_diagnostic(tmp_path: Path) -> None:
    ws = build_identity_seed_workspace(tmp_path)
    try:
        ws.seed_edge_rule_challenge_case()
        result = ws.new_bootstrap_service().run_bootstrap(profile_id=ws.profile_id)

        assert int(result["materialized_cluster_count"]) >= 1
        assert int(result["review_pending_cluster_count"]) >= 1
        assert int(result["discarded_cluster_count"]) >= 0
        summary_edge_rejects = result["edge_reject_counts"]
        assert {"not_mutual", "distance_recheck_failed", "photo_conflict"}.issubset(summary_edge_rejects.keys())
        assert int(summary_edge_rejects["not_mutual"]) >= 1
        assert int(summary_edge_rejects["distance_recheck_failed"]) >= 1
        assert int(summary_edge_rejects["photo_conflict"]) >= 1

        row = ws.conn.execute(
            """
            SELECT diagnostic_json
            FROM auto_cluster
            WHERE cluster_status = 'review_pending'
            ORDER BY id ASC
            LIMIT 1
            """
        ).fetchone()
        assert row is not None
        diagnostic = ws.parse_json(row["diagnostic_json"])
        assert "cluster_size" in diagnostic
        assert "distinct_photo_count" in diagnostic
        assert "selected_seed_count" in diagnostic
        assert "pre_dedup_seed_candidate_count" in diagnostic
        assert "quality_distribution" in diagnostic
        assert "external_margin" in diagnostic
        assert "edge_reject_counts" in diagnostic
        assert "dedup_drop_counts" in diagnostic
        assert "reject_reason" in diagnostic
        assert int(diagnostic["pre_dedup_seed_candidate_count"]) >= int(diagnostic["selected_seed_count"])

        edge_reject_counts = diagnostic["edge_reject_counts"]
        assert {"not_mutual", "distance_recheck_failed", "photo_conflict"}.issubset(edge_reject_counts.keys())
        assert int(edge_reject_counts.get("not_mutual", 0)) >= 1
        assert int(edge_reject_counts.get("distance_recheck_failed", 0)) >= 1
        assert int(edge_reject_counts.get("photo_conflict", 0)) >= 1

        dedup_drop = diagnostic["dedup_drop_counts"]
        assert "exact_duplicate" in dedup_drop
        assert "burst_duplicate" in dedup_drop
    finally:
        ws.close()


def test_bootstrap_materialize_transaction_closure_creates_person_assignment_and_trusted_samples(tmp_path: Path) -> None:
    ws = build_identity_seed_workspace(tmp_path)
    try:
        ws.seed_materialize_happy_case()
        ws.new_bootstrap_service().run_bootstrap(profile_id=ws.profile_id)

        person = ws.conn.execute(
            """
            SELECT id, cover_observation_id, origin_cluster_id
            FROM person
            WHERE origin_cluster_id IS NOT NULL
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        assert person is not None
        person_id = int(person["id"])
        cluster_id = int(person["origin_cluster_id"])
        assert int(person["cover_observation_id"]) > 0

        cluster = ws.conn.execute(
            """
            SELECT cluster_status, resolved_person_id, diagnostic_json
            FROM auto_cluster
            WHERE id = ?
            """,
            (cluster_id,),
        ).fetchone()
        assert cluster is not None
        assert cluster["cluster_status"] == "materialized"
        assert int(cluster["resolved_person_id"]) == person_id
        cluster_diagnostic = ws.parse_json(cluster["diagnostic_json"])
        assert cluster_diagnostic["decision_kind"] == "materialized"

        assignments = ws.conn.execute(
            """
            SELECT assignment_source, threshold_profile_id, diagnostic_json
            FROM person_face_assignment
            WHERE person_id = ?
              AND active = 1
            ORDER BY id ASC
            """,
            (person_id,),
        ).fetchall()
        assert len(assignments) >= 3
        for row in assignments:
            assert row["assignment_source"] == "bootstrap"
            assert int(row["threshold_profile_id"]) == ws.profile_id
            diagnostic = ws.parse_json(row["diagnostic_json"])
            assert diagnostic["decision_kind"] == "bootstrap_materialize"
            assert int(diagnostic["auto_cluster_id"]) == cluster_id

        trusted_samples = ws.conn.execute(
            """
            SELECT threshold_profile_id, source_auto_cluster_id, trust_source
            FROM person_trusted_sample
            WHERE person_id = ?
              AND active = 1
            ORDER BY id ASC
            """,
            (person_id,),
        ).fetchall()
        assert len(trusted_samples) >= 3
        for row in trusted_samples:
            assert int(row["threshold_profile_id"]) == ws.profile_id
            assert int(row["source_auto_cluster_id"]) == cluster_id
            assert row["trust_source"] == "bootstrap_seed"
    finally:
        ws.close()


def test_bootstrap_dedup_seed_insufficient_does_not_create_person(tmp_path: Path) -> None:
    ws = build_identity_seed_workspace(tmp_path)
    try:
        ws.seed_bootstrap_dedup_collision_case()
        ws.new_bootstrap_service().run_bootstrap(profile_id=ws.profile_id)

        seeded_people = ws.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM person
            WHERE origin_cluster_id IS NOT NULL
            """
        ).fetchone()
        assert seeded_people is not None
        assert int(seeded_people["c"]) == 0

        row = ws.conn.execute(
            """
            SELECT cluster_status, diagnostic_json
            FROM auto_cluster
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        assert row is not None
        assert row["cluster_status"] == "review_pending"
        diagnostic = ws.parse_json(row["diagnostic_json"])
        assert diagnostic["reject_reason"] == "seed_insufficient_after_dedup"
        assert int(diagnostic["pre_dedup_seed_candidate_count"]) > int(diagnostic["selected_seed_count"])
        dedup_drop = diagnostic["dedup_drop_counts"]
        assert int(dedup_drop.get("exact_duplicate", 0)) >= 1
        assert int(dedup_drop.get("burst_duplicate", 0)) >= 1
    finally:
        ws.close()


def test_bootstrap_ann_sync_failure_compensates_to_review_pending_without_half_state(tmp_path: Path) -> None:
    ws = build_identity_seed_workspace(tmp_path)
    try:
        ws.seed_materialize_happy_case()
        ws.fail_next_ann_sync()
        ws.new_bootstrap_service().run_bootstrap(profile_id=ws.profile_id)

        seeded_people = ws.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM person
            WHERE origin_cluster_id IS NOT NULL
            """
        ).fetchone()
        assert seeded_people is not None
        assert int(seeded_people["c"]) == 0

        assignment_count = ws.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM person_face_assignment
            WHERE assignment_source = 'bootstrap'
            """
        ).fetchone()
        assert assignment_count is not None
        assert int(assignment_count["c"]) == 0

        trusted_count = ws.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM person_trusted_sample
            """
        ).fetchone()
        assert trusted_count is not None
        assert int(trusted_count["c"]) == 0

        prototype_count = ws.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM person_prototype
            WHERE active = 1
            """
        ).fetchone()
        assert prototype_count is not None
        assert int(prototype_count["c"]) == 0

        row = ws.conn.execute(
            """
            SELECT cluster_status, resolved_person_id, diagnostic_json
            FROM auto_cluster
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        assert row is not None
        assert row["cluster_status"] == "review_pending"
        assert row["resolved_person_id"] is None
        diagnostic = ws.parse_json(row["diagnostic_json"])
        assert diagnostic["reject_reason"] == "artifact_rebuild_failed"
    finally:
        ws.close()


def test_bootstrap_reports_total_completed_and_percent(tmp_path: Path) -> None:
    ws = build_identity_seed_workspace(tmp_path)
    try:
        ws.seed_edge_rule_challenge_case()
        events: list[dict[str, object]] = []
        service = IdentityBootstrapService(
            ws.conn,
            identity_repo=IdentityRepo(ws.conn),
            person_repo=ws.person_repo,
            prototype_service=ws.new_bootstrap_service().prototype_service,
            progress_reporter=events.append,
        )

        service.run_bootstrap(profile_id=ws.profile_id)

        distance_events = [
            item
            for item in events
            if item.get("phase") == "bootstrap_materialize" and item.get("subphase") == "distance_matrix"
        ]
        persist_events = [
            item
            for item in events
            if item.get("phase") == "bootstrap_materialize" and item.get("subphase") == "persist_clusters"
        ]
        assert distance_events
        assert persist_events
        final_persist = persist_events[-1]
        assert int(final_persist["total_count"]) >= 1
        assert int(final_persist["completed_count"]) == int(final_persist["total_count"])
        assert float(final_persist["percent"]) == 100.0
    finally:
        ws.close()
