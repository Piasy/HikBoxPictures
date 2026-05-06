"""Feature Slice 3 AC-8：导出 running 期间 WebUI 控件禁用/隐藏 — Playwright 测试。"""

from __future__ import annotations

from pathlib import Path
import time

import httpx
from playwright.sync_api import Page
from playwright.sync_api import expect
from playwright.sync_api import sync_playwright

from tests.helpers import (
    REPO_ROOT,
    fetch_all,
    find_free_port,
    spawn_hikbox,
    terminate_process,
    wait_for_http_ready,
)


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
        port1 = find_free_port()
        process1 = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port1))
        base_url1 = f"http://127.0.0.1:{port1}"
        try:
            wait_for_http_ready(f"{base_url1}/")
            # 命名人物
            httpx.post(f"{base_url1}/people/{alex_id}/name", data={"display_name": "Alex Chen"}, follow_redirects=False, timeout=5.0)
            httpx.post(f"{base_url1}/people/{blair_id}/name", data={"display_name": "Blair Lin"}, follow_redirects=False, timeout=5.0)
            # 做一次合并以便有可撤销的 merge
            resp = httpx.post(f"{base_url1}/people/merge", data={"person_id": [alex_id, casey_id]}, follow_redirects=False, timeout=5.0)
            assert resp.status_code == 303, f"merge should succeed: {resp.status_code}"
        finally:
            terminate_process(process1)

        # 用 blocking hook 启动 serve
        port = find_free_port()
        process = spawn_hikbox(
            "serve",
            "--workspace", str(workspace),
            "--port", str(port),
            pythonpath_prepend=[hook_module_dir],
        )
        base_url = f"http://127.0.0.1:{port}"
        try:
            wait_for_http_ready(f"{base_url}/")

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
                running_count = fetch_all(library_db, "SELECT COUNT(*) FROM export_run WHERE status = 'running'")[0][0]
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
            terminate_process(process)
