from __future__ import annotations

import sqlite3
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from .fixtures_identity_v3_1 import build_identity_phase1_workspace

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "rebuild_identities_v3.py"
_SCRIPT_SPEC = spec_from_file_location("task9_rebuild_identities_v3_script", _SCRIPT_PATH)
if _SCRIPT_SPEC is None or _SCRIPT_SPEC.loader is None:
    raise RuntimeError(f"无法加载重建脚本: {_SCRIPT_PATH}")
_SCRIPT_MODULE = module_from_spec(_SCRIPT_SPEC)
sys.modules[_SCRIPT_SPEC.name] = _SCRIPT_MODULE
_SCRIPT_SPEC.loader.exec_module(_SCRIPT_MODULE)
rebuild_main = _SCRIPT_MODULE.main


def _count(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return int(row["c"])


def test_rebuild_wrapper_runs_snapshot_and_rerun_in_v3_1_main_chain(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "task9-rebuild-wrapper")
    try:
        ws.seed_materialize_candidate_case()

        rc = rebuild_main(["--workspace", str(ws.root)])
        assert rc == 0

        run_row = ws.conn.execute(
            """
            SELECT id, observation_snapshot_id, run_status, is_review_target
            FROM identity_cluster_run
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        assert run_row is not None
        run_id = int(run_row["id"])

        assert int(run_row["observation_snapshot_id"]) > 0
        assert str(run_row["run_status"]) == "succeeded"
        assert bool(run_row["is_review_target"]) is True

        assert _count(ws.conn, "SELECT COUNT(*) AS c FROM identity_observation_snapshot") >= 1
        assert _count(
            ws.conn,
            "SELECT COUNT(*) AS c FROM identity_cluster WHERE run_id = ?",
            (run_id,),
        ) > 0
        assert _count(
            ws.conn,
            """
            SELECT COUNT(*) AS c
            FROM identity_cluster_member AS m
            JOIN identity_cluster AS c ON c.id = m.cluster_id
            WHERE c.run_id = ?
            """,
            (run_id,),
        ) > 0
        assert _count(
            ws.conn,
            """
            SELECT COUNT(*) AS c
            FROM identity_cluster_resolution AS r
            JOIN identity_cluster AS c ON c.id = r.cluster_id
            WHERE c.run_id = ?
            """,
            (run_id,),
        ) > 0
    finally:
        ws.close()


def test_rebuild_wrapper_rejects_dry_run_with_exit_code_2(tmp_path: Path) -> None:
    ws = build_identity_phase1_workspace(tmp_path / "task9-rebuild-dry-run")
    try:
        ws.seed_materialize_candidate_case()
        snapshot_count_before = _count(ws.conn, "SELECT COUNT(*) AS c FROM identity_observation_snapshot")
        run_count_before = _count(ws.conn, "SELECT COUNT(*) AS c FROM identity_cluster_run")

        rc = rebuild_main(["--workspace", str(ws.root), "--dry-run"])
        assert rc == 2

        assert _count(ws.conn, "SELECT COUNT(*) AS c FROM identity_observation_snapshot") == snapshot_count_before
        assert _count(ws.conn, "SELECT COUNT(*) AS c FROM identity_cluster_run") == run_count_before
    finally:
        ws.close()
