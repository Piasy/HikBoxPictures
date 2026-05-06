from __future__ import annotations

import socket
from pathlib import Path

import httpx
import pytest

import hikbox_pictures.cli as cli_module

from tests.helpers import (
    FIXTURE_DIR,
    add_source,
    find_free_port,
    init_workspace,
    prepare_workspace_models,
    run_hikbox,
    spawn_hikbox,
    terminate_process,
    wait_for_http_ready,
)
from tests.serve_cli_helpers import (
    create_broken_webui_workspace,
    create_broken_webui_workspace_missing_assignment_updated_at,
    create_slice_a_only_workspace,
    port_is_listening,
    wait_for_batch_status,
)


def test_serve_fails_without_initialized_workspace_and_leaves_port_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "missing-workspace"
    port = find_free_port()

    result = run_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
    )

    assert result.returncode != 0
    assert "工作区" in result.stderr
    assert "Traceback" not in result.stderr
    assert not port_is_listening(port)


def test_serve_uses_204_as_default_person_detail_page_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    captured_calls: list[dict[str, object]] = []

    def fake_serve_workspace(
        *,
        workspace: Path,
        port: int,
        person_detail_page_size: int,
    ) -> None:
        captured_calls.append(
            {
                "workspace": workspace,
                "port": port,
                "person_detail_page_size": person_detail_page_size,
            }
        )

    monkeypatch.setattr(cli_module, "serve_workspace", fake_serve_workspace)

    exit_code = cli_module.main(
        [
            "serve",
            "--workspace",
            str(workspace),
            "--port",
            "45678",
        ]
    )

    assert exit_code == 0
    assert captured_calls == [
        {
            "workspace": workspace,
            "port": 45678,
            "person_detail_page_size": 204,
        }
    ]


def test_serve_rejects_invalid_person_detail_page_size(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    init_result = init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    port = find_free_port()
    invalid_results = [
        run_hikbox(
            "serve",
            "--workspace",
            str(workspace),
            "--port",
            str(port),
            "--person-detail-page-size",
            "0",
        ),
        run_hikbox(
            "serve",
            "--workspace",
            str(workspace),
            "--port",
            str(port),
            "--person-detail-page-size",
            "-1",
        ),
        run_hikbox(
            "serve",
            "--workspace",
            str(workspace),
            "--port",
            str(port),
            "--person-detail-page-size",
            "abc",
        ),
    ]

    for result in invalid_results:
        assert result.returncode != 0
        assert "person-detail-page-size" in result.stderr
        assert "正整数" in result.stderr
        assert not port_is_listening(port)


@pytest.mark.parametrize("invalid_port", ["-1", "70000", "abc"])
def test_serve_rejects_invalid_port_range_or_format(tmp_path: Path, invalid_port: str) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    init_result = init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    result = run_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        invalid_port,
    )

    assert result.returncode != 0
    assert "--port" in result.stderr or "端口" in result.stderr
    assert "Traceback" not in result.stderr


def test_serve_fails_when_target_port_is_occupied(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    init_result = init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied_socket:
        occupied_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        occupied_socket.bind(("127.0.0.1", 0))
        occupied_socket.listen(1)
        port = int(occupied_socket.getsockname()[1])

        result = run_hikbox(
            "serve",
            "--workspace",
            str(workspace),
            "--port",
            str(port),
        )

        assert result.returncode != 0
        assert "端口" in result.stderr
        assert "占用" in result.stderr
        assert "Traceback" not in result.stderr
        assert port_is_listening(port)

    assert not port_is_listening(port)


def test_serve_fails_cleanly_when_workspace_lacks_webui_schema(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-slice-a-only"
    external_root = tmp_path / "external-root-slice-a-only"
    source_dir = tmp_path / "source-slice-a-only"
    source_dir.mkdir()
    (source_dir / "sample.jpg").write_bytes((FIXTURE_DIR / "pg_001_single_alex_01.jpg").read_bytes())
    create_slice_a_only_workspace(workspace, external_root, source_dir)
    port = find_free_port()

    result = run_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
    )

    assert result.returncode != 0
    assert "schema" in result.stderr
    assert "WebUI" in result.stderr
    assert "Traceback" not in result.stderr
    assert not port_is_listening(port)


def test_serve_fails_cleanly_when_webui_schema_columns_are_incompatible(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-broken-webui-schema"
    external_root = tmp_path / "external-root-broken-webui-schema"
    source_dir = tmp_path / "source-broken-webui-schema"
    source_dir.mkdir()
    (source_dir / "sample.jpg").write_bytes((FIXTURE_DIR / "pg_001_single_alex_01.jpg").read_bytes())
    create_broken_webui_workspace(workspace, external_root, source_dir)
    port = find_free_port()

    result = run_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
    )

    assert result.returncode != 0
    assert "schema" in result.stderr
    assert "列" in result.stderr
    assert "Traceback" not in result.stderr
    assert not port_is_listening(port)


def test_serve_fails_cleanly_when_person_face_assignments_updated_at_is_missing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-broken-assignment-updated-at"
    external_root = tmp_path / "external-root-broken-assignment-updated-at"
    source_dir = tmp_path / "source-broken-assignment-updated-at"
    source_dir.mkdir()
    (source_dir / "sample.jpg").write_bytes((FIXTURE_DIR / "pg_001_single_alex_01.jpg").read_bytes())
    create_broken_webui_workspace_missing_assignment_updated_at(workspace, external_root, source_dir)
    port = find_free_port()

    result = run_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
    )

    assert result.returncode != 0
    assert "schema" in result.stderr
    assert "person_face_assignments.updated_at" in result.stderr
    assert "Traceback" not in result.stderr
    assert not port_is_listening(port)


def test_serve_fails_when_scan_is_running_and_does_not_bind_port(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-running-scan"
    external_root = tmp_path / "external-root-running-scan"
    init_result = init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    prepare_workspace_models(workspace)
    add_result = add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0

    scan_process = spawn_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    library_db = workspace / ".hikbox" / "library.db"
    port = find_free_port()
    try:
        wait_for_batch_status(library_db, batch_index=2, expected_status="running")
        result = run_hikbox(
            "serve",
            "--workspace",
            str(workspace),
            "--port",
            str(port),
        )
        assert result.returncode != 0
        assert "扫描" in result.stderr
        assert "运行" in result.stderr
        assert "Traceback" not in result.stderr
        assert not port_is_listening(port)
    finally:
        terminate_process(scan_process)


def test_serve_renders_empty_state_and_missing_person_returns_404(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-empty-state"
    external_root = tmp_path / "external-root-empty-state"
    init_result = init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    add_result = add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0

    port = find_free_port()
    process = spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        wait_for_http_ready(f"{base_url}/")
        homepage = httpx.get(f"{base_url}/", follow_redirects=True, timeout=5.0)
        people_page = httpx.get(f"{base_url}/people", follow_redirects=True, timeout=5.0)
        missing_person = httpx.get(f"{base_url}/people/not-a-real-person", timeout=5.0)

        assert homepage.status_code == 200
        assert people_page.status_code == 200
        assert "empty" in homepage.text or "暂无人物" in homepage.text
        assert "empty" in people_page.text or "暂无人物" in people_page.text
        assert missing_person.status_code == 404
        assert "not-a-real-person" in missing_person.text or "未找到" in missing_person.text
    finally:
        terminate_process(process)
