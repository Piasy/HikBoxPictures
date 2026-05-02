"""pytest 共享 fixture：session 级金色工作区 + function 级复制。

将全量 fixture 的 init→scan 改为 session 级别只执行一次，
每个测试通过复制获得独立可写副本，大幅缩减测试耗时。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "people_gallery_scan"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"

# ---------------------------------------------------------------------------
# 金色工作区全局状态（session 级别，通过懒加载 + 锁保证只构建一次）
# ---------------------------------------------------------------------------

_golden_lock = threading.Lock()
_golden_state: dict | None = None


def _get_golden_state() -> dict:
    """懒加载构建金色工作区（线程安全，整个进程生命周期只执行一次）。"""
    global _golden_state
    if _golden_state is not None:
        return _golden_state

    with _golden_lock:
        if _golden_state is not None:
            return _golden_state

        base = REPO_ROOT / ".tmp" / "golden-workspace"
        workspace = base / "workspace"
        external_root = base / "external-root"
        library_db = workspace / ".hikbox" / "library.db"

        # 如果金色工作区已存在且有效（例如被 pytest 加载的另一份 conftest 模块构建），直接复用
        if library_db.is_file():
            manifest = _load_manifest()
            _golden_state = {
                "workspace": workspace,
                "external_root": external_root,
                "manifest": manifest,
                "target_mapping": _expected_target_mapping(library_db, manifest),
            }
            return _golden_state

        _clean_dir(base)
        base.mkdir(parents=True, exist_ok=True)

        manifest = _load_manifest()

        init_result = _init_workspace(workspace, external_root)
        assert init_result.returncode == 0, f"golden init 失败: {init_result.stderr}"
        add_result = _add_source(workspace, FIXTURE_DIR)
        assert add_result.returncode == 0, f"golden source add 失败: {add_result.stderr}"
        scan_result = _run_hikbox(
            "scan", "start", "--workspace", str(workspace), "--batch-size", "10",
        )
        assert scan_result.returncode == 0, f"golden scan 失败: {scan_result.stderr}"

        _golden_state = {
            "workspace": workspace,
            "external_root": external_root,
            "manifest": manifest,
            "target_mapping": _expected_target_mapping(library_db, manifest),
        }
        return _golden_state


def _clean_dir(path: Path) -> None:
    """删除目录（如果存在）。"""
    if path.exists():
        shutil.rmtree(path)


# ---------------------------------------------------------------------------
# 底层 CLI helper（供 conftest 内部使用）
# ---------------------------------------------------------------------------


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


def _init_workspace(workspace: Path, external_root: Path) -> subprocess.CompletedProcess[str]:
    return _run_hikbox(
        "init", "--workspace", str(workspace), "--external-root", str(external_root),
    )


def _add_source(workspace: Path, source_dir: Path) -> subprocess.CompletedProcess[str]:
    return _run_hikbox("source", "add", "--workspace", str(workspace), str(source_dir))


def _load_manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _fetch_all(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(db_path)
    try:
        return [tuple(row) for row in connection.execute(sql, params).fetchall()]
    finally:
        connection.close()


def _expected_target_mapping(library_db: Path, manifest: dict[str, object]) -> dict[str, str]:
    """从扫描结果中建立目标人物标签 → person_id 的映射。"""
    rows = _fetch_all(
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
# 路径修复：复制工作区后更新绝对路径
# ---------------------------------------------------------------------------


def _fixup_workspace_paths(
    workspace: Path,
    new_external_root: Path,
    old_external_root: Path,
) -> None:
    """修复复制后的工作区中的绝对路径。"""
    _fixup_config_json(workspace, new_external_root)
    _fixup_face_observation_paths(workspace, new_external_root, old_external_root)


def _fixup_config_json(workspace: Path, new_external_root: Path) -> None:
    config_path = workspace / ".hikbox" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["external_root"] = str(new_external_root)
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")


def _fixup_face_observation_paths(
    workspace: Path,
    new_external_root: Path,
    old_external_root: Path,
) -> None:
    """将 face_observations 中 crop_path / context_path 的旧前缀替换为新前缀。"""
    library_db = workspace / ".hikbox" / "library.db"
    old_prefix = str(old_external_root)
    new_prefix = str(new_external_root)

    connection = sqlite3.connect(str(library_db))
    try:
        with connection:
            for col in ("crop_path", "context_path"):
                connection.execute(
                    f"UPDATE face_observations SET {col} = REPLACE({col}, ?, ?)",
                    (old_prefix, new_prefix),
                )
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 公共 API：复制已扫描工作区
# ---------------------------------------------------------------------------


def copy_scanned_workspace(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, object], dict[str, str]]:
    """从金色工作区复制出独立副本，修复路径后返回。

    返回与各测试文件中 _create_scanned_workspace() 相同的 5-tuple：
    (workspace, external_root, library_db, manifest, target_person_ids)
    """
    golden = _get_golden_state()
    golden_workspace: Path = golden["workspace"]
    golden_external_root: Path = golden["external_root"]
    manifest: dict[str, object] = golden["manifest"]
    target_mapping: dict[str, str] = golden["target_mapping"]

    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"

    # 复制 workspace（模型已统一存放在 .tmp/insightface_model/，workspace 内不含模型）
    shutil.copytree(str(golden_workspace), str(workspace))

    # 复制 external_root（包含 artifacts 产物文件）
    shutil.copytree(str(golden_external_root), str(external_root))

    # 修复绝对路径
    _fixup_workspace_paths(workspace, external_root, golden_external_root)

    library_db = workspace / ".hikbox" / "library.db"
    return workspace, external_root, library_db, manifest, target_mapping


# ---------------------------------------------------------------------------
# pytest fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def scanned_workspace(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, object], dict[str, str]]:
    """function 级 fixture：从金色工作区复制出独立副本。

    用法：在测试函数签名中添加 `scanned_workspace` 参数即可。
    """
    return copy_scanned_workspace(tmp_path)
