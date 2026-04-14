from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_seed_workspace = _MODULE.build_seed_workspace

from hikbox_pictures.services.asset_stage_runner import AssetStageRunner
from tests.people_gallery.real_image_helper import copy_raw_face_image


def test_scan_session_source_progress_counts_follow_stage_pipeline(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        session_id = ws.scan_repo.create_session(mode="incremental", status="running", started=True)
        source_id = int(ws.source_repo.list_sources(active=True)[0]["id"])
        session_source_id = ws.scan_repo.create_session_source(session_id, source_id, status="running")
        first = copy_raw_face_image(tmp_path / "progress-a.jpg", index=0)
        second = copy_raw_face_image(tmp_path / "progress-b.jpg", index=1)
        ws.asset_repo.add_photo_asset(source_id, str(first), processing_status="discovered")
        ws.asset_repo.add_photo_asset(source_id, str(second), processing_status="discovered")
        ws.conn.commit()

        runner = AssetStageRunner(ws.conn)

        runner.run_stage(session_source_id, "metadata")
        source_state = ws.scan_repo.get_session_source(session_source_id)
        assert source_state is not None
        assert source_state["discovered_count"] == 2
        assert source_state["metadata_done_count"] == 2
        assert source_state["faces_done_count"] == 0
        assert source_state["embeddings_done_count"] == 0
        assert source_state["assignment_done_count"] == 0

        runner.run_stage(session_source_id, "faces")
        source_state = ws.scan_repo.get_session_source(session_source_id)
        assert source_state is not None
        assert source_state["discovered_count"] == 2
        assert source_state["metadata_done_count"] == 2
        assert source_state["faces_done_count"] == 2
        assert source_state["embeddings_done_count"] == 0
        assert source_state["assignment_done_count"] == 0

        runner.run_stage(session_source_id, "embeddings")
        source_state = ws.scan_repo.get_session_source(session_source_id)
        assert source_state is not None
        assert source_state["discovered_count"] == 2
        assert source_state["metadata_done_count"] == 2
        assert source_state["faces_done_count"] == 2
        assert source_state["embeddings_done_count"] == 2
        assert source_state["assignment_done_count"] == 0

        runner.run_stage(session_source_id, "assignment")
        source_state = ws.scan_repo.get_session_source(session_source_id)
        assert source_state is not None
        assert source_state["discovered_count"] == 2
        assert source_state["metadata_done_count"] == 2
        assert source_state["faces_done_count"] == 2
        assert source_state["embeddings_done_count"] == 2
        assert source_state["assignment_done_count"] == 2
    finally:
        ws.close()
