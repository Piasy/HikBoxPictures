"""pytest 共享 fixture：session 级金色工作区 + function 级复制。

将全量 fixture 的 init→scan 改为 session 级别只执行一次，
每个测试通过复制获得独立可写副本，大幅缩减测试耗时。
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path

import pytest

from tests.helpers import (
    REPO_ROOT,
    add_source,
    expected_target_mapping,
    fetch_all,
    init_workspace,
    load_manifest,
    run_hikbox,
)


FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "people_gallery_scan"

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
            manifest = load_manifest()
            _golden_state = {
                "workspace": workspace,
                "external_root": external_root,
                "manifest": manifest,
                "target_mapping": expected_target_mapping(library_db, manifest),
            }
            return _golden_state

        _clean_dir(base)
        base.mkdir(parents=True, exist_ok=True)

        manifest = load_manifest()

        init_result = init_workspace(workspace, external_root)
        assert init_result.returncode == 0, f"golden init 失败: {init_result.stderr}"
        add_result = add_source(workspace, FIXTURE_DIR)
        assert add_result.returncode == 0, f"golden source add 失败: {add_result.stderr}"
        scan_result = run_hikbox(
            "scan", "start", "--workspace", str(workspace), "--batch-size", "10",
        )
        assert scan_result.returncode == 0, f"golden scan 失败: {scan_result.stderr}"

        _golden_state = {
            "workspace": workspace,
            "external_root": external_root,
            "manifest": manifest,
            "target_mapping": expected_target_mapping(library_db, manifest),
        }
        return _golden_state


def _clean_dir(path: Path) -> None:
    """删除目录（如果存在）。"""
    if path.exists():
        shutil.rmtree(path)


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
    import json

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
    import sqlite3

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
