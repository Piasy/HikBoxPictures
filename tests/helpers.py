"""测试共享 helper 函数。

供各测试文件和 conftest.py 复用，避免跨文件重复定义。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import time

import httpx


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "people_gallery_scan"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"


# ---------------------------------------------------------------------------
# CLI 调用
# ---------------------------------------------------------------------------


def run_hikbox(
    *args: str,
    cwd: Path | None = None,
    env_updates: dict[str, str] | None = None,
    pythonpath_prepend: list[Path] | None = None,
    timeout: int | None = None,
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
        timeout=timeout,
        check=False,
    )


def spawn_hikbox(
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


def init_workspace(workspace: Path, external_root: Path) -> subprocess.CompletedProcess[str]:
    return run_hikbox(
        "init",
        "--workspace",
        str(workspace),
        "--external-root",
        str(external_root),
    )


def add_source(workspace: Path, source_dir: Path) -> subprocess.CompletedProcess[str]:
    return run_hikbox(
        "source",
        "add",
        "--workspace",
        str(workspace),
        str(source_dir),
    )


# ---------------------------------------------------------------------------
# 模型与 fixture
# ---------------------------------------------------------------------------


def find_model_root() -> Path:
    candidates = [REPO_ROOT / ".insightface", Path.home() / ".insightface"]
    candidates.extend(parent / ".insightface" for parent in REPO_ROOT.parents)
    for candidate in candidates:
        if (candidate / "models" / "buffalo_l" / "det_10g.onnx").exists():
            return candidate
    raise AssertionError("缺少 InsightFace buffalo_l 模型目录，无法执行集成测试")


def prepare_workspace_models(workspace: Path) -> None:
    source_root = find_model_root()
    target_root = workspace / ".hikbox" / "models" / "insightface"
    if target_root.exists():
        import shutil
        shutil.rmtree(target_root)
        shutil.copytree(source_root, target_root)
    else:
        import shutil
        shutil.copytree(source_root, target_root)


def load_manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 网络 / 服务
# ---------------------------------------------------------------------------


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(sock.getsockname()[1])


def wait_for_http_ready(base_url: str) -> None:
    deadline = time.time() + 30
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            response = httpx.get(base_url, follow_redirects=True, timeout=1.0)
            if response.status_code < 500:
                return
        except Exception as exc:
            last_error = exc
        time.sleep(0.2)
    raise AssertionError(f"等待服务可用超时: {base_url}; last_error={last_error!r}")


def terminate_process(process: subprocess.Popen[str]) -> tuple[str, str]:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    try:
        stdout_text, stderr_text = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout_text, stderr_text = process.communicate(timeout=30)
    return stdout_text, stderr_text


# ---------------------------------------------------------------------------
# 数据库查询
# ---------------------------------------------------------------------------


def fetch_all(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(db_path)
    try:
        return [tuple(row) for row in connection.execute(sql, params).fetchall()]
    finally:
        connection.close()


def expected_target_mapping(library_db: Path, manifest: dict[str, object]) -> dict[str, str]:
    rows = fetch_all(
        library_db,
        """
        SELECT
          assets.file_name,
          person_face_assignments.person_id
        FROM person_face_assignments
        INNER JOIN face_observations
          ON face_observations.id = person_face_assignments.face_observation_id
        INNER JOIN assets
          ON assets.id = face_observations.asset_id
        WHERE person_face_assignments.active = 1
        ORDER BY assets.file_name ASC
        """,
    )
    assignment_rows: dict[str, list[str]] = {}
    for file_name, person_id in rows:
        assignment_rows.setdefault(str(file_name), []).append(str(person_id))

    mapping: dict[str, str] = {}
    for label in manifest["expected_person_groups"]:
        observed_person_ids: set[str] = set()
        for asset in manifest["assets"]:
            if asset["expected_target_people"] != [label]:
                continue
            file_name = str(asset["file"])
            assigned = assignment_rows.get(file_name, [])
            if not assigned:
                continue
            observed_person_ids.update(assigned)
        assert observed_person_ids, f"{label} 缺少 target assignment"
        assert len(observed_person_ids) == 1, observed_person_ids
        mapping[str(label)] = next(iter(observed_person_ids))
    return mapping


# ---------------------------------------------------------------------------
# Web API 操作
# ---------------------------------------------------------------------------


def name_person_via_api(base_url: str, person_id: str, display_name: str) -> httpx.Response:
    return httpx.post(
        f"{base_url}/people/{person_id}/name",
        data={"display_name": display_name},
        follow_redirects=False,
        timeout=5.0,
    )


def merge_people_via_api(base_url: str, person_ids: list[str]) -> httpx.Response:
    return httpx.post(
        f"{base_url}/people/merge",
        data={"person_id": person_ids},
        follow_redirects=False,
        timeout=5.0,
    )


def create_template_via_api(
    base_url: str,
    *,
    name: str,
    person_ids: list[str],
    output_root: str,
) -> dict[str, object]:
    response = httpx.post(
        f"{base_url}/api/export-templates",
        data={
            "name": name,
            "output_root": output_root,
            "person_id": person_ids,
        },
        timeout=5.0,
    )
    response.raise_for_status()
    return response.json()


def list_templates_via_api(base_url: str) -> list[dict[str, object]]:
    response = httpx.get(f"{base_url}/api/export-templates", timeout=5.0)
    response.raise_for_status()
    return response.json()["templates"]
