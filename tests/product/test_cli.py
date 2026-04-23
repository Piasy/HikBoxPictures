import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hikbox_pictures.cli import cli_entry
from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.scan.execution_service import DetectStageRunResult, ScanSessionRunResult
from hikbox_pictures.product.scan.session_service import ScanSessionRepository


def test_scan_start_or_resume_prints_incremental_assignment_stats_at_end(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)

    def fake_run_session(self, *, scan_session_id: int, runtime_defaults=None, detector=None, embedding_calculator=None):
        ScanSessionRepository(self._db_path).update_status(scan_session_id, status="completed")
        return ScanSessionRunResult(
            scan_session_id=scan_session_id,
            detect_result=DetectStageRunResult(claimed_batches=0, acked_batches=0, interrupted=False),
            assignment_run_id=88,
            new_face_count=12,
            anchor_candidate_face_count=9,
            anchor_attached_face_count=4,
            anchor_missed_face_count=5,
            anchor_missed_by_person={101: 4, 202: 1},
            local_rebuild_count=3,
            fallback_reason="incremental anchor miss too high",
        )

    monkeypatch.setattr("hikbox_pictures.cli.ScanExecutionService.run_session", fake_run_session)
    monkeypatch.setattr(
        "hikbox_pictures.cli.ScanExecutionService.detect_stage_progress",
        lambda self, *, scan_session_id: {
            "acked_batches": 0,
            "running_batches": 0,
            "claimed_batches": 0,
            "failed_batches": 0,
            "source_detect_status": {},
        },
    )

    exit_code = cli_entry(
        [
            "scan",
            "start-or-resume",
            "--workspace",
            str(workspace_root),
            "--run-kind",
            "scan_incremental",
        ]
    )

    assert exit_code == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[-7:] == [
        "new_face_count: 12",
        "anchor_candidate_face_count: 9",
        "anchor_attached_face_count: 4",
        "anchor_missed_face_count: 5",
        'anchor_missed_by_person: {"101": 4, "202": 1}',
        "local_rebuild_count: 3",
        "fallback_reason: incremental anchor miss too high",
    ]
