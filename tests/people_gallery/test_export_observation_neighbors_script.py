from __future__ import annotations

import json
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_FIXTURE_SPEC = spec_from_file_location("people_gallery_fixtures_workspace_export_observation_neighbors", _FIXTURE_PATH)
if _FIXTURE_SPEC is None or _FIXTURE_SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_FIXTURE_MODULE = module_from_spec(_FIXTURE_SPEC)
sys.modules[_FIXTURE_SPEC.name] = _FIXTURE_MODULE
_FIXTURE_SPEC.loader.exec_module(_FIXTURE_MODULE)
build_identity_seed_workspace = _FIXTURE_MODULE.build_identity_seed_workspace

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "export_observation_neighbors.py"
_SCRIPT_SPEC = spec_from_file_location("task7_export_observation_neighbors_script", _SCRIPT_PATH)
if _SCRIPT_SPEC is None or _SCRIPT_SPEC.loader is None:
    raise RuntimeError(f"无法加载导出脚本: {_SCRIPT_PATH}")
_SCRIPT_MODULE = module_from_spec(_SCRIPT_SPEC)
sys.modules[_SCRIPT_SPEC.name] = _SCRIPT_MODULE
_SCRIPT_SPEC.loader.exec_module(_SCRIPT_MODULE)
export_main = _SCRIPT_MODULE.main


def test_export_observation_neighbors_script_exports_bundle_and_rounds_html_numbers(
    tmp_path: Path,
) -> None:
    ws = build_identity_seed_workspace(tmp_path / "task7-export-observation-neighbors")
    output_root = tmp_path / "script-output"
    try:
        target_a = ws.insert_observation_with_embedding(
            vector=[0.00, 0.00, 0.00, 0.00],
            quality_score=0.9567,
            photo_label="tool-a",
        )
        neighbor_a1 = ws.insert_observation_with_embedding(
            vector=[0.01, 0.00, 0.00, 0.00],
            quality_score=0.9345,
            photo_label="tool-b",
        )
        neighbor_a2 = ws.insert_observation_with_embedding(
            vector=[0.03, 0.00, 0.00, 0.00],
            quality_score=0.9123,
            photo_label="tool-c",
        )
        target_b = ws.insert_observation_with_embedding(
            vector=[1.00, 0.00, 0.00, 0.00],
            quality_score=0.9876,
            photo_label="tool-d",
        )
        neighbor_b1 = ws.insert_observation_with_embedding(
            vector=[1.01, 0.00, 0.00, 0.00],
            quality_score=0.9765,
            photo_label="tool-e",
        )
        neighbor_b2 = ws.insert_observation_with_embedding(
            vector=[1.03, 0.00, 0.00, 0.00],
            quality_score=0.9654,
            photo_label="tool-f",
        )

        rc = export_main(
            [
                "--workspace",
                str(ws.root),
                "--observation-ids",
                f"{target_a['observation_id']},{target_b['observation_id']}",
                "--neighbor-count",
                "2",
                "--output-root",
                str(output_root),
            ]
        )

        assert rc == 0
        output_dirs = [path for path in output_root.iterdir() if path.is_dir()]
        assert len(output_dirs) == 1
        output_dir = output_dirs[0]

        index_path = output_dir / "index.html"
        manifest_path = output_dir / "manifest.json"
        assert index_path.is_file()
        assert manifest_path.is_file()

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert int(manifest["neighbor_count_per_target"]) == 2
        targets = manifest["targets"]
        assert len(targets) == 2

        html_text = index_path.read_text(encoding="utf-8")
        assert (
            f"observation {target_a['observation_id']} / photo {target_a['asset_id']} / quality 0.96"
            in html_text
        )
        assert (
            f"observation {target_b['observation_id']} / photo {target_b['asset_id']} / quality 0.99"
            in html_text
        )
        assert "distance 0.01" in html_text
        assert "distance 0.03" in html_text
        assert "distance 1.00" not in html_text
        assert "quality 0.9567" not in html_text

        for observation_id in (target_a["observation_id"], target_b["observation_id"]):
            observation_dir = output_dir / f"obs-{observation_id}"
            assert observation_dir.is_dir()

        expected_files = {
            output_dir / f"obs-{target_a['observation_id']}" / f"00-target_obs-{target_a['observation_id']}_photo-{target_a['asset_id']}__crop.jpg",
            output_dir / f"obs-{target_a['observation_id']}" / f"00-target_obs-{target_a['observation_id']}_photo-{target_a['asset_id']}__preview.jpg",
            output_dir / f"obs-{target_a['observation_id']}" / f"01-nn_obs-{neighbor_a1['observation_id']}_photo-{neighbor_a1['asset_id']}__crop.jpg",
            output_dir / f"obs-{target_a['observation_id']}" / f"02-nn_obs-{neighbor_a2['observation_id']}_photo-{neighbor_a2['asset_id']}__preview.jpg",
            output_dir / f"obs-{target_b['observation_id']}" / f"01-nn_obs-{neighbor_b1['observation_id']}_photo-{neighbor_b1['asset_id']}__crop.jpg",
            output_dir / f"obs-{target_b['observation_id']}" / f"02-nn_obs-{neighbor_b2['observation_id']}_photo-{neighbor_b2['asset_id']}__preview.jpg",
        }
        for expected_file in expected_files:
            assert expected_file.is_file()
    finally:
        ws.close()
