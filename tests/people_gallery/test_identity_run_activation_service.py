from pathlib import Path

import pytest

from hikbox_pictures.db.connection import connect_db

from .fixtures_identity_v3_1 import build_identity_phase1_workspace


def _list_published_person_ids(*, ws, run_id: int) -> list[int]:  # type: ignore[no-untyped-def]
    rows = ws.conn.execute(
        """
        SELECT person_id
        FROM identity_cluster_resolution
        WHERE source_run_id = ?
          AND publish_state = 'published'
          AND person_id IS NOT NULL
        ORDER BY person_id ASC
        """,
        (int(run_id),),
    ).fetchall()
    return [int(row["person_id"]) for row in rows]


def test_activate_run_switches_owner_and_live_assignment_seed_prototype_ann(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "activate-run")
    try:
        ws.seed_materialize_candidate_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        service = ws.new_cluster_run_service()
        run_a = service.execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=True,
        )
        ws.new_cluster_prepare_service().prepare_run(run_id=int(run_a["run_id"]))
        ws.new_run_activation_service().activate_run(run_id=int(run_a["run_id"]))

        first_owner = ws.get_cluster_run(int(run_a["run_id"]))
        first_live_count = ws.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM person_cluster_origin
            WHERE source_run_id = ?
              AND active = 1
            """,
            (int(run_a["run_id"]),),
        ).fetchone()
        first_live_assignments = ws.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM person_face_assignment
            WHERE active = 1
              AND source_run_id = ?
            """,
            (int(run_a["run_id"]),),
        ).fetchone()
        first_live_seeds = ws.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM person_trusted_sample
            WHERE active = 1
              AND source_run_id = ?
            """,
            (int(run_a["run_id"]),),
        ).fetchone()
        assert bool(first_owner["is_materialization_owner"]) is True
        assert int(first_live_count["c"]) >= 1
        assert int(first_live_assignments["c"]) >= 1
        assert int(first_live_seeds["c"]) >= 1
        assert ws.get_live_prototype_owner_run_id() == int(run_a["run_id"])
        assert ws.get_live_ann_owner_run_id() == int(run_a["run_id"])
        assert sorted(ws.get_live_ann_person_ids()) == _list_published_person_ids(
            ws=ws,
            run_id=int(run_a["run_id"]),
        )

        run_b = service.execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=int(run_a["run_id"]),
            select_as_review_target=True,
        )
        ws.new_cluster_prepare_service().prepare_run(run_id=int(run_b["run_id"]))
        ws.new_run_activation_service().activate_run(run_id=int(run_b["run_id"]))

        old_live_count = ws.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM person_cluster_origin
            WHERE source_run_id = ?
              AND active = 1
            """,
            (int(run_a["run_id"]),),
        ).fetchone()
        old_live_active_people = ws.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM person AS p
            JOIN person_cluster_origin AS pco ON pco.person_id = p.id
            WHERE pco.source_run_id = ?
              AND pco.active = 0
              AND p.status = 'active'
              AND p.ignored = 0
            """,
            (int(run_a["run_id"]),),
        ).fetchone()
        assert bool(ws.get_cluster_run(int(run_a["run_id"]))["is_materialization_owner"]) is False
        assert bool(ws.get_cluster_run(int(run_b["run_id"]))["is_materialization_owner"]) is True
        assert int(old_live_count["c"]) == 0
        assert int(old_live_active_people["c"]) == 0
        assert ws.get_live_prototype_owner_run_id() == int(run_b["run_id"])
        assert ws.get_live_ann_owner_run_id() == int(run_b["run_id"])
        assert sorted(ws.get_live_ann_person_ids()) == _list_published_person_ids(
            ws=ws,
            run_id=int(run_b["run_id"]),
        )
    finally:
        ws.close()


def test_activate_run_rejects_checksum_mismatch_and_rolls_back_live_side_effects(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "activate-run-checksum")
    try:
        ws.seed_materialize_candidate_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        run = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=True,
        )
        ws.new_cluster_prepare_service().prepare_run(run_id=int(run["run_id"]))
        ws.corrupt_prepared_ann_artifact(run_id=int(run["run_id"]))

        with pytest.raises(ValueError, match="checksum"):
            ws.new_run_activation_service().activate_run(run_id=int(run["run_id"]))

        owner = ws.conn.execute(
            "SELECT COUNT(*) AS c FROM identity_cluster_run WHERE is_materialization_owner = 1"
        ).fetchone()
        live_people = ws.conn.execute(
            "SELECT COUNT(*) AS c FROM person_cluster_origin WHERE source_run_id = ? AND active = 1",
            (int(run["run_id"]),),
        ).fetchone()
        assert int(owner["c"]) == 0
        assert int(live_people["c"]) == 0
        assert ws.get_live_ann_owner_run_id() is None
    finally:
        ws.close()


def test_activate_run_marks_publish_failed_and_no_false_published_when_publish_stage_errors(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "activate-run-publish-failed")
    try:
        ws.seed_materialize_candidate_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        run = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=True,
        )
        ws.new_cluster_prepare_service().prepare_run(run_id=int(run["run_id"]))
        ws.stub_publish_stage_failure(run_id=int(run["run_id"]), reason="person_publish_plan_invalid")

        with pytest.raises(RuntimeError, match="publish"):
            ws.new_run_activation_service().activate_run(run_id=int(run["run_id"]))

        failed = ws.list_cluster_resolutions(run_id=int(run["run_id"]))
        assert any(item["publish_state"] == "publish_failed" for item in failed)
        assert all(item["publish_state"] != "published" for item in failed if item["publish_state"] == "publish_failed")
        assert all(item["publish_failure_reason"] for item in failed if item["publish_state"] == "publish_failed")
        verify_conn = connect_db(ws.paths.db_path)
        try:
            persisted_failed = verify_conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM identity_cluster_resolution AS r
                JOIN identity_cluster AS c ON c.id = r.cluster_id
                WHERE c.run_id = ?
                  AND r.publish_state = 'publish_failed'
                  AND COALESCE(r.publish_failure_reason, '') <> ''
                """,
                (int(run["run_id"]),),
            ).fetchone()
            assert persisted_failed is not None
            assert int(persisted_failed["c"]) >= 1
        finally:
            verify_conn.close()
        run_live_people = ws.conn.execute(
            "SELECT COUNT(*) AS c FROM person_cluster_origin WHERE source_run_id = ? AND active = 1",
            (int(run["run_id"]),),
        ).fetchone()
        assert int(run_live_people["c"]) == 0
    finally:
        ws.close()


def test_activate_run_failure_keeps_previous_owner_live_state_unchanged(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "activate-run-keep-owner")
    try:
        ws.seed_materialize_candidate_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        service = ws.new_cluster_run_service()

        run_a = service.execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=True,
        )
        ws.new_cluster_prepare_service().prepare_run(run_id=int(run_a["run_id"]))
        ws.new_run_activation_service().activate_run(run_id=int(run_a["run_id"]))

        run_b = service.execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=int(run_a["run_id"]),
            select_as_review_target=True,
        )
        ws.new_cluster_prepare_service().prepare_run(run_id=int(run_b["run_id"]))
        ws.corrupt_prepared_ann_artifact(run_id=int(run_b["run_id"]))
        with pytest.raises(ValueError, match="checksum"):
            ws.new_run_activation_service().activate_run(run_id=int(run_b["run_id"]))

        assert bool(ws.get_cluster_run(int(run_a["run_id"]))["is_materialization_owner"]) is True
        assert bool(ws.get_cluster_run(int(run_b["run_id"]))["is_materialization_owner"]) is False
        assert ws.get_live_ann_owner_run_id() == int(run_a["run_id"])
        assert ws.get_live_prototype_owner_run_id() == int(run_a["run_id"])
        run_b_live_people = ws.conn.execute(
            "SELECT COUNT(*) AS c FROM person_cluster_origin WHERE source_run_id = ? AND active = 1",
            (int(run_b["run_id"]),),
        ).fetchone()
        assert int(run_b_live_people["c"]) == 0
    finally:
        ws.close()


def test_activate_run_ann_meta_write_failure_after_swap_keeps_previous_owner_live_state_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "activate-run-ann-meta-write-failed")
    try:
        ws.seed_materialize_candidate_case()
        snapshot = ws.new_observation_snapshot_service().build_snapshot(
            observation_profile_id=ws.observation_profile_id,
            candidate_knn_limit=24,
        )
        service = ws.new_cluster_run_service()

        run_a = service.execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=True,
        )
        ws.new_cluster_prepare_service().prepare_run(run_id=int(run_a["run_id"]))
        ws.new_run_activation_service().activate_run(run_id=int(run_a["run_id"]))
        live_checksum_before = ws.get_live_ann_checksum()
        live_person_ids_before = ws.get_live_ann_person_ids()

        run_b = service.execute_run(
            observation_snapshot_id=int(snapshot["snapshot_id"]),
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=int(run_a["run_id"]),
            select_as_review_target=True,
        )
        ws.new_cluster_prepare_service().prepare_run(run_id=int(run_b["run_id"]))

        meta_path = (ws.paths.artifacts_dir / "ann" / "prototype_index.npz.meta.json").resolve()
        original_write_text = Path.write_text

        def _write_text_fail_on_live_meta(  # type: ignore[no-untyped-def]
            self: Path,
            text: str,
            *args: object,
            **kwargs: object,
        ) -> int:
            if self.resolve() == meta_path:
                raise RuntimeError("inject_ann_meta_write_failure_after_swap")
            return original_write_text(self, text, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", _write_text_fail_on_live_meta)
        with pytest.raises(RuntimeError, match="inject_ann_meta_write_failure_after_swap"):
            ws.new_run_activation_service().activate_run(run_id=int(run_b["run_id"]))

        assert bool(ws.get_cluster_run(int(run_a["run_id"]))["is_materialization_owner"]) is True
        assert bool(ws.get_cluster_run(int(run_b["run_id"]))["is_materialization_owner"]) is False
        assert ws.get_live_ann_owner_run_id() == int(run_a["run_id"])
        assert ws.get_live_prototype_owner_run_id() == int(run_a["run_id"])
        assert ws.get_live_ann_checksum() == live_checksum_before
        assert ws.get_live_ann_person_ids() == live_person_ids_before
        run_b_live_people = ws.conn.execute(
            "SELECT COUNT(*) AS c FROM person_cluster_origin WHERE source_run_id = ? AND active = 1",
            (int(run_b["run_id"]),),
        ).fetchone()
        assert int(run_b_live_people["c"]) == 0
    finally:
        ws.close()
