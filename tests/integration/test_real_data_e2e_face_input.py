from __future__ import annotations

import json
from pathlib import Path

from tests.integration.test_productization_acceptance import (
    REPO_ROOT,
    _build_workspace_with_source,
    _fetchall,
    _fetchone,
    _read_cli_json_output,
    _run_cli_scan,
    python_bin,
)


REAL_E2E_DATASET_DIR = REPO_ROOT / "tests" / "data" / "e2e-face-input"
REAL_E2E_MANIFEST_PATH = REAL_E2E_DATASET_DIR / "manifest.json"


def test_real_dataset_scan_runs_full_pipeline_and_persists_results(
    tmp_path: Path,
    python_bin: Path,
) -> None:
    manifest = json.loads(REAL_E2E_MANIFEST_PATH.read_text(encoding="utf-8"))
    layout = _build_workspace_with_source(
        workspace_root=tmp_path / "workspace",
        external_root=tmp_path / "external",
        source_root=REAL_E2E_DATASET_DIR,
        label="real-e2e-face-input",
    )

    result = _run_cli_scan(
        python_bin,
        workspace_root=layout.workspace_root,
        args=["--json", "scan", "start-new", "--workspace", str(layout.workspace_root)],
    )
    payload = _read_cli_json_output(result.stdout)
    session_id = int(payload["data"]["session_id"])
    stage_rows = _fetchall(
        layout.library_db,
        "SELECT stage_status_json FROM scan_session_source WHERE scan_session_id=? ORDER BY id ASC",
        (session_id,),
    )
    photo_count, obs_count, assign_count, person_count, bbox_distinct, quality_distinct = _fetchone(
        layout.library_db,
        """
        SELECT
          (SELECT COUNT(*) FROM photo_asset WHERE asset_status='active'),
          (SELECT COUNT(*) FROM face_observation WHERE active=1),
          (SELECT COUNT(*) FROM person_face_assignment WHERE active=1),
          (SELECT COUNT(*) FROM person WHERE status='active' AND merged_into_person_id IS NULL),
          (
            SELECT COUNT(DISTINCT printf('%.6f,%.6f,%.6f,%.6f', bbox_x1, bbox_y1, bbox_x2, bbox_y2))
            FROM face_observation
            WHERE active=1
          ),
          (
            SELECT COUNT(DISTINCT printf('%.6f', quality_score))
            FROM face_observation
            WHERE active=1
          )
        """,
    )

    assert REAL_E2E_DATASET_DIR.exists()
    assert result.returncode == 0, result.stderr
    assert payload["ok"] is True
    assert payload["data"]["status"] == "completed"
    assert len(stage_rows) == 1
    assert json.loads(str(stage_rows[0][0])) == {
        "assignment": "done",
        "cluster": "done",
        "detect": "done",
        "discover": "done",
        "embed": "done",
        "metadata": "done",
    }
    assert int(photo_count) == int(manifest["raw_count"]) + int(manifest["group_count"])
    assert int(obs_count) > int(photo_count)
    assert int(assign_count) > 0
    assert int(person_count) >= 3
    assert int(bbox_distinct) > 1
    assert int(quality_distinct) > 1
