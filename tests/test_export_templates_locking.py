"""Feature Slice 3 导出运行中人物写操作锁定 — 服务级集成测试。

覆盖 AC-1 到 AC-7：
- AC-1: name API 在 running 期间返回 423
- AC-2: merge API 在 running 期间返回 423
- AC-3: undo API 在 running 期间返回 423
- AC-4: exclude API 在 running 期间返回 423
- AC-5: 第二个模板执行返回 423；并发原子互斥
- AC-6: 导出完成后写 API 恢复
- AC-7: 服务启动时残留 running 标 failed
"""

from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time

import httpx
import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "people_gallery_scan"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"


# ---------------------------------------------------------------------------
# helpers (复刻自现有测试文件的模式)
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
    return _run_hikbox("init", "--workspace", str(workspace), "--external-root", str(external_root))


def _add_source(workspace: Path, source_dir: Path) -> subprocess.CompletedProcess[str]:
    return _run_hikbox("source", "add", "--workspace", str(workspace), str(source_dir))


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
    raise AssertionError("缺少 InsightFace buffalo_l 模型目录，无法执行集成测试")


def _load_manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(sock.getsockname()[1])


def _wait_for_http_ready(base_url: str) -> None:
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


def _execute_sql(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(sql, params)
        connection.commit()
    finally:
        connection.close()


def _expected_target_mapping(library_db: Path, manifest: dict[str, object]) -> dict[str, str]:
    rows = _fetch_all(
        library_db,
        """
        SELECT assets.file_name, person_face_assignments.person_id
        FROM person_face_assignments
        INNER JOIN face_observations ON face_observations.id = person_face_assignments.face_observation_id
        INNER JOIN assets ON assets.id = face_observations.asset_id
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
# blocking hook module — 让导出在文件复制阶段阻塞以维持 running 状态
# ---------------------------------------------------------------------------


def _write_blocking_hook_module(hook_module_dir: Path, block_file: Path) -> None:
    """创建一个 sitecustomize.py，配置 per-file-copy hook 在 block_file 存在时阻塞。"""
    hook_module_dir.mkdir(parents=True, exist_ok=True)
    (hook_module_dir / "sitecustomize.py").write_text(
        f'''
import json
import os
import time

import hikbox_pictures.product.export_templates as et

def make_hook():
    block_file = {repr(str(block_file))}

    def hook():
        while os.path.exists(block_file):
            time.sleep(0.05)

    return hook

et.set_per_file_copy_hook(make_hook())
''',
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 共用 setup：创建扫描后的 workspace，命名人物，创建模板，启动 serve
# ---------------------------------------------------------------------------


class _LockingTestContext:
    """持有一次锁定测试所需的所有资源。"""

    def __init__(
        self,
        tmp_path: Path,
        *,
        name_alex: str = "Alex Chen",
        name_blair: str = "Blair Lin",
    ) -> None:
        self.workspace: Path
        self.library_db: Path
        self.manifest: dict[str, object]
        self.target_ids: dict[str, str]
        self.alex_id: str
        self.blair_id: str
        self.template_id: str
        self.output_root: Path
        self.port: int
        self.base_url: str
        self.process: subprocess.Popen[str] | None = None
        self.block_file: Path | None = None
        self.hook_module_dir: Path | None = None
        self.tmp_path = tmp_path
        self._name_alex = name_alex
        self._name_blair = name_blair

    def setup_baseline(self) -> None:
        """创建扫描 workspace、命名人物、创建模板（不启动 serve）。"""
        workspace = self.tmp_path / "workspace"
        external_root = self.tmp_path / "external-root"
        manifest = _load_manifest()
        init_result = _init_workspace(workspace, external_root)
        assert init_result.returncode == 0, init_result.stderr
        _prepare_workspace_models(workspace)
        add_result = _add_source(workspace, FIXTURE_DIR)
        assert add_result.returncode == 0, add_result.stderr
        scan_result = _run_hikbox("scan", "start", "--workspace", str(workspace), "--batch-size", "10")
        assert scan_result.returncode == 0, scan_result.stderr
        library_db = workspace / ".hikbox" / "library.db"
        target_ids = _expected_target_mapping(library_db, manifest)
        alex_id = target_ids["target_alex"]
        blair_id = target_ids["target_blair"]
        output_root = self.tmp_path / "export-output"

        self.workspace = workspace
        self.library_db = library_db
        self.manifest = manifest
        self.target_ids = target_ids
        self.alex_id = alex_id
        self.blair_id = blair_id
        self.output_root = output_root

    def setup_serve(self) -> None:
        """启动 serve 进程（不含 blocking hook）。"""
        port = _find_free_port()
        process = _spawn_hikbox("serve", "--workspace", str(self.workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        self.port = port
        self.base_url = base_url
        self.process = process
        _wait_for_http_ready(f"{base_url}/")

    def setup_serve_with_blocking_hook(self) -> None:
        """启动带 blocking hook 的 serve 进程。"""
        self.block_file = self.tmp_path / "block_file"
        self.block_file.touch()
        self.hook_module_dir = self.tmp_path / "hook_module"
        _write_blocking_hook_module(self.hook_module_dir, self.block_file)

        port = _find_free_port()
        process = _spawn_hikbox(
            "serve",
            "--workspace", str(self.workspace),
            "--port", str(port),
            pythonpath_prepend=[self.hook_module_dir],
        )
        base_url = f"http://127.0.0.1:{port}"
        self.port = port
        self.base_url = base_url
        self.process = process
        self.hook_module_dir = self.hook_module_dir
        _wait_for_http_ready(f"{base_url}/")

    def unblock_export(self) -> None:
        """移除 block_file，让阻塞的导出继续执行。"""
        if self.block_file is not None and self.block_file.exists():
            self.block_file.unlink()

    def name_people(self) -> None:
        """通过真实 API 命名 alex 和 blair。"""
        _name_person_via_api(self.base_url, self.alex_id, self._name_alex)
        _name_person_via_api(self.base_url, self.blair_id, self._name_blair)

    def create_template(self, name: str = "Alex & Blair", *, output_root: str | None = None) -> str:
        """通过真实 API 创建模板，返回 template_id。"""
        root = output_root if output_root is not None else str(self.output_root)
        response = httpx.post(
            f"{self.base_url}/api/export-templates",
            data={
                "name": name,
                "output_root": root,
                "person_id": [self.alex_id, self.blair_id],
            },
            timeout=5.0,
        )
        response.raise_for_status()
        self.template_id = response.json()["template_id"]
        return self.template_id

    def start_blocking_export(self) -> int:
        """在后台线程中发起 POST execute（将阻塞在 hook），等待 export_run 出现后返回。

        返回 run_id，如果未能在超时前获得 running 记录则返回 -1。
        """
        import threading

        # 在后台线程中发起请求（会被 hook 阻塞）
        result_container: list[dict[str, object] | Exception] = []

        def _do_execute() -> None:
            try:
                resp = httpx.post(
                    f"{self.base_url}/api/export-templates/{self.template_id}/execute",
                    timeout=120.0,
                )
                if resp.status_code == 200:
                    result_container.append(resp.json())
                elif resp.status_code == 423:
                    result_container.append({"status_code": 423, "detail": resp.text})
                else:
                    result_container.append(
                        RuntimeError(f"execute returned {resp.status_code}: {resp.text[:200]}")
                    )
            except Exception as exc:
                result_container.append(exc)

        thread = threading.Thread(target=_do_execute, daemon=True)
        thread.start()

        # 等待 export_run 出现
        deadline = time.time() + 15
        run_id = -1
        while time.time() < deadline:
            rows = _fetch_all(self.library_db, "SELECT run_id FROM export_run WHERE status = 'running'")
            if rows:
                run_id = int(rows[0][0])
                break
            time.sleep(0.1)

        if run_id == -1:
            # 可能 execute 已经完成（太快了）或失败了
            thread.join(timeout=3)
            if result_container:
                result = result_container[0]
                if isinstance(result, dict) and "run_id" in result:
                    run_id = int(result["run_id"])
                elif isinstance(result, dict) and result.get("status_code") == 423:
                    run_id = -1  # 已有 running

        self._execute_thread = thread
        return run_id

    def teardown(self) -> None:
        self.unblock_export()
        if self.process is not None:
            _terminate_process(self.process)


# ---------------------------------------------------------------------------
# API helper wrappers
# ---------------------------------------------------------------------------


def _name_person_via_api(base_url: str, person_id: str, display_name: str) -> httpx.Response:
    return httpx.post(
        f"{base_url}/people/{person_id}/name",
        data={"display_name": display_name},
        follow_redirects=False,
        timeout=5.0,
    )


def _merge_people_via_api(base_url: str, person_ids: list[str]) -> httpx.Response:
    return httpx.post(
        f"{base_url}/people/merge",
        data={"person_id": person_ids},
        follow_redirects=False,
        timeout=5.0,
    )


def _undo_merge_via_api(base_url: str) -> httpx.Response:
    return httpx.post(
        f"{base_url}/people/merge/undo",
        follow_redirects=False,
        timeout=5.0,
    )


def _exclude_person_via_api(base_url: str, person_id: str, assignment_ids: list[str]) -> httpx.Response:
    return httpx.post(
        f"{base_url}/people/{person_id}/exclude",
        data={"assignment_id": assignment_ids},
        follow_redirects=False,
        timeout=5.0,
    )


def _execute_template_via_api(base_url: str, template_id: str) -> httpx.Response:
    return httpx.post(
        f"{base_url}/api/export-templates/{template_id}/execute",
        timeout=30.0,
    )


# ---------------------------------------------------------------------------
# AC-1: 命名 API 在 running 期间返回 423
# ---------------------------------------------------------------------------


class TestExportLockingNameAPI:
    """AC-1：导出 running 时 POST /people/{id}/name 返回 423，DB 不变。"""

    def test_name_api_returns_423_during_export(self, tmp_path: Path) -> None:
        ctx = _LockingTestContext(tmp_path)
        try:
            ctx.setup_baseline()
            ctx.setup_serve_with_blocking_hook()
            ctx.name_people()
            ctx.create_template()

            # 启动导出（将阻塞在 hook）
            ctx.start_blocking_export()
            # 短暂等待确保 export_run 已写入
            time.sleep(0.5)

            # 验证 DB 中确实有 running 记录
            running_before = _fetch_all(ctx.library_db, "SELECT run_id, status FROM export_run WHERE status = 'running'")
            assert len(running_before) == 1, f"应该有一条 running 记录: {running_before}"

            # 获取 alex 当前 name 状态
            name_before = _fetch_all(
                ctx.library_db,
                "SELECT display_name, is_named FROM person WHERE id = ?",
                (ctx.alex_id,),
            )
            rename_count_before = _fetch_all(
                ctx.library_db,
                "SELECT COUNT(*) FROM person_name_events",
            )[0][0]

            # 发起命名请求
            response = _name_person_via_api(ctx.base_url, ctx.alex_id, "Should Not Work")
            assert response.status_code == 423, (
                f"期望 423 Locked，实际 {response.status_code}: {response.text[:200]}"
            )
            assert "导出进行中" in response.text, f"响应应包含可读错误: {response.text[:200]}"

            # DB 不变
            name_after = _fetch_all(
                ctx.library_db,
                "SELECT display_name, is_named FROM person WHERE id = ?",
                (ctx.alex_id,),
            )
            rename_count_after = _fetch_all(
                ctx.library_db,
                "SELECT COUNT(*) FROM person_name_events",
            )[0][0]
            assert name_after == name_before, f"display_name 不应变化: {name_before} -> {name_after}"
            assert rename_count_after == rename_count_before, "rename log 不应新增"
        finally:
            ctx.teardown()


# ---------------------------------------------------------------------------
# AC-2: merge API 在 running 期间返回 423
# ---------------------------------------------------------------------------


class TestExportLockingMergeAPI:
    """AC-2：导出 running 时 POST /people/merge 返回 423，DB 不变。"""

    def test_merge_api_returns_423_during_export(self, tmp_path: Path) -> None:
        ctx = _LockingTestContext(tmp_path)
        try:
            ctx.setup_baseline()
            ctx.setup_serve_with_blocking_hook()
            ctx.name_people()
            ctx.create_template()

            # 准备一个匿名人物作为合并目标
            casey_id = ctx.target_ids["target_casey"]

            # 启动导出（将阻塞在 hook）
            ctx.start_blocking_export()
            time.sleep(0.5)

            # 记录合并前的状态
            status_before = _fetch_all(
                ctx.library_db,
                "SELECT id, status FROM person WHERE id IN (?, ?)",
                (ctx.alex_id, casey_id),
            )
            merge_count_before = _fetch_all(
                ctx.library_db,
                "SELECT COUNT(*) FROM person_merge_operations",
            )[0][0]

            # 发起合并请求
            response = _merge_people_via_api(ctx.base_url, [ctx.alex_id, casey_id])
            assert response.status_code == 423, (
                f"期望 423 Locked，实际 {response.status_code}: {response.text[:200]}"
            )
            assert "导出进行中" in response.text, f"响应应包含可读错误: {response.text[:200]}"

            # DB 不变
            status_after = _fetch_all(
                ctx.library_db,
                "SELECT id, status FROM person WHERE id IN (?, ?)",
                (ctx.alex_id, casey_id),
            )
            merge_count_after = _fetch_all(
                ctx.library_db,
                "SELECT COUNT(*) FROM person_merge_operations",
            )[0][0]
            assert status_after == status_before, f"person status 不应变化: {status_before} -> {status_after}"
            assert merge_count_after == merge_count_before, "person_merge_operations 不应新增"
        finally:
            ctx.teardown()


# ---------------------------------------------------------------------------
# AC-3: undo API 在 running 期间返回 423
# ---------------------------------------------------------------------------


class TestExportLockingUndoAPI:
    """AC-3：导出 running 时 POST /people/merge/undo 返回 423，DB 不变。"""

    def test_undo_api_returns_423_during_export(self, tmp_path: Path) -> None:
        ctx = _LockingTestContext(tmp_path)
        try:
            ctx.setup_baseline()
            # 先用普通 serve 做一次 merge（为 undo 做准备）
            ctx.setup_serve()
            ctx.name_people()
            ctx.create_template()

            casey_id = ctx.target_ids["target_casey"]
            merge_resp = _merge_people_via_api(ctx.base_url, [ctx.alex_id, casey_id])
            assert merge_resp.status_code == 303, f"merge 应该成功: {merge_resp.status_code}"

            # 验证有可撤销的 merge
            undo_count_before = _fetch_all(
                ctx.library_db,
                "SELECT COUNT(*) FROM person_merge_operations WHERE undone_at IS NULL",
            )[0][0]
            assert undo_count_before > 0, "应该有可撤销的合并操作"

            # 停掉普通 serve，启动带 blocking hook 的 serve
            _terminate_process(ctx.process)
            ctx.setup_serve_with_blocking_hook()

            # 启动导出（将阻塞在 hook）
            ctx.start_blocking_export()
            time.sleep(0.5)

            # 记录撤销前的状态
            merge_rows_before = _fetch_all(
                ctx.library_db,
                "SELECT id, undone_at FROM person_merge_operations ORDER BY id",
            )

            # 发起撤销请求
            response = _undo_merge_via_api(ctx.base_url)
            assert response.status_code == 423, (
                f"期望 423 Locked，实际 {response.status_code}: {response.text[:200]}"
            )
            assert "导出进行中" in response.text, f"响应应包含可读错误: {response.text[:200]}"

            # DB 不变
            merge_rows_after = _fetch_all(
                ctx.library_db,
                "SELECT id, undone_at FROM person_merge_operations ORDER BY id",
            )
            assert merge_rows_after == merge_rows_before, "person_merge_operations undone 状态不应变化"
        finally:
            ctx.teardown()


# ---------------------------------------------------------------------------
# AC-4: exclude API 在 running 期间返回 423
# ---------------------------------------------------------------------------


class TestExportLockingExcludeAPI:
    """AC-4：导出 running 时 POST /people/{id}/exclude 返回 423，DB 不变。"""

    def test_exclude_api_returns_423_during_export(self, tmp_path: Path) -> None:
        ctx = _LockingTestContext(tmp_path)
        try:
            ctx.setup_baseline()
            ctx.setup_serve_with_blocking_hook()
            ctx.name_people()
            ctx.create_template()

            # 获取 alex 的一些 assignment ids
            assignment_rows = _fetch_all(
                ctx.library_db,
                "SELECT id FROM person_face_assignments WHERE person_id = ? AND active = 1",
                (ctx.alex_id,),
            )
            assert len(assignment_rows) >= 1, "alex 至少需要 1 个 active assignment"
            assignment_ids = [str(r[0]) for r in assignment_rows[:2]]

            # 启动导出（将阻塞在 hook）
            ctx.start_blocking_export()
            time.sleep(0.5)

            # 记录排除前的状态
            active_before = _fetch_all(
                ctx.library_db,
                "SELECT COUNT(*) FROM person_face_assignments WHERE person_id = ? AND active = 1",
                (ctx.alex_id,),
            )[0][0]
            exclusion_count_before = _fetch_all(
                ctx.library_db,
                "SELECT COUNT(*) FROM person_face_exclusions",
            )[0][0]

            # 发起排除请求
            response = _exclude_person_via_api(ctx.base_url, ctx.alex_id, assignment_ids)
            assert response.status_code == 423, (
                f"期望 423 Locked，实际 {response.status_code}: {response.text[:200]}"
            )
            assert "导出进行中" in response.text, f"响应应包含可读错误: {response.text[:200]}"

            # DB 不变
            active_after = _fetch_all(
                ctx.library_db,
                "SELECT COUNT(*) FROM person_face_assignments WHERE person_id = ? AND active = 1",
                (ctx.alex_id,),
            )[0][0]
            exclusion_count_after = _fetch_all(
                ctx.library_db,
                "SELECT COUNT(*) FROM person_face_exclusions",
            )[0][0]
            assert active_after == active_before, f"active assignments 不应变化: {active_before} -> {active_after}"
            assert exclusion_count_after == exclusion_count_before, "person_face_exclusions 不应新增"
        finally:
            ctx.teardown()


# ---------------------------------------------------------------------------
# AC-5: 第二个模板执行返回 423；并发原子互斥
# ---------------------------------------------------------------------------


class TestExportLockingConcurrentExecute:
    """AC-5：两个同时执行的模板只有一个能创建 running，另一个得 423。"""

    def test_second_execute_returns_423_during_running(self, tmp_path: Path) -> None:
        """子情形 (a)：第一个 export running 期间第二个模板执行返回 423。"""
        ctx = _LockingTestContext(tmp_path)
        try:
            ctx.setup_baseline()
            ctx.setup_serve_with_blocking_hook()
            ctx.name_people()
            template_id_1 = ctx.create_template(name="Template One")

            # 启动第一个导出（阻塞在 hook）
            ctx.start_blocking_export()
            time.sleep(0.5)

            # 验证只有一个 running 记录
            running_count = _fetch_all(ctx.library_db, "SELECT COUNT(*) FROM export_run WHERE status = 'running'")[0][0]
            assert running_count == 1

            # 创建第二个模板（不同 output_root 避免去重）
            template_id_2 = ctx.create_template(name="Template Two", output_root=str(ctx.tmp_path / "export-output-2"))

            # 尝试执行第二个模板
            response = _execute_template_via_api(ctx.base_url, template_id_2)
            assert response.status_code == 423, (
                f"期望 423 Locked，实际 {response.status_code}: {response.text[:200]}"
            )
            assert "已有导出" in response.text or "导出进行中" in response.text, (
                f"响应应包含可读错误: {response.text[:200]}"
            )

            # DB 中仍然只有一个 running 记录
            running_count_after = _fetch_all(ctx.library_db, "SELECT COUNT(*) FROM export_run WHERE status = 'running'")[0][0]
            assert running_count_after == 1, f"仍应只有 1 个 running: {running_count_after}"

            # 总 export_run 行数不变（没有新 run 产生）
            total_runs = _fetch_all(ctx.library_db, "SELECT COUNT(*) FROM export_run")[0][0]
            assert total_runs == 1, f"总 export_run 行数应为 1: {total_runs}"
        finally:
            ctx.teardown()

    def test_concurrent_execute_only_one_succeeds(self, tmp_path: Path) -> None:
        """子情形 (b)：并发发送两个模板执行请求，只有一个成功。"""
        ctx = _LockingTestContext(tmp_path)
        try:
            ctx.setup_baseline()
            ctx.setup_serve_with_blocking_hook()
            ctx.name_people()
            template_id_1 = ctx.create_template(name="Template One", output_root=str(ctx.tmp_path / "export-output-1"))
            template_id_2 = ctx.create_template(name="Template Two", output_root=str(ctx.tmp_path / "export-output-2"))

            # 使用线程并发发送两个执行请求
            results: list[httpx.Response] = []
            semaphore = threading.Event()

            def execute(tid: str) -> None:
                # 等待信号同时开始
                semaphore.wait()
                try:
                    resp = _execute_template_via_api(ctx.base_url, tid)
                except httpx.HTTPStatusError as exc:
                    resp = exc.response
                except Exception as exc:
                    resp = httpx.Response(status_code=500)
                    resp._content = str(exc).encode()
                results.append(resp)

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(execute, template_id_1)
                f2 = executor.submit(execute, template_id_2)
                # 让两个线程同时开始
                time.sleep(0.1)
                semaphore.set()

                # 等待一小段时间让 running 记录出现
                time.sleep(1.0)

                # 验证 DB 中只有 1 个 running 记录
                running_count = _fetch_all(ctx.library_db, "SELECT COUNT(*) FROM export_run WHERE status = 'running'")[0][0]
                assert running_count == 1, f"应该有恰好 1 个 running: {running_count}"

                # 此时应该有一个请求返回了 423（第二个请求被拦截）
                # 另一个请求被 hook 阻塞，尚未返回

                # 解除阻塞让成功请求完成
                ctx.unblock_export()

                # 等待所有线程完成
                concurrent.futures.wait([f1, f2], timeout=60)

            assert len(results) == 2, f"应该有 2 个响应: {len(results)}"

            success_codes = [r.status_code for r in results if r.status_code == 200]
            locked_codes = [r.status_code for r in results if r.status_code == 423]
            other_codes = [
                r.status_code for r in results if r.status_code not in (200, 423)
            ]

            assert len(success_codes) == 1, (
                f"应该恰好 1 个 200: success={success_codes} locked={locked_codes} other={other_codes}"
            )
            assert len(locked_codes) == 1, (
                f"应该恰好 1 个 423: success={success_codes} locked={locked_codes} other={other_codes}"
            )

            # DB 中最多 1 个 running 记录
            running_count = _fetch_all(ctx.library_db, "SELECT COUNT(*) FROM export_run WHERE status = 'running'")[0][0]
            assert running_count <= 1, f"running count 应 <= 1: {running_count}"
        finally:
            ctx.teardown()


# ---------------------------------------------------------------------------
# AC-6: 导出完成后写 API 恢复
# ---------------------------------------------------------------------------


class TestExportLockingCompletedRecovery:
    """AC-6：导出 completed/failed 后写 API 恢复正常。"""

    def test_name_api_works_after_export_completed(self, tmp_path: Path) -> None:
        ctx = _LockingTestContext(tmp_path, name_alex="Alex Chen")
        try:
            ctx.setup_baseline()
            ctx.setup_serve_with_blocking_hook()
            ctx.name_people()
            ctx.create_template()

            # 启动导出（阻塞）
            ctx.start_blocking_export()
            time.sleep(0.5)

            # 验证 running 期间命名返回 423
            response = _name_person_via_api(ctx.base_url, ctx.alex_id, "During Export")
            assert response.status_code == 423

            # 解除阻塞，等待导出完成
            ctx.unblock_export()
            time.sleep(2)

            # 验证 export_run status 变为 completed
            run_status = _fetch_all(
                ctx.library_db,
                "SELECT status FROM export_run ORDER BY run_id DESC LIMIT 1",
            )[0][0]
            assert run_status == "completed", f"导出应已完成: {run_status}"

            # 现在命名应该成功
            response2 = _name_person_via_api(ctx.base_url, ctx.alex_id, "Alex Renamed")
            assert response2.status_code in (302, 303), (
                f"导出完成后命名应成功，实际 {response2.status_code}: {response2.text[:200]}"
            )

            # DB 应已更新
            display_name = _fetch_all(
                ctx.library_db,
                "SELECT display_name FROM person WHERE id = ?",
                (ctx.alex_id,),
            )[0][0]
            assert display_name == "Alex Renamed", f"display_name 应已更新: {display_name}"
        finally:
            ctx.teardown()

    def test_name_api_works_after_export_failed(self, tmp_path: Path) -> None:
        """子情形：导出 failed 后写 API 也应恢复。"""
        import os

        ctx = _LockingTestContext(tmp_path)
        try:
            ctx.setup_baseline()
            ctx.setup_serve()
            ctx.name_people()

            # 使用只读目录作为 output_root 制造真实 failed 导出
            readonly_output = tmp_path / "readonly-export"
            readonly_output.mkdir()
            os.chmod(readonly_output, 0o555)
            template_id = ctx.create_template(output_root=str(readonly_output))

            response = _execute_template_via_api(ctx.base_url, template_id)
            assert response.status_code == 500, f"导出应失败: {response.status_code}"

            # 验证 export_run 为 failed
            run_status = _fetch_all(
                ctx.library_db,
                "SELECT status FROM export_run ORDER BY run_id DESC LIMIT 1",
            )[0][0]
            assert run_status == "failed", f"导出应标为 failed: {run_status}"

            # 验证 failed 后命名 API 恢复正常
            resp = _name_person_via_api(ctx.base_url, ctx.alex_id, "Alex After Failed")
            assert resp.status_code in (302, 303), f"导出失败后命名应成功: {resp.status_code}"

            display_name = _fetch_all(
                ctx.library_db,
                "SELECT display_name FROM person WHERE id = ?",
                (ctx.alex_id,),
            )[0][0]
            assert display_name == "Alex After Failed", f"display_name 应已更新: {display_name}"
        finally:
            os.chmod(readonly_output, 0o755)
            ctx.teardown()

    def test_all_write_apis_recover_after_export_completed(self, tmp_path: Path) -> None:
        """全面验证：导出完成后所有 4 类写 API 恢复。"""
        ctx = _LockingTestContext(tmp_path)
        try:
            ctx.setup_baseline()
            ctx.setup_serve_with_blocking_hook()
            ctx.name_people()
            ctx.create_template()

            # 先做一次 merge 以便后续有可撤销的 undo
            casey_id = ctx.target_ids["target_casey"]
            merge_resp = _merge_people_via_api(ctx.base_url, [ctx.alex_id, casey_id])
            assert merge_resp.status_code == 303, f"merge 应该成功: {merge_resp.status_code}"

            # 启动导出（阻塞）
            ctx.start_blocking_export()
            time.sleep(0.5)

            # 验证所有 API 都返回 423
            resp_name = _name_person_via_api(ctx.base_url, ctx.alex_id, "Blocked")
            assert resp_name.status_code == 423

            resp_merge = _merge_people_via_api(ctx.base_url, [ctx.alex_id, ctx.blair_id])
            assert resp_merge.status_code == 423

            resp_undo = _undo_merge_via_api(ctx.base_url)
            assert resp_undo.status_code == 423

            # 获取 alex 的 assignment ids 用于 exclude 测试
            assignment_rows = _fetch_all(
                ctx.library_db,
                "SELECT id FROM person_face_assignments WHERE person_id = ? AND active = 1",
                (ctx.alex_id,),
            )
            assignment_ids = [str(r[0]) for r in assignment_rows[:1]]
            resp_exclude = _exclude_person_via_api(ctx.base_url, ctx.alex_id, assignment_ids)
            assert resp_exclude.status_code == 423

            # 解除阻塞，等待导出完成
            ctx.unblock_export()
            time.sleep(2)

            # 验证导出已完成
            run_status = _fetch_all(ctx.library_db, "SELECT status FROM export_run ORDER BY run_id DESC LIMIT 1")[0][0]
            assert run_status == "completed"

            # 现在命名应成功
            resp_name2 = _name_person_via_api(ctx.base_url, ctx.alex_id, "New Name")
            assert resp_name2.status_code in (302, 303)

            # 合并应成功（至少不会返回 423）
            resp_merge2 = _merge_people_via_api(ctx.base_url, [ctx.alex_id, ctx.blair_id])
            assert resp_merge2.status_code not in (423,), f"合并不应返回 423: {resp_merge2.status_code}"

            # undo 应成功（至少不会返回 423）
            resp_undo2 = _undo_merge_via_api(ctx.base_url)
            assert resp_undo2.status_code not in (423,), f"undo 不应返回 423: {resp_undo2.status_code}"

            # exclude 应成功（至少不会返回 423）
            resp_exclude2 = _exclude_person_via_api(ctx.base_url, ctx.alex_id, assignment_ids)
            assert resp_exclude2.status_code not in (423,), f"exclude 不应返回 423: {resp_exclude2.status_code}"
        finally:
            ctx.teardown()


# ---------------------------------------------------------------------------
# AC-7: 服务启动时残留 running 标 failed
# ---------------------------------------------------------------------------


class TestExportLockingStaleRunningCleanup:
    """AC-7：服务启动时将残留 running 记录标记为 failed，解除锁定。"""

    def test_stale_running_cleaned_up_on_serve_start(self, tmp_path: Path) -> None:
        ctx = _LockingTestContext(tmp_path)
        try:
            ctx.setup_baseline()
            # 使用普通 serve 创建模板、命名人物
            ctx.setup_serve()
            ctx.name_people()
            ctx.create_template()

            # 执行一次正常导出以获得 template_id 关联的合法 run
            resp = httpx.post(
                f"{ctx.base_url}/api/export-templates/{ctx.template_id}/execute",
                timeout=30.0,
            )
            assert resp.status_code == 200

            # 停掉 serve
            _terminate_process(ctx.process)

            # 直接修改 DB 制造一条残留 running 记录（AC-7 允许的降级手段）
            _execute_sql(
                ctx.library_db,
                """
                UPDATE export_run
                SET status = 'running', completed_at = NULL
                WHERE run_id = (SELECT MAX(run_id) FROM export_run)
                """,
            )

            # 验证 DB 中有 running 记录
            running_before = _fetch_all(ctx.library_db, "SELECT COUNT(*) FROM export_run WHERE status = 'running'")[0][0]
            assert running_before == 1, f"重启前应有 running 记录: {running_before}"

            # 重启 serve（不带 blocking hook）
            port = _find_free_port()
            process2 = _spawn_hikbox("serve", "--workspace", str(ctx.workspace), "--port", str(port))
            base_url2 = f"http://127.0.0.1:{port}"
            _wait_for_http_ready(f"{base_url2}/")

            try:
                # 验证残留 running 已被标为 failed
                running_after = _fetch_all(ctx.library_db, "SELECT COUNT(*) FROM export_run WHERE status = 'running'")[0][0]
                assert running_after == 0, f"重启后不应有 running 记录: {running_after}"

                stale_row = _fetch_all(
                    ctx.library_db,
                    "SELECT status FROM export_run ORDER BY run_id DESC LIMIT 1",
                )[0]
                assert stale_row[0] == "failed", f"残留记录应标为 failed: {stale_row}"

                # 验证锁定已解除 —— 命名 API 可用
                resp_name = _name_person_via_api(base_url2, ctx.alex_id, "After Cleanup")
                assert resp_name.status_code in (302, 303), (
                    f"残留清理后命名应成功: {resp_name.status_code}"
                )
            finally:
                _terminate_process(process2)
        finally:
            ctx.teardown()
