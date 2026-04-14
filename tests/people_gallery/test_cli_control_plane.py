from __future__ import annotations

import re
import sqlite3
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from hikbox_pictures.cli import main
from tests.people_gallery.real_image_helper import bind_real_source_roots, copy_raw_face_image

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace_cli_control_plane", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_seed_workspace = _MODULE.build_seed_workspace


def _init_args(workspace: Path, *, external_root: Path | None = None) -> list[str]:
    resolved_external_root = workspace if external_root is None else external_root
    return [
        "init",
        "--workspace",
        str(workspace),
        "--external-root",
        str(resolved_external_root),
    ]


def test_cli_init_creates_workspace_and_db(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    rc = main(_init_args(workspace, external_root=external_root))

    assert rc == 0
    assert (workspace / ".hikbox" / "library.db").exists()
    assert (workspace / ".hikbox" / "config.json").exists()
    assert (external_root / "artifacts" / "ann").exists()
    assert (external_root / "logs" / "runs").exists()
    assert (external_root / "exports").exists()


def test_cli_init_requires_external_root(capsys) -> None:
    rc = main(["init", "--workspace", "/tmp/ws"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "--external-root" in err


def test_cli_help_contains_control_plane_commands(capsys) -> None:
    rc = main(["--help"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "init" in out
    assert "source" in out
    assert "serve" in out
    assert "scan" in out
    assert "rebuild-artifacts" in out
    assert "export" in out
    assert "logs" in out


def test_cli_logs_help_contains_tail_and_prune(capsys) -> None:
    rc = main(["logs", "--help"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "tail" in out
    assert "prune" in out


def test_cli_export_help_contains_run(capsys) -> None:
    rc = main(["export", "--help"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "run" in out


def test_scan_status_command(tmp_path: Path, capsys) -> None:
    rc_init = main(_init_args(tmp_path, external_root=tmp_path / ".hikbox"))
    assert rc_init == 0

    rc_status = main(["scan", "status", "--workspace", str(tmp_path)])
    assert rc_status == 0
    out = capsys.readouterr().out
    assert "scan session_id=" in out
    assert "status=idle" in out


def test_rebuild_artifacts_command(tmp_path: Path, capsys) -> None:
    rc_init = main(_init_args(tmp_path, external_root=tmp_path / ".hikbox"))
    assert rc_init == 0
    capsys.readouterr()

    rc = main(["rebuild-artifacts", "--workspace", str(tmp_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "ANN 与人物原型重建完成" in out
    assert (tmp_path / ".hikbox" / "artifacts" / "ann" / "prototype_index.npz").exists()


def test_init_does_not_import_deepface_engine(tmp_path: Path) -> None:
    sys.modules.pop("hikbox_pictures.deepface_engine", None)

    rc = main(_init_args(tmp_path, external_root=tmp_path / ".hikbox"))

    assert rc == 0
    assert "hikbox_pictures.deepface_engine" not in sys.modules


def test_logs_prune_command_returns_zero_and_prints_summary(tmp_path: Path, capsys) -> None:
    assert main(_init_args(tmp_path, external_root=tmp_path / ".hikbox")) == 0
    capsys.readouterr()

    rc_logs = main(["logs", "prune", "--workspace", str(tmp_path)])
    assert rc_logs == 0
    out_logs = capsys.readouterr().out
    assert "logs pruned=" in out_logs


def test_logs_prune_days_must_be_positive(tmp_path: Path, capsys) -> None:
    rc_logs = main(["logs", "prune", "--workspace", str(tmp_path), "--days", "0"])
    assert rc_logs == 2
    err_logs = capsys.readouterr().err
    assert "--days 必须大于 0" in err_logs


def test_cli_control_plane_happy_path_covers_scan_export_logs(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "workspace"
    rc_init = main(_init_args(workspace, external_root=workspace / ".hikbox"))
    assert rc_init == 0
    capsys.readouterr()

    ws = build_seed_workspace(workspace, seed_export_assets=True)
    try:
        bind_real_source_roots(ws, tmp_path / "scan-input")
        rc_scan = main(["scan", "--workspace", str(workspace)])
        assert rc_scan == 0
        out_scan = capsys.readouterr().out
        assert "scan session_id=" in out_scan
        assert "mode=incremental" in out_scan
        assert "status=running" in out_scan
        assert "status=completed" in out_scan

        rc_export = main(
            ["export", "run", "--workspace", str(workspace), "--template-id", str(ws.export_template_id)]
        )
        assert rc_export == 0
        out_export = capsys.readouterr().out
        assert "matched_only=2" in out_export
        assert "matched_group=1" in out_export
        assert "failed=0" in out_export

        run_id_match = re.search(r"run_id=(\d+)", out_export)
        assert run_id_match is not None
        run_id = run_id_match.group(1)

        rc_tail = main(
            [
                "logs",
                "tail",
                "--workspace",
                str(workspace),
                "--run-kind",
                "export",
                "--run-id",
                run_id,
                "--limit",
                "20",
            ]
        )
        assert rc_tail == 0
        out_tail = capsys.readouterr().out
        assert '"event_type":"export.delivery.started"' in out_tail
        assert '"event_type":"export.delivery.completed"' in out_tail
        assert '"event_type":"export.delivery.failed"' not in out_tail

        rc_prune = main(["logs", "prune", "--workspace", str(workspace), "--days", "90"])
        assert rc_prune == 0
        out_prune = capsys.readouterr().out
        assert "logs pruned=" in out_prune
        assert "days=90" in out_prune
    finally:
        ws.close()


@pytest.mark.real_face_engine
def test_cli_scan_executes_discover_to_completed_pipeline(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "workspace"
    source_root = tmp_path / "source-files"
    source_root.mkdir(parents=True, exist_ok=True)
    copy_raw_face_image(source_root / "x.jpg", index=0)
    copy_raw_face_image(source_root / "y.jpeg", index=1)

    assert main(_init_args(workspace, external_root=workspace / ".hikbox")) == 0
    assert (
        main(
            [
                "source",
                "add",
                "--workspace",
                str(workspace),
                "--name",
                "scan-source",
                "--root-path",
                str(source_root),
            ]
        )
        == 0
    )
    capsys.readouterr()

    rc_scan = main(["scan", "--workspace", str(workspace)])
    assert rc_scan == 0
    out_scan = capsys.readouterr().out
    assert "scan session_id=" in out_scan
    assert "status=running" in out_scan
    assert "status=completed" in out_scan

    rc_status = main(["scan", "status", "--workspace", str(workspace)])
    assert rc_status == 0
    out_status = capsys.readouterr().out
    assert "status=completed" in out_status

    conn = sqlite3.connect(workspace / ".hikbox" / "library.db")
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM photo_asset WHERE processing_status = 'assignment_done') AS asset_count,
              (SELECT COUNT(*) FROM face_observation WHERE active = 1) AS observation_count,
              (SELECT COUNT(*) FROM face_embedding) AS embedding_count,
              (
                SELECT COUNT(*)
                FROM review_item
                WHERE review_type = 'new_person'
                  AND status = 'open'
              ) AS new_person_review_count
            """
        ).fetchone()
        assert row is not None
        assert int(row["asset_count"]) == 2
        assert int(row["observation_count"]) >= 2
        assert int(row["embedding_count"]) >= 2
        assert int(row["new_person_review_count"]) >= 1
    finally:
        conn.close()
