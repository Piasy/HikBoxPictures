"""Feature Slice 3 AC-8：导出 running 期间 WebUI 控件禁用/隐藏 — Playwright 测试。"""

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

from playwright.sync_api import Page
from playwright.sync_api import expect
from playwright.sync_api import sync_playwright


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "people_gallery_scan"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"


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
    import httpx
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


def _write_blocking_hook_module(hook_module_dir: Path, block_file: Path) -> None:
    """创建 sitecustomize.py，配置 per-file-copy hook 在 block_file 存在时阻塞。"""
    hook_module_dir.mkdir(parents=True, exist_ok=True)
    (hook_module_dir / "sitecustomize.py").write_text(
        f'''
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


class TestWebUIExportLockingControls:
    """AC-8：导出 running 期间 WebUI 控件禁用/隐藏。"""

    def test_controls_disabled_during_export_running(self, scanned_workspace, tmp_path: Path) -> None:
        """验证导出 running 时四类控件 disabled。"""
        import httpx

        workspace, external_root, library_db, manifest, target_ids = scanned_workspace
        alex_id = target_ids["target_alex"]
        blair_id = target_ids["target_blair"]
        casey_id = target_ids["target_casey"]
        output_root = tmp_path / "export-output"

        # 创建 blocking hook
        block_file = tmp_path / "block_file"
        block_file.touch()
        hook_module_dir = tmp_path / "hook_module"
        _write_blocking_hook_module(hook_module_dir, block_file)

        # 先用普通 serve 做 merge（制造可撤销合并 + 匿名人物）
        port1 = _find_free_port()
        process1 = _spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port1))
        base_url1 = f"http://127.0.0.1:{port1}"
        try:
            _wait_for_http_ready(f"{base_url1}/")
            # 命名人物
            httpx.post(f"{base_url1}/people/{alex_id}/name", data={"display_name": "Alex Chen"}, follow_redirects=False, timeout=5.0)
            httpx.post(f"{base_url1}/people/{blair_id}/name", data={"display_name": "Blair Lin"}, follow_redirects=False, timeout=5.0)
            # 做一次合并以便有可撤销的 merge
            resp = httpx.post(f"{base_url1}/people/merge", data={"person_id": [alex_id, casey_id]}, follow_redirects=False, timeout=5.0)
            assert resp.status_code == 303, f"merge should succeed: {resp.status_code}"
        finally:
            _terminate_process(process1)

        # 用 blocking hook 启动 serve
        port = _find_free_port()
        process = _spawn_hikbox(
            "serve",
            "--workspace", str(workspace),
            "--port", str(port),
            pythonpath_prepend=[hook_module_dir],
        )
        base_url = f"http://127.0.0.1:{port}"
        try:
            _wait_for_http_ready(f"{base_url}/")

            # 创建模板（通过 API）
            resp = httpx.post(
                f"{base_url}/api/export-templates",
                data={
                    "name": "Alex & Blair",
                    "output_root": str(output_root),
                    "person_id": [alex_id, blair_id],
                },
                timeout=5.0,
            )
            resp.raise_for_status()
            template_id = resp.json()["template_id"]

            # 先调用 preview 填充 export_plan，否则 _run_export 无数据可复制，hook 不会触发
            preview_resp = httpx.get(
                f"{base_url}/api/export-templates/{template_id}/preview",
                timeout=30.0,
            )
            preview_resp.raise_for_status()

            # 在后台线程启动导出（将阻塞在 hook），主线程等待 running 记录出现
            import threading

            execute_result: list[httpx.Response | Exception] = []

            def _do_execute() -> None:
                try:
                    resp = httpx.post(
                        f"{base_url}/api/export-templates/{template_id}/execute",
                        timeout=120.0,
                    )
                    execute_result.append(resp)
                except Exception as exc:
                    execute_result.append(exc)

            execute_thread = threading.Thread(target=_do_execute, daemon=True)
            execute_thread.start()

            deadline = time.time() + 15
            running_count = 0
            while time.time() < deadline:
                running_count = _fetch_all(library_db, "SELECT COUNT(*) FROM export_run WHERE status = 'running'")[0][0]
                if running_count == 1:
                    break
                time.sleep(0.1)
            assert running_count == 1, f"应该有 1 条 running 记录: {running_count}"

            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 900})

                # ---- 首页控件禁用检查 ----
                page.goto(f"{base_url}/people")

                # 合并按钮应 disabled
                merge_button = page.locator("form[data-merge-form] button[type=submit]")
                expect(merge_button).to_be_disabled()

                # 合并 checkbox 应 disabled
                merge_checkboxes = page.locator("[data-merge-checkbox]")
                checkbox_count = merge_checkboxes.count()
                assert checkbox_count > 0, "首页至少应有 1 个合并 checkbox"
                for i in range(checkbox_count):
                    expect(merge_checkboxes.nth(i)).to_be_disabled()

                # 撤销合并按钮应 disabled
                undo_button = page.locator("[data-undo-submit]")
                expect(undo_button).to_be_disabled()

                # ---- 详情页控件禁用检查 ----
                page.goto(f"{base_url}/people/{alex_id}")

                # 命名输入框应 disabled
                name_input = page.locator("input#display_name")
                expect(name_input).to_be_disabled()

                # 命名保存按钮应 disabled
                save_button = page.locator("form[data-name-form] button[type=submit]")
                expect(save_button).to_be_disabled()

                # 排除按钮应 disabled
                exclude_button = page.locator("form[data-exclude-form] button[type=submit]")
                expect(exclude_button).to_be_disabled()

                # 排除 checkbox 应 disabled
                exclude_checkboxes = page.locator("[data-exclude-checkbox]")
                exc_count = exclude_checkboxes.count()
                assert exc_count > 0, "详情页至少应有 1 个排除 checkbox"
                for i in range(exc_count):
                    expect(exclude_checkboxes.nth(i)).to_be_disabled()

                # ---- 后端兜底：即使绕过 disabled，POST 仍返回 423 ----
                # 验证首页 merge POST 返回 423
                resp_merge = httpx.post(
                    f"{base_url}/people/merge",
                    data={"person_id": [alex_id, blair_id]},
                    follow_redirects=False,
                    timeout=5.0,
                )
                assert resp_merge.status_code == 423, (
                    f"绕过 disabled 的 merge POST 应返回 423: {resp_merge.status_code}"
                )
                assert "导出进行中" in resp_merge.text, f"响应应包含可读错误: {resp_merge.text[:200]}"

                # 验证详情页 name POST 返回 423
                resp_name = httpx.post(
                    f"{base_url}/people/{alex_id}/name",
                    data={"display_name": "Should Not Work"},
                    follow_redirects=False,
                    timeout=5.0,
                )
                assert resp_name.status_code == 423, (
                    f"绕过 disabled 的 name POST 应返回 423: {resp_name.status_code}"
                )
                assert "导出进行中" in resp_name.text, f"响应应包含可读错误: {resp_name.text[:200]}"

                browser.close()

            # 解除阻塞，验证恢复
            if block_file.exists():
                block_file.unlink()
            time.sleep(2)

            # 验证导出 completed 后，控件恢复
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 900})

                # 首页 — 控件应恢复
                page.goto(f"{base_url}/people")
                merge_button2 = page.locator("form[data-merge-form] button[type=submit]")
                expect(merge_button2).to_be_enabled()
                undo_button2 = page.locator("[data-undo-submit]")
                expect(undo_button2).to_be_enabled()
                merge_checkboxes2 = page.locator("[data-merge-checkbox]")
                assert merge_checkboxes2.count() > 0
                expect(merge_checkboxes2.first).to_be_enabled()

                # 详情页 — 控件应恢复
                page.goto(f"{base_url}/people/{alex_id}")
                name_input2 = page.locator("input#display_name")
                expect(name_input2).to_be_enabled()
                save_button2 = page.locator("form[data-name-form] button[type=submit]")
                expect(save_button2).to_be_enabled()
                exclude_button2 = page.locator("form[data-exclude-form] button[type=submit]")
                expect(exclude_button2).to_be_enabled()
                exclude_checkboxes2 = page.locator("[data-exclude-checkbox]")
                assert exclude_checkboxes2.count() > 0
                expect(exclude_checkboxes2.first).to_be_enabled()

                browser.close()
        finally:
            if block_file.exists():
                block_file.unlink()
            # 等待后台 execute 线程完成，避免进程终止时中断仍在进行的 HTTP 请求
            try:
                execute_thread.join(timeout=30)
            except NameError:
                pass
            _terminate_process(process)
