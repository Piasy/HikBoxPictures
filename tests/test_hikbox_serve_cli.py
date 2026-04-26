from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time

import httpx
import pytest

import hikbox_pictures.cli as cli_module


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "people_gallery_scan"
OLD_SLICE_A_LIBRARY_SQL = """
CREATE TABLE schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1');

CREATE TABLE library_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  created_at TEXT NOT NULL
);
""".strip()
OLD_SLICE_A_EMBEDDING_SQL = """
CREATE TABLE schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1');
""".strip()
BROKEN_WEBUI_LIBRARY_SQL = """
CREATE TABLE schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1');

CREATE TABLE library_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  created_at TEXT NOT NULL
);

CREATE TABLE assets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL REFERENCES library_sources(id),
  absolute_path TEXT NOT NULL UNIQUE,
  file_name TEXT NOT NULL,
  file_extension TEXT NOT NULL,
  capture_month TEXT NOT NULL,
  file_fingerprint TEXT NOT NULL,
  processing_status TEXT NOT NULL,
  failure_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE scan_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_fingerprint TEXT NOT NULL UNIQUE,
  batch_size INTEGER NOT NULL,
  status TEXT NOT NULL,
  command TEXT NOT NULL,
  total_batches INTEGER NOT NULL DEFAULT 0,
  completed_batches INTEGER NOT NULL DEFAULT 0,
  failed_assets INTEGER NOT NULL DEFAULT 0,
  success_faces INTEGER NOT NULL DEFAULT 0,
  artifact_files INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE TABLE face_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id INTEGER NOT NULL REFERENCES assets(id),
  face_index INTEGER NOT NULL,
  bbox_x1 REAL NOT NULL,
  bbox_y1 REAL NOT NULL,
  bbox_x2 REAL NOT NULL,
  bbox_y2 REAL NOT NULL,
  image_width INTEGER NOT NULL,
  image_height INTEGER NOT NULL,
  score REAL NOT NULL,
  crop_path TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE person (
  id TEXT PRIMARY KEY,
  display_name TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE person_name_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id TEXT NOT NULL REFERENCES person(id),
  event_type TEXT NOT NULL,
  new_display_name TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE person_face_assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id TEXT NOT NULL REFERENCES person(id),
  face_observation_id INTEGER NOT NULL REFERENCES face_observations(id),
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
""".strip()


def _run_hikbox(
    *args: str,
    cwd: Path | None = None,
    env_updates: dict[str, str] | None = None,
    pythonpath_prepend: list[Path] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    pythonpath_parts = [str(path) for path in (pythonpath_prepend or [])]
    pythonpath_parts.append(str(REPO_ROOT))
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    if env_updates:
        env.update(env_updates)
    return subprocess.run(
        [sys.executable, "-m", "hikbox_pictures", *args],
        cwd=cwd or REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _spawn_hikbox(
    *args: str,
    cwd: Path | None = None,
    env_updates: dict[str, str] | None = None,
    pythonpath_prepend: list[Path] | None = None,
) -> subprocess.Popen[str]:
    env = os.environ.copy()
    pythonpath_parts = [str(path) for path in (pythonpath_prepend or [])]
    pythonpath_parts.append(str(REPO_ROOT))
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    if env_updates:
        env.update(env_updates)
    return subprocess.Popen(
        [sys.executable, "-m", "hikbox_pictures", *args],
        cwd=cwd or REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _init_workspace(workspace: Path, external_root: Path) -> subprocess.CompletedProcess[str]:
    return _run_hikbox(
        "init",
        "--workspace",
        str(workspace),
        "--external-root",
        str(external_root),
    )


def _add_source(workspace: Path, source_dir: Path) -> subprocess.CompletedProcess[str]:
    return _run_hikbox(
        "source",
        "add",
        "--workspace",
        str(workspace),
        str(source_dir),
    )


def _prepare_workspace_models(workspace: Path) -> None:
    source_root = _find_model_root()
    target_root = workspace / ".hikbox" / "models" / "insightface"
    if target_root.exists():
        shutil.rmtree(target_root)
    shutil.copytree(source_root, target_root)


def _find_model_root() -> Path:
    candidates = [REPO_ROOT / ".insightface", Path.home() / ".insightface"]
    candidates.extend(parent / ".insightface" for parent in REPO_ROOT.parents)
    for candidate in candidates:
        if (candidate / "models" / "buffalo_l" / "det_10g.onnx").exists():
            return candidate
    raise AssertionError("缺少 InsightFace buffalo_l 模型目录，无法执行 serve 真实集成测试")


def _create_slice_a_only_workspace(workspace: Path, external_root: Path, source_dir: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    hikbox_dir = workspace / ".hikbox"
    hikbox_dir.mkdir(parents=True, exist_ok=True)
    (external_root / "artifacts" / "crops").mkdir(parents=True, exist_ok=True)
    (external_root / "artifacts" / "context").mkdir(parents=True, exist_ok=True)
    (external_root / "logs").mkdir(parents=True, exist_ok=True)
    (hikbox_dir / "config.json").write_text(
        json.dumps(
            {
                "config_version": 1,
                "external_root": str(external_root.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    library_db = hikbox_dir / "library.db"
    embedding_db = hikbox_dir / "embedding.db"
    library_conn = sqlite3.connect(library_db)
    try:
        with library_conn:
            library_conn.executescript(OLD_SLICE_A_LIBRARY_SQL)
            library_conn.execute(
                """
                INSERT INTO library_sources (path, label, active, created_at)
                VALUES (?, 'legacy-source', 1, '2026-04-24T00:00:00Z')
                """,
                (str(source_dir.resolve()),),
            )
    finally:
        library_conn.close()

    embedding_conn = sqlite3.connect(embedding_db)
    try:
        with embedding_conn:
            embedding_conn.executescript(OLD_SLICE_A_EMBEDDING_SQL)
    finally:
        embedding_conn.close()


def _create_broken_webui_workspace(workspace: Path, external_root: Path, source_dir: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    hikbox_dir = workspace / ".hikbox"
    hikbox_dir.mkdir(parents=True, exist_ok=True)
    (external_root / "artifacts" / "crops").mkdir(parents=True, exist_ok=True)
    (external_root / "artifacts" / "context").mkdir(parents=True, exist_ok=True)
    (external_root / "logs").mkdir(parents=True, exist_ok=True)
    (hikbox_dir / "config.json").write_text(
        json.dumps(
            {
                "config_version": 1,
                "external_root": str(external_root.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    library_db = hikbox_dir / "library.db"
    embedding_db = hikbox_dir / "embedding.db"
    library_conn = sqlite3.connect(library_db)
    try:
        with library_conn:
            library_conn.executescript(BROKEN_WEBUI_LIBRARY_SQL)
            library_conn.execute(
                """
                INSERT INTO library_sources (path, label, active, created_at)
                VALUES (?, 'broken-source', 1, '2026-04-24T00:00:00Z')
                """,
                (str(source_dir.resolve()),),
            )
    finally:
        library_conn.close()

    embedding_conn = sqlite3.connect(embedding_db)
    try:
        with embedding_conn:
            embedding_conn.executescript(OLD_SLICE_A_EMBEDDING_SQL)
    finally:
        embedding_conn.close()


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(sock.getsockname()[1])


def _port_is_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _wait_for_batch_status(db_path: Path, *, batch_index: int, expected_status: str) -> None:
    deadline = time.time() + 90
    while time.time() < deadline:
        connection = sqlite3.connect(db_path)
        try:
            row = connection.execute(
                "SELECT status FROM scan_batches WHERE batch_index = ?",
                (batch_index,),
            ).fetchone()
        finally:
            connection.close()
        if row is not None and str(row[0]) == expected_status:
            return
        time.sleep(0.2)
    raise AssertionError(f"等待 batch_index={batch_index} 进入 {expected_status} 超时")


def _wait_for_http_ready(base_url: str) -> None:
    deadline = time.time() + 30
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            response = httpx.get(base_url, follow_redirects=True, timeout=1.0)
            if response.status_code < 500:
                return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        time.sleep(0.2)
    raise AssertionError(f"等待服务可用超时: {base_url}; last_error={last_error!r}")


def _terminate_process(process: subprocess.Popen[str]) -> tuple[str, str]:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    try:
        stdout_text, stderr_text = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout_text, stderr_text = process.communicate(timeout=30)
    return stdout_text, stderr_text


def test_serve_fails_without_initialized_workspace_and_leaves_port_closed(tmp_path: Path) -> None:
    workspace = tmp_path / "missing-workspace"
    port = _find_free_port()

    result = _run_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
    )

    assert result.returncode != 0
    assert "工作区" in result.stderr
    assert "Traceback" not in result.stderr
    assert not _port_is_listening(port)


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
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    port = _find_free_port()
    invalid_results = [
        _run_hikbox(
            "serve",
            "--workspace",
            str(workspace),
            "--port",
            str(port),
            "--person-detail-page-size",
            "0",
        ),
        _run_hikbox(
            "serve",
            "--workspace",
            str(workspace),
            "--port",
            str(port),
            "--person-detail-page-size",
            "-1",
        ),
        _run_hikbox(
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
        assert not _port_is_listening(port)


@pytest.mark.parametrize("invalid_port", ["-1", "70000", "abc"])
def test_serve_rejects_invalid_port_range_or_format(tmp_path: Path, invalid_port: str) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    result = _run_hikbox(
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
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied_socket:
        occupied_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        occupied_socket.bind(("127.0.0.1", 0))
        occupied_socket.listen(1)
        port = int(occupied_socket.getsockname()[1])

        result = _run_hikbox(
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
        assert _port_is_listening(port)

    assert not _port_is_listening(port)


def test_serve_fails_cleanly_when_workspace_lacks_webui_schema(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-slice-a-only"
    external_root = tmp_path / "external-root-slice-a-only"
    source_dir = tmp_path / "source-slice-a-only"
    source_dir.mkdir()
    (source_dir / "sample.jpg").write_bytes((FIXTURE_DIR / "pg_001_single_alex_01.jpg").read_bytes())
    _create_slice_a_only_workspace(workspace, external_root, source_dir)
    port = _find_free_port()

    result = _run_hikbox(
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
    assert not _port_is_listening(port)


def test_serve_fails_cleanly_when_webui_schema_columns_are_incompatible(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-broken-webui-schema"
    external_root = tmp_path / "external-root-broken-webui-schema"
    source_dir = tmp_path / "source-broken-webui-schema"
    source_dir.mkdir()
    (source_dir / "sample.jpg").write_bytes((FIXTURE_DIR / "pg_001_single_alex_01.jpg").read_bytes())
    _create_broken_webui_workspace(workspace, external_root, source_dir)
    port = _find_free_port()

    result = _run_hikbox(
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
    assert not _port_is_listening(port)


def test_serve_fails_when_scan_is_running_and_does_not_bind_port(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-running-scan"
    external_root = tmp_path / "external-root-running-scan"
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0

    scan_process = _spawn_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    library_db = workspace / ".hikbox" / "library.db"
    port = _find_free_port()
    try:
        _wait_for_batch_status(library_db, batch_index=2, expected_status="running")
        result = _run_hikbox(
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
        assert not _port_is_listening(port)
    finally:
        _terminate_process(scan_process)


def test_serve_renders_empty_state_and_missing_person_returns_404(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-empty-state"
    external_root = tmp_path / "external-root-empty-state"
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    add_result = _add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0

    port = _find_free_port()
    process = _spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_http_ready(f"{base_url}/")
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
        _terminate_process(process)
