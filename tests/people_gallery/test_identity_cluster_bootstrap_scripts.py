from __future__ import annotations

import sqlite3
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType

from .fixtures_identity_v3_1 import build_identity_phase1_workspace

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
_WORKTREE_SRC = (Path(__file__).resolve().parents[2] / "src").resolve()


def _load_script_main(script_name: str, module_name: str):  # type: ignore[no-untyped-def]
    script_path = _SCRIPTS_DIR / script_name
    spec = spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载脚本: {script_path}")
    module = module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.main


def _load_script_module(script_name: str, module_name: str) -> ModuleType:
    script_path = _SCRIPTS_DIR / script_name
    spec = spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载脚本: {script_path}")
    module = module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _count(conn: sqlite3.Connection, sql: str, params: tuple[int, ...]) -> int:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return int(row["c"])


def _create_second_cluster_profile(ws) -> int:  # type: ignore[no-untyped-def]
    cursor = ws.conn.execute(
        """
        INSERT INTO identity_cluster_profile(
            profile_name,
            profile_version,
            discovery_knn_k,
            density_min_samples,
            raw_cluster_min_size,
            raw_cluster_min_distinct_photo_count,
            intra_photo_conflict_policy_version,
            anchor_core_min_support_ratio,
            anchor_core_radius_quantile,
            core_min_support_ratio,
            boundary_min_support_ratio,
            boundary_radius_multiplier,
            split_min_component_size,
            split_min_medoid_gap,
            existence_min_retained_count,
            existence_min_anchor_core_count,
            existence_min_distinct_photo_count,
            existence_min_support_ratio_p50,
            existence_max_intra_photo_conflict_ratio,
            attachment_max_distance,
            attachment_candidate_knn_k,
            attachment_min_support_ratio,
            attachment_min_separation_gap,
            materialize_min_anchor_core_count,
            materialize_min_distinct_photo_count,
            materialize_max_compactness_p90,
            materialize_min_separation_gap,
            materialize_max_boundary_ratio,
            trusted_seed_min_quality,
            trusted_seed_min_count,
            trusted_seed_max_count,
            trusted_seed_allow_boundary,
            active,
            activated_at
        )
        SELECT
            profile_name || '.variant',
            profile_version || '.variant',
            discovery_knn_k,
            density_min_samples,
            raw_cluster_min_size,
            raw_cluster_min_distinct_photo_count,
            intra_photo_conflict_policy_version,
            anchor_core_min_support_ratio,
            anchor_core_radius_quantile,
            core_min_support_ratio,
            boundary_min_support_ratio,
            boundary_radius_multiplier,
            split_min_component_size,
            split_min_medoid_gap,
            existence_min_retained_count,
            existence_min_anchor_core_count,
            existence_min_distinct_photo_count,
            existence_min_support_ratio_p50,
            existence_max_intra_photo_conflict_ratio,
            attachment_max_distance,
            attachment_candidate_knn_k,
            attachment_min_support_ratio,
            attachment_min_separation_gap,
            materialize_min_anchor_core_count,
            materialize_min_distinct_photo_count,
            materialize_max_compactness_p90,
            materialize_min_separation_gap,
            materialize_max_boundary_ratio,
            trusted_seed_min_quality,
            trusted_seed_min_count,
            trusted_seed_max_count,
            trusted_seed_allow_boundary,
            0,
            NULL
        FROM identity_cluster_profile
        WHERE id = ?
        """,
        (int(ws.cluster_profile_id),),
    )
    ws.conn.commit()
    return int(cursor.lastrowid)


def test_scripts_build_snapshot_rerun_history_and_select_review_target(tmp_path: Path) -> None:
    build_snapshot_main = _load_script_main(
        "build_identity_observation_snapshot.py",
        "task6_build_identity_observation_snapshot_script",
    )
    rerun_main = _load_script_main(
        "rerun_identity_cluster_run.py",
        "task6_rerun_identity_cluster_run_script",
    )
    select_main = _load_script_main(
        "select_identity_cluster_run.py",
        "task6_select_identity_cluster_run_script",
    )

    ws = build_identity_phase1_workspace(tmp_path / "task6-scripts-rerun-select")
    try:
        ws.seed_materialize_candidate_case()
        profile_b_id = _create_second_cluster_profile(ws)

        rc_snapshot = build_snapshot_main(["--workspace", str(ws.root), "--candidate-knn-limit", "24"])
        assert rc_snapshot == 0

        snapshot_row = ws.conn.execute(
            """
            SELECT id
            FROM identity_observation_snapshot
            WHERE status = 'succeeded'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        assert snapshot_row is not None
        snapshot_id = int(snapshot_row["id"])

        rc_rerun_a = rerun_main(
            [
                "--workspace",
                str(ws.root),
                "--snapshot-id",
                str(snapshot_id),
                "--cluster-profile-id",
                str(ws.cluster_profile_id),
            ]
        )
        assert rc_rerun_a == 0

        run_a = ws.conn.execute(
            """
            SELECT *
            FROM identity_cluster_run
            WHERE observation_snapshot_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (snapshot_id,),
        ).fetchone()
        assert run_a is not None
        run_a_id = int(run_a["id"])
        assert int(run_a["observation_snapshot_id"]) == snapshot_id
        assert int(run_a["cluster_profile_id"]) == int(ws.cluster_profile_id)
        assert str(run_a["run_status"]) == "succeeded"

        rc_rerun_b = rerun_main(
            [
                "--workspace",
                str(ws.root),
                "--snapshot-id",
                str(snapshot_id),
                "--cluster-profile-id",
                str(profile_b_id),
                "--supersedes-run-id",
                str(run_a_id),
            ]
        )
        assert rc_rerun_b == 0

        run_rows = ws.conn.execute(
            """
            SELECT id, cluster_profile_id, run_status, is_review_target
            FROM identity_cluster_run
            WHERE observation_snapshot_id = ?
            ORDER BY id ASC
            """,
            (snapshot_id,),
        ).fetchall()
        assert len(run_rows) == 2
        run_b_id = int(run_rows[1]["id"])
        assert run_b_id != run_a_id
        assert int(run_rows[1]["cluster_profile_id"]) == profile_b_id
        assert str(run_rows[1]["run_status"]) == "succeeded"
        assert bool(run_rows[1]["is_review_target"]) is True

        for run_id in (run_a_id, run_b_id):
            assert (
                _count(
                    ws.conn,
                    """
                    SELECT COUNT(*) AS c
                    FROM identity_cluster
                    WHERE run_id = ?
                    """,
                    (int(run_id),),
                )
                > 0
            )
            assert (
                _count(
                    ws.conn,
                    """
                    SELECT COUNT(*) AS c
                    FROM identity_cluster_member AS m
                    JOIN identity_cluster AS c ON c.id = m.cluster_id
                    WHERE c.run_id = ?
                    """,
                    (int(run_id),),
                )
                > 0
            )
            assert (
                _count(
                    ws.conn,
                    """
                    SELECT COUNT(*) AS c
                    FROM identity_cluster_resolution AS r
                    JOIN identity_cluster AS c ON c.id = r.cluster_id
                    WHERE c.run_id = ?
                    """,
                    (int(run_id),),
                )
                > 0
            )

        rc_select = select_main(["--workspace", str(ws.root), "--run-id", str(run_a_id)])
        assert rc_select == 0
        selected = ws.get_cluster_run(run_a_id)
        unselected = ws.get_cluster_run(run_b_id)
        assert bool(selected["is_review_target"]) is True
        assert bool(unselected["is_review_target"]) is False
    finally:
        ws.close()


def test_activate_script_requires_prepare_then_activate_sets_owner(tmp_path: Path) -> None:
    build_snapshot_main = _load_script_main(
        "build_identity_observation_snapshot.py",
        "task6_build_identity_observation_snapshot_script_activate",
    )
    rerun_main = _load_script_main(
        "rerun_identity_cluster_run.py",
        "task6_rerun_identity_cluster_run_script_activate",
    )
    activate_main = _load_script_main(
        "activate_identity_cluster_run.py",
        "task6_activate_identity_cluster_run_script",
    )

    ws = build_identity_phase1_workspace(tmp_path / "task6-scripts-activate")
    try:
        ws.seed_materialize_candidate_case()

        rc_snapshot = build_snapshot_main(["--workspace", str(ws.root), "--candidate-knn-limit", "24"])
        assert rc_snapshot == 0
        snapshot_id = int(
            ws.conn.execute(
                """
                SELECT id
                FROM identity_observation_snapshot
                WHERE status = 'succeeded'
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()["id"]
        )

        # 先构造一个 succeeded 但未 prepare 的 run，验证 activate 脚本会失败。
        run_unprepared = ws.new_cluster_run_service().execute_run(
            observation_snapshot_id=snapshot_id,
            cluster_profile_id=ws.cluster_profile_id,
            supersedes_run_id=None,
            select_as_review_target=False,
        )
        unprepared_id = int(run_unprepared["run_id"])
        rc_activate_fail = activate_main(["--workspace", str(ws.root), "--run-id", str(unprepared_id)])
        assert rc_activate_fail == 1
        owner_count = ws.conn.execute(
            "SELECT COUNT(*) AS c FROM identity_cluster_run WHERE is_materialization_owner = 1"
        ).fetchone()
        assert owner_count is not None
        assert int(owner_count["c"]) == 0

        # 通过 rerun 脚本触发 execute + prepare 后，再激活应成功。
        rc_rerun = rerun_main(
            [
                "--workspace",
                str(ws.root),
                "--snapshot-id",
                str(snapshot_id),
                "--cluster-profile-id",
                str(ws.cluster_profile_id),
                "--supersedes-run-id",
                str(unprepared_id),
            ]
        )
        assert rc_rerun == 0

        prepared_run_row = ws.conn.execute(
            """
            SELECT id
            FROM identity_cluster_run
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        assert prepared_run_row is not None
        prepared_run_id = int(prepared_run_row["id"])

        rc_activate_ok = activate_main(["--workspace", str(ws.root), "--run-id", str(prepared_run_id)])
        assert rc_activate_ok == 0
        activated = ws.conn.execute(
            """
            SELECT is_materialization_owner, activated_at
            FROM identity_cluster_run
            WHERE id = ?
            """,
            (int(prepared_run_id),),
        ).fetchone()
        assert activated is not None
        assert bool(activated["is_materialization_owner"]) is True
        assert activated["activated_at"] is not None
    finally:
        ws.close()


def test_rerun_script_no_select_review_target_keeps_new_run_unselected(tmp_path: Path) -> None:
    build_snapshot_main = _load_script_main(
        "build_identity_observation_snapshot.py",
        "task6_build_identity_observation_snapshot_script_no_select",
    )
    rerun_main = _load_script_main(
        "rerun_identity_cluster_run.py",
        "task6_rerun_identity_cluster_run_script_no_select",
    )

    ws = build_identity_phase1_workspace(tmp_path / "task6-scripts-rerun-no-select")
    try:
        ws.seed_materialize_candidate_case()
        rc_snapshot = build_snapshot_main(["--workspace", str(ws.root), "--candidate-knn-limit", "24"])
        assert rc_snapshot == 0
        snapshot_id = int(
            ws.conn.execute(
                """
                SELECT id
                FROM identity_observation_snapshot
                WHERE status = 'succeeded'
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()["id"]
        )

        rc_rerun_a = rerun_main(
            [
                "--workspace",
                str(ws.root),
                "--snapshot-id",
                str(snapshot_id),
                "--cluster-profile-id",
                str(ws.cluster_profile_id),
            ]
        )
        assert rc_rerun_a == 0
        run_a_id = int(
            ws.conn.execute(
                """
                SELECT id
                FROM identity_cluster_run
                WHERE observation_snapshot_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (snapshot_id,),
            ).fetchone()["id"]
        )

        rc_rerun_b = rerun_main(
            [
                "--workspace",
                str(ws.root),
                "--snapshot-id",
                str(snapshot_id),
                "--cluster-profile-id",
                str(ws.cluster_profile_id),
                "--supersedes-run-id",
                str(run_a_id),
                "--no-select-review-target",
            ]
        )
        assert rc_rerun_b == 0

        run_rows = ws.conn.execute(
            """
            SELECT id, is_review_target
            FROM identity_cluster_run
            WHERE observation_snapshot_id = ?
            ORDER BY id ASC
            """,
            (snapshot_id,),
        ).fetchall()
        assert len(run_rows) == 2
        assert bool(run_rows[0]["is_review_target"]) is True
        assert bool(run_rows[1]["is_review_target"]) is False
    finally:
        ws.close()


def test_rebuild_wrapper_rejects_dry_run_and_warns_legacy_options(tmp_path: Path, capsys) -> None:
    rebuild_main = _load_script_main(
        "rebuild_identities_v3.py",
        "task6_rebuild_identities_v3_script_wrapper",
    )

    ws = build_identity_phase1_workspace(tmp_path / "task6-scripts-rebuild-wrapper")
    try:
        ws.seed_materialize_candidate_case()
        snapshot_count_before = _count(ws.conn, "SELECT COUNT(*) AS c FROM identity_observation_snapshot", ())
        run_count_before = _count(ws.conn, "SELECT COUNT(*) AS c FROM identity_cluster_run", ())

        rc_dry_run = rebuild_main(["--workspace", str(ws.root), "--dry-run"])
        assert rc_dry_run == 2
        dry_run_captured = capsys.readouterr()
        assert "--dry-run 不受支持" in dry_run_captured.err
        assert _count(ws.conn, "SELECT COUNT(*) AS c FROM identity_observation_snapshot", ()) == snapshot_count_before
        assert _count(ws.conn, "SELECT COUNT(*) AS c FROM identity_cluster_run", ()) == run_count_before

        rc_rebuild = rebuild_main(
            [
                "--workspace",
                str(ws.root),
                "--backup-db",
                "--skip-ann-rebuild",
                "--threshold-profile",
                str(tmp_path / "legacy-threshold-profile.yaml"),
            ]
        )
        assert rc_rebuild == 0
        rebuild_captured = capsys.readouterr()
        assert "--backup-db 已废弃并被忽略" in rebuild_captured.err
        assert "--skip-ann-rebuild 已废弃并被忽略" in rebuild_captured.err
        assert "--threshold-profile 已废弃并被忽略" in rebuild_captured.err
        assert _count(ws.conn, "SELECT COUNT(*) AS c FROM identity_observation_snapshot", ()) > snapshot_count_before
        assert _count(ws.conn, "SELECT COUNT(*) AS c FROM identity_cluster_run", ()) > run_count_before
    finally:
        ws.close()


def test_scripts_pop_preloaded_foreign_orchestrator_module_before_import() -> None:
    orchestrator_module_name = "hikbox_pictures.services.identity_bootstrap_orchestrator"
    original_module = sys.modules.get(orchestrator_module_name)
    script_names = [
        "build_identity_observation_snapshot.py",
        "rerun_identity_cluster_run.py",
        "select_identity_cluster_run.py",
        "activate_identity_cluster_run.py",
        "rebuild_identities_v3.py",
    ]
    try:
        for idx, script_name in enumerate(script_names, start=1):
            foreign_module = ModuleType(orchestrator_module_name)
            foreign_module.__file__ = f"/tmp/foreign-src-{idx}/src/hikbox_pictures/services/identity_bootstrap_orchestrator.py"

            class _ForeignOrchestrator:
                pass

            foreign_module.IdentityBootstrapOrchestrator = _ForeignOrchestrator
            sys.modules[orchestrator_module_name] = foreign_module

            module = _load_script_module(script_name, f"task6_preloaded_foreign_orchestrator_{idx}")
            loaded_orchestrator_module = sys.modules[orchestrator_module_name]
            loaded_module_file = Path(getattr(loaded_orchestrator_module, "__file__", "")).resolve()
            assert str(loaded_module_file).startswith(str(_WORKTREE_SRC))
            assert module.IdentityBootstrapOrchestrator is loaded_orchestrator_module.IdentityBootstrapOrchestrator
    finally:
        if original_module is None:
            sys.modules.pop(orchestrator_module_name, None)
        else:
            sys.modules[orchestrator_module_name] = original_module
