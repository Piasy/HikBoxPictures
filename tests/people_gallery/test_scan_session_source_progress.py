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

import hikbox_pictures.services.asset_stage_runner as asset_stage_runner_module

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


def test_scan_session_source_progress_flushes_incremental_snapshot_before_stage_end(tmp_path, monkeypatch) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        session_id = ws.scan_repo.create_session(mode="incremental", status="running", started=True)
        source_id = int(ws.source_repo.list_sources(active=True)[0]["id"])
        session_source_id = ws.scan_repo.create_session_source(session_id, source_id, status="running")

        for index in range(3):
            ws.asset_repo.add_photo_asset(
                source_id,
                f"/tmp/flush-{index}.jpg",
                processing_status="metadata_done",
            )
        ws.conn.commit()

        clock = {"now": 0.0}
        monkeypatch.setattr(asset_stage_runner_module.time, "monotonic", lambda: clock["now"])

        class _TickingFaceRunner(AssetStageRunner):
            def __init__(self, conn):
                super().__init__(conn)
                self._calls = 0

            def _run_faces_stage(self, asset_id: int, scan_session_id: int) -> None:
                self._calls += 1
                self.asset_repo.mark_stage_done_if_current(
                    asset_id,
                    from_status="metadata_done",
                    to_status="faces_done",
                    last_processed_session_id=scan_session_id,
                )
                if self._calls == 1:
                    clock["now"] = 1.0
                elif self._calls == 2:
                    clock["now"] = 6.0
                else:
                    clock["now"] = 7.0

        runner = _TickingFaceRunner(ws.conn)
        captured_updates: list[dict[str, int]] = []
        original_update = runner.scan_repo.update_source_progress_counts

        def _capture_update(
            source_id_value: int,
            *,
            discovered_count: int,
            metadata_done_count: int,
            faces_done_count: int,
            embeddings_done_count: int,
            assignment_done_count: int,
        ) -> None:
            captured_updates.append(
                {
                    "session_source_id": int(source_id_value),
                    "discovered_count": int(discovered_count),
                    "metadata_done_count": int(metadata_done_count),
                    "faces_done_count": int(faces_done_count),
                    "embeddings_done_count": int(embeddings_done_count),
                    "assignment_done_count": int(assignment_done_count),
                }
            )
            original_update(
                source_id_value,
                discovered_count=discovered_count,
                metadata_done_count=metadata_done_count,
                faces_done_count=faces_done_count,
                embeddings_done_count=embeddings_done_count,
                assignment_done_count=assignment_done_count,
            )

        monkeypatch.setattr(runner.scan_repo, "update_source_progress_counts", _capture_update)

        runner.run_stage(session_source_id, "faces")

        assert [item["faces_done_count"] for item in captured_updates] == [0, 2, 3]
        assert all(item["session_source_id"] == session_source_id for item in captured_updates)
        assert all(item["discovered_count"] == 3 for item in captured_updates)
        assert all(item["metadata_done_count"] == 3 for item in captured_updates)
    finally:
        ws.close()
