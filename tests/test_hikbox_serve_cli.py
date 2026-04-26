from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
from typing import Any

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

CREATE TABLE person_merge_operations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  winner_person_id TEXT NOT NULL REFERENCES person(id),
  loser_person_id TEXT NOT NULL REFERENCES person(id),
  winner_display_name_before TEXT,
  winner_is_named_before INTEGER NOT NULL,
  winner_status_before TEXT NOT NULL,
  loser_display_name_before TEXT,
  loser_is_named_before INTEGER NOT NULL,
  loser_status_before TEXT NOT NULL,
  merged_at TEXT NOT NULL
);

CREATE TABLE person_merge_operation_assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  merge_operation_id INTEGER NOT NULL REFERENCES person_merge_operations(id),
  assignment_id INTEGER NOT NULL REFERENCES person_face_assignments(id),
  person_role TEXT NOT NULL
);
""".strip()
BROKEN_WEBUI_LIBRARY_SQL_MISSING_ASSIGNMENT_UPDATED_AT = """
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
  live_photo_mov_path TEXT,
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
  context_path TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE person (
  id TEXT PRIMARY KEY,
  display_name TEXT,
  is_named INTEGER NOT NULL DEFAULT 0 CHECK (is_named IN (0, 1)),
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE person_name_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id TEXT NOT NULL REFERENCES person(id),
  event_type TEXT NOT NULL,
  old_display_name TEXT,
  new_display_name TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE person_face_assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id TEXT NOT NULL REFERENCES person(id),
  face_observation_id INTEGER NOT NULL REFERENCES face_observations(id),
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE TABLE person_merge_operations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  winner_person_id TEXT NOT NULL REFERENCES person(id),
  loser_person_id TEXT NOT NULL REFERENCES person(id),
  winner_display_name_before TEXT,
  winner_is_named_before INTEGER NOT NULL,
  winner_status_before TEXT NOT NULL,
  loser_display_name_before TEXT,
  loser_is_named_before INTEGER NOT NULL,
  loser_status_before TEXT NOT NULL,
  merged_at TEXT NOT NULL
);

CREATE TABLE person_merge_operation_assignments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  merge_operation_id INTEGER NOT NULL REFERENCES person_merge_operations(id),
  assignment_id INTEGER NOT NULL REFERENCES person_face_assignments(id),
  person_role TEXT NOT NULL
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


def _create_broken_webui_workspace_missing_assignment_updated_at(
    workspace: Path,
    external_root: Path,
    source_dir: Path,
) -> None:
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
            library_conn.executescript(BROKEN_WEBUI_LIBRARY_SQL_MISSING_ASSIGNMENT_UPDATED_AT)
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


def _fetch_all(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(db_path)
    try:
        return [tuple(row) for row in connection.execute(sql, params).fetchall()]
    finally:
        connection.close()


def _read_merge_slice_db_snapshot(library_db: Path) -> dict[str, Any]:
    return {
        "people": _fetch_all(
            library_db,
            """
            SELECT id, display_name, is_named, status, write_revision, updated_at
            FROM person
            ORDER BY id ASC
            """,
        ),
        "active_assignments": _fetch_all(
            library_db,
            """
            SELECT id, person_id, face_observation_id, active, updated_at
            FROM person_face_assignments
            ORDER BY id ASC
            """,
        ),
        "merge_operations": _fetch_all(
            library_db,
            """
            SELECT
              id,
              winner_person_id,
              loser_person_id,
              winner_write_revision_after_merge,
              loser_write_revision_after_merge,
              undone_at
            FROM person_merge_operations
            ORDER BY id ASC
            """,
        ),
        "merge_assignments": _fetch_all(
            library_db,
            """
            SELECT merge_operation_id, assignment_id, person_role
            FROM person_merge_operation_assignments
            ORDER BY id ASC
            """,
        ),
    }


def _load_manifest() -> dict[str, object]:
    return json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))


def _asset_assignment_rows(library_db: Path) -> dict[str, list[tuple[int, str]]]:
    rows = _fetch_all(
        library_db,
        """
        SELECT
          assets.file_name,
          face_observations.face_index,
          person_face_assignments.person_id
        FROM person_face_assignments
        INNER JOIN face_observations
          ON face_observations.id = person_face_assignments.face_observation_id
        INNER JOIN assets
          ON assets.id = face_observations.asset_id
        WHERE person_face_assignments.active = 1
        ORDER BY assets.file_name ASC, face_observations.face_index ASC
        """,
    )
    result: dict[str, list[tuple[int, str]]] = {}
    for file_name, face_index, person_id in rows:
        result.setdefault(str(file_name), []).append((int(face_index), str(person_id)))
    return result


def _expected_target_mapping(library_db: Path, manifest: dict[str, object]) -> dict[str, str]:
    assignment_rows = _asset_assignment_rows(library_db)
    mapping: dict[str, str] = {}
    for label in manifest["expected_person_groups"]:
        observed_person_ids: set[str] = set()
        for asset in manifest["assets"]:
            if asset["expected_target_people"] != [label]:
                continue
            file_name = str(asset["file"])
            observed_person_ids.update(person_id for _, person_id in assignment_rows.get(file_name, []))
        assert observed_person_ids, f"{label} 缺少 target assignment"
        assert len(observed_person_ids) == 1, observed_person_ids
        mapping[str(label)] = next(iter(observed_person_ids))
    return mapping


def _read_person_page_status(base_url: str, person_id: str) -> int:
    return int(httpx.get(f"{base_url}/people/{person_id}", timeout=5.0).status_code)


def _read_person_write_revision(library_db: Path, person_id: str) -> int:
    return int(
        _fetch_all(
            library_db,
            """
            SELECT write_revision
            FROM person
            WHERE id = ?
            """,
            (person_id,),
        )[0][0]
    )


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


def test_serve_fails_cleanly_when_person_face_assignments_updated_at_is_missing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-broken-assignment-updated-at"
    external_root = tmp_path / "external-root-broken-assignment-updated-at"
    source_dir = tmp_path / "source-broken-assignment-updated-at"
    source_dir.mkdir()
    (source_dir / "sample.jpg").write_bytes((FIXTURE_DIR / "pg_001_single_alex_01.jpg").read_bytes())
    _create_broken_webui_workspace_missing_assignment_updated_at(workspace, external_root, source_dir)
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
    assert "person_face_assignments.updated_at" in result.stderr
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


def test_serve_merge_rejects_crafted_requests_without_db_changes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-merge-crafted"
    external_root = tmp_path / "external-root-merge-crafted"
    manifest = _load_manifest()
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0

    scan_result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert scan_result.returncode == 0, scan_result.stderr

    library_db = workspace / ".hikbox" / "library.db"
    target_person_ids = _expected_target_mapping(library_db, manifest)
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]
    blair_person_id = target_person_ids["target_blair"]
    winner_person_id = min(alex_person_id, casey_person_id)
    loser_person_id = casey_person_id if winner_person_id == alex_person_id else alex_person_id

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
        invalid_cases = [
            ({"person_id": [alex_person_id]}, "必须恰好选择 2 个人物"),
            ({"person_id": [alex_person_id, alex_person_id]}, "不能重复选择同一个人物"),
            (
                {"person_id": [alex_person_id, "00000000-0000-0000-0000-000000000000"]},
                "未找到可合并的人物",
            ),
        ]
        for payload, expected_message in invalid_cases:
            snapshot_before = _read_merge_slice_db_snapshot(library_db)
            response = httpx.post(
                f"{base_url}/people/merge",
                data=payload,
                follow_redirects=False,
                timeout=5.0,
            )
            assert response.status_code == 400
            assert expected_message in response.text
            assert _read_merge_slice_db_snapshot(library_db) == snapshot_before

        valid_merge_response = httpx.post(
            f"{base_url}/people/merge",
            data={"person_id": [casey_person_id, alex_person_id]},
            follow_redirects=False,
            timeout=5.0,
        )
        assert valid_merge_response.status_code == 303

        snapshot_before_inactive_attempt = _read_merge_slice_db_snapshot(library_db)
        inactive_response = httpx.post(
            f"{base_url}/people/merge",
            data={"person_id": [loser_person_id, blair_person_id]},
            follow_redirects=False,
            timeout=5.0,
        )
        assert inactive_response.status_code == 400
        assert "不能合并已失效的人物" in inactive_response.text
        assert _read_merge_slice_db_snapshot(library_db) == snapshot_before_inactive_attempt
    finally:
        _terminate_process(process)


def test_serve_undo_rejects_crafted_request_when_no_merge_exists(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-undo-no-merge"
    external_root = tmp_path / "external-root-undo-no-merge"
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0

    library_db = workspace / ".hikbox" / "library.db"
    snapshot_before = _read_merge_slice_db_snapshot(library_db)

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
        response = httpx.post(
            f"{base_url}/people/merge/undo",
            follow_redirects=False,
            timeout=5.0,
        )
        assert response.status_code == 400
        assert "当前没有可撤销的最近一次合并" in response.text
        assert _read_merge_slice_db_snapshot(library_db) == snapshot_before
    finally:
        _terminate_process(process)


def test_serve_merge_rolls_back_when_fault_injection_fails_mid_transaction(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-merge-fault"
    external_root = tmp_path / "external-root-merge-fault"
    manifest = _load_manifest()
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0

    scan_result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert scan_result.returncode == 0, scan_result.stderr

    library_db = workspace / ".hikbox" / "library.db"
    target_person_ids = _expected_target_mapping(library_db, manifest)
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]
    db_snapshot_before_merge = _read_merge_slice_db_snapshot(library_db)

    port = _find_free_port()
    process = _spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
        env_updates={"HIKBOX_TEST_MERGE_FAIL_STAGE": "after_assignment_migration"},
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_http_ready(f"{base_url}/")
        response = httpx.post(
            f"{base_url}/people/merge",
            data={"person_id": [casey_person_id, alex_person_id]},
            follow_redirects=False,
            timeout=5.0,
        )
        assert response.status_code == 500
        assert "人物合并失败" in response.text
        assert _read_merge_slice_db_snapshot(library_db) == db_snapshot_before_merge

        people_page = httpx.get(f"{base_url}/people", timeout=5.0)
        alex_detail = httpx.get(f"{base_url}/people/{alex_person_id}", timeout=5.0)
        casey_detail = httpx.get(f"{base_url}/people/{casey_person_id}", timeout=5.0)
        assert people_page.status_code == 200
        assert alex_person_id in people_page.text
        assert casey_person_id in people_page.text
        assert alex_detail.status_code == 200
        assert casey_detail.status_code == 200
    finally:
        _terminate_process(process)


def test_serve_undo_rolls_back_when_fault_injection_fails_mid_transaction(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-undo-fault"
    external_root = tmp_path / "external-root-undo-fault"
    manifest = _load_manifest()
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0

    scan_result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert scan_result.returncode == 0, scan_result.stderr

    library_db = workspace / ".hikbox" / "library.db"
    target_person_ids = _expected_target_mapping(library_db, manifest)
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]

    merge_port = _find_free_port()
    merge_process = _spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(merge_port),
    )
    merge_base_url = f"http://127.0.0.1:{merge_port}"
    try:
        _wait_for_http_ready(f"{merge_base_url}/")
        merge_response = httpx.post(
            f"{merge_base_url}/people/merge",
            data={"person_id": [casey_person_id, alex_person_id]},
            follow_redirects=False,
            timeout=5.0,
        )
        assert merge_response.status_code == 303
    finally:
        _terminate_process(merge_process)

    db_snapshot_before_undo_attempt = _read_merge_slice_db_snapshot(library_db)

    fault_port = _find_free_port()
    fault_process = _spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(fault_port),
        env_updates={"HIKBOX_TEST_UNDO_FAIL_STAGE": "after_assignment_restore"},
    )
    fault_base_url = f"http://127.0.0.1:{fault_port}"
    try:
        _wait_for_http_ready(f"{fault_base_url}/")
        response = httpx.post(
            f"{fault_base_url}/people/merge/undo",
            follow_redirects=False,
            timeout=5.0,
        )
        assert response.status_code == 500
        assert "撤销最近一次合并失败" in response.text
        assert _read_merge_slice_db_snapshot(library_db) == db_snapshot_before_undo_attempt

        merge_operations = _fetch_all(
            library_db,
            """
            SELECT id, undone_at
            FROM person_merge_operations
            ORDER BY id ASC
            """,
        )
        assert len(merge_operations) == 1
        assert merge_operations[0][1] is None
    finally:
        _terminate_process(fault_process)

    success_port = _find_free_port()
    success_process = _spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(success_port),
    )
    success_base_url = f"http://127.0.0.1:{success_port}"
    try:
        _wait_for_http_ready(f"{success_base_url}/")
        success_response = httpx.post(
            f"{success_base_url}/people/merge/undo",
            follow_redirects=False,
            timeout=5.0,
        )
        assert success_response.status_code == 303
        people_page = httpx.get(f"{success_base_url}/people", timeout=5.0)
        assert people_page.status_code == 200
        assert _read_person_page_status(success_base_url, alex_person_id) == 200
        assert _read_person_page_status(success_base_url, casey_person_id) == 200
        merge_operations = _fetch_all(
            library_db,
            """
            SELECT id, undone_at
            FROM person_merge_operations
            ORDER BY id ASC
            """,
        )
        assert len(merge_operations) == 1
        assert merge_operations[0][1] is not None
    finally:
        _terminate_process(success_process)


def test_serve_undo_allows_only_one_real_rollback_under_concurrency(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-undo-concurrency"
    external_root = tmp_path / "external-root-undo-concurrency"
    manifest = _load_manifest()
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0

    scan_result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert scan_result.returncode == 0, scan_result.stderr

    library_db = workspace / ".hikbox" / "library.db"
    target_person_ids = _expected_target_mapping(library_db, manifest)
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]

    merge_port = _find_free_port()
    merge_process = _spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(merge_port),
    )
    merge_base_url = f"http://127.0.0.1:{merge_port}"
    try:
        _wait_for_http_ready(f"{merge_base_url}/")
        merge_response = httpx.post(
            f"{merge_base_url}/people/merge",
            data={"person_id": [casey_person_id, alex_person_id]},
            follow_redirects=False,
            timeout=5.0,
        )
        assert merge_response.status_code == 303
    finally:
        _terminate_process(merge_process)

    trace_file = tmp_path / ".tmp" / "people-gallery-merge-undo" / "undo-overlap-trace.log"
    port = _find_free_port()
    process = _spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
        env_updates={
            "HIKBOX_TEST_UNDO_HOLD_SECONDS": "0.5",
            "HIKBOX_TEST_UNDO_TRACE_FILE": str(trace_file),
        },
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_http_ready(f"{base_url}/")

        def _post_undo() -> httpx.Response:
            return httpx.post(
                f"{base_url}/people/merge/undo",
                follow_redirects=False,
                timeout=10.0,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda _: _post_undo(), range(2)))

        status_codes = sorted(response.status_code for response in responses)
        assert status_codes == [303, 400]
        error_response = next(response for response in responses if response.status_code == 400)
        assert "最近一次成功合并已经撤销" in error_response.text or "当前没有可撤销的最近一次合并" in error_response.text
        trace_lines = trace_file.read_text(encoding="utf-8").splitlines()
        trace_events = [line.rsplit(" ", maxsplit=1)[1] for line in trace_lines]
        assert trace_events.count("handler_enter") == 2, trace_lines
        first_terminal_index = min(
            index
            for index, event in enumerate(trace_events)
            if event in {"request_succeeded", "validation_failed", "request_failed"}
        )
        second_handler_enter_index = [
            index for index, event in enumerate(trace_events) if event == "handler_enter"
        ][1]
        assert second_handler_enter_index < first_terminal_index, trace_lines

        merge_rows = _fetch_all(
            library_db,
            """
            SELECT id, winner_person_id, loser_person_id, undone_at
            FROM person_merge_operations
            ORDER BY id ASC
            """,
        )
        assert len(merge_rows) == 1
        assert merge_rows[0][3] is not None
        assert _read_person_page_status(base_url, alex_person_id) == 200
        assert _read_person_page_status(base_url, casey_person_id) == 200
    finally:
        _terminate_process(process)


def test_serve_undo_rejects_incomplete_merge_snapshot_without_db_changes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-undo-broken-snapshot"
    external_root = tmp_path / "external-root-undo-broken-snapshot"
    manifest = _load_manifest()
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0

    scan_result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert scan_result.returncode == 0, scan_result.stderr

    library_db = workspace / ".hikbox" / "library.db"
    target_person_ids = _expected_target_mapping(library_db, manifest)
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]

    merge_port = _find_free_port()
    merge_process = _spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(merge_port),
    )
    merge_base_url = f"http://127.0.0.1:{merge_port}"
    try:
        _wait_for_http_ready(f"{merge_base_url}/")
        merge_response = httpx.post(
            f"{merge_base_url}/people/merge",
            data={"person_id": [casey_person_id, alex_person_id]},
            follow_redirects=False,
            timeout=5.0,
        )
        assert merge_response.status_code == 303
    finally:
        _terminate_process(merge_process)

    snapshot_before_undo_attempt = _read_merge_slice_db_snapshot(library_db)

    port = _find_free_port()
    process = _spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
        env_updates={"HIKBOX_TEST_BREAK_LATEST_MERGE_SNAPSHOT": "1"},
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_http_ready(f"{base_url}/")
        response = httpx.post(
            f"{base_url}/people/merge/undo",
            follow_redirects=False,
            timeout=5.0,
        )
        assert response.status_code == 400
        assert "最近一次合并快照不完整" in response.text
        assert _read_merge_slice_db_snapshot(library_db) == snapshot_before_undo_attempt
        assert _read_person_page_status(base_url, alex_person_id) in {200, 404}
        assert _read_person_page_status(base_url, casey_person_id) in {200, 404}
    finally:
        _terminate_process(process)


def test_serve_undo_rejects_after_scan_invalidation_deletes_winner_assignment(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace-undo-invalidation"
    external_root = tmp_path / "external-root-undo-invalidation"
    source_dir = tmp_path / "scan-source"
    shutil.copytree(FIXTURE_DIR, source_dir)
    manifest = _load_manifest()
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, source_dir)
    assert add_result.returncode == 0

    scan_result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert scan_result.returncode == 0, scan_result.stderr

    library_db = workspace / ".hikbox" / "library.db"
    target_person_ids = _expected_target_mapping(library_db, manifest)
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]
    winner_person_id = min(alex_person_id, casey_person_id)

    merge_port = _find_free_port()
    merge_process = _spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(merge_port),
    )
    merge_base_url = f"http://127.0.0.1:{merge_port}"
    try:
        _wait_for_http_ready(f"{merge_base_url}/")
        merge_response = httpx.post(
            f"{merge_base_url}/people/merge",
            data={"person_id": [casey_person_id, alex_person_id]},
            follow_redirects=False,
            timeout=5.0,
        )
        assert merge_response.status_code == 303
    finally:
        _terminate_process(merge_process)

    merge_operation_row = _fetch_all(
        library_db,
        """
        SELECT winner_write_revision_after_merge
        FROM person_merge_operations
        ORDER BY id DESC
        LIMIT 1
        """,
    )[0]
    winner_revision_after_merge = int(merge_operation_row[0])
    target_file = next(
        str(asset["file"])
        for asset in manifest["assets"]
        if asset["expected_target_people"] == ["target_alex"]
    )
    (source_dir / target_file).write_bytes(b"not-a-valid-image-anymore")

    rescan_result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert rescan_result.returncode == 0, rescan_result.stderr
    assert _read_person_write_revision(library_db, winner_person_id) > winner_revision_after_merge

    undo_snapshot_before_attempt = _read_merge_slice_db_snapshot(library_db)
    undo_port = _find_free_port()
    undo_process = _spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(undo_port),
    )
    undo_base_url = f"http://127.0.0.1:{undo_port}"
    try:
        _wait_for_http_ready(f"{undo_base_url}/")
        undo_response = httpx.post(
            f"{undo_base_url}/people/merge/undo",
            follow_redirects=False,
            timeout=5.0,
        )
        assert undo_response.status_code == 400
        assert "合并之后已发生新的人物相关写入" in undo_response.text
        assert _read_merge_slice_db_snapshot(library_db) == undo_snapshot_before_attempt
    finally:
        _terminate_process(undo_process)
