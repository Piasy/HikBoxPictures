from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from fastapi.testclient import TestClient

from hikbox_pictures.api.app import create_app
from hikbox_pictures.cli import main
from tests.people_gallery.real_image_helper import bind_real_source_roots

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace_scan_abort", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_seed_workspace = _MODULE.build_seed_workspace


def test_scan_abort_and_new_command(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "ws"
    ws = build_seed_workspace(workspace)
    try:
        bind_real_source_roots(ws, tmp_path / "scan-input")
        current = ws.latest_resumable_session()
        assert current is not None
        assert current["status"] == "paused"
        capsys.readouterr()

        assert main(["scan", "abort", "--workspace", str(workspace)]) == 0
        out_abort = capsys.readouterr().out
        assert "status=interrupted" in out_abort

        assert main(["scan", "new", "--workspace", str(workspace)]) == 2
        err_new = capsys.readouterr().err
        assert "--abandon-resumable" in err_new

        assert main(["scan", "new", "--workspace", str(workspace), "--abandon-resumable"]) == 0
        out_new = capsys.readouterr().out
        assert "session_id=" in out_new
        assert "status=completed" in out_new
    finally:
        ws.close()


def test_api_scan_abort_and_start_new(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    ws = build_seed_workspace(workspace)
    try:
        bind_real_source_roots(ws, tmp_path / "api-scan-input")
        client = TestClient(create_app(workspace=workspace))

        session = ws.latest_resumable_session()
        assert session is not None
        old_session_id = int(session["id"])

        abort_response = client.post("/api/scan/abort")
        assert abort_response.status_code == 200
        abort_body = abort_response.json()
        assert abort_body["session_id"] == old_session_id
        assert abort_body["status"] == "interrupted"

        conflict_response = client.post("/api/scan/start_new")
        assert conflict_response.status_code == 409

        create_response = client.post("/api/scan/start_new", params={"abandon_resumable": True})
        assert create_response.status_code == 200
        create_body = create_response.json()
        assert create_body["session_id"] != old_session_id
        assert create_body["status"] == "completed"
        assert create_body["mode"] == "incremental"

        abandoned = ws.scan_repo.get_session(old_session_id)
        assert abandoned is not None
        assert abandoned["status"] == "abandoned"
    finally:
        ws.close()
