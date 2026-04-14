from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_seed_workspace = _MODULE.build_seed_workspace

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOCALHOST = "127.0.0.1"
_PLAYWRIGHT_FONT_ROOT = _PROJECT_ROOT / ".cache" / "playwright-fonts"
_PLAYWRIGHT_FONT_CONF = _PLAYWRIGHT_FONT_ROOT / "fontconfig" / "fonts.conf"


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((_LOCALHOST, 0))
        return int(sock.getsockname()[1])


def _wait_server_ready(health_url: str, proc: subprocess.Popen[str], timeout_seconds: float = 12.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            output = ""
            if proc.stdout is not None:
                output = proc.stdout.read()
            raise RuntimeError(f"Web 服务提前退出，exit={proc.returncode}，输出:\n{output}")
        try:
            with urlopen(health_url, timeout=0.5) as response:
                if response.status == 200:
                    return
        except URLError:
            pass
        time.sleep(0.2)
    raise TimeoutError(f"等待 Web 服务超时: {health_url}")


@contextmanager
def _serve_workspace(workspace: Path):
    port = _pick_free_port()
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"src{os.pathsep}{existing_pythonpath}" if existing_pythonpath else "src"
    cmd = [
        sys.executable,
        "-m",
        "hikbox_pictures.cli",
        "serve",
        "--workspace",
        str(workspace),
        "--host",
        _LOCALHOST,
        "--port",
        str(port),
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(_PROJECT_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://{_LOCALHOST}:{port}"
    try:
        _wait_server_ready(f"{base_url}/api/health", proc)
        yield base_url
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)


def _open_page_and_screenshot(page, *, base_url: str, screenshot_path: Path) -> None:
    page.set_viewport_size({"width": 1440, "height": 1200})
    page.goto(f"{base_url}/", wait_until="networkidle")
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(screenshot_path), full_page=True)
    assert screenshot_path.exists()
    assert screenshot_path.stat().st_size > 0


def _launch_browser_or_skip(playwright):
    env = os.environ.copy()
    if _PLAYWRIGHT_FONT_CONF.exists():
        env["FONTCONFIG_FILE"] = str(_PLAYWRIGHT_FONT_CONF)
    try:
        return playwright.chromium.launch(headless=True, env=env)
    except Exception as exc:  # pragma: no cover - 环境相关跳过分支
        pytest.skip(f"无法启动 Chromium（请先执行 playwright install chromium）: {exc}")


def _ensure_playwright_zh_font_ready_or_skip() -> None:
    setup_script = _PROJECT_ROOT / "scripts" / "setup_playwright_zh_fonts.sh"
    if not setup_script.exists():
        pytest.skip("缺少 scripts/setup_playwright_zh_fonts.sh，无法准备中文字体")

    if not _PLAYWRIGHT_FONT_CONF.exists():
        try:
            subprocess.run(
                [str(setup_script)],
                cwd=str(_PROJECT_ROOT),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except Exception as exc:  # pragma: no cover - 环境相关跳过分支
            pytest.skip(f"准备 Playwright 中文字体失败: {exc}")

    try:
        check = subprocess.run(
            ["fc-match", "sans:lang=zh-cn"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "FONTCONFIG_FILE": str(_PLAYWRIGHT_FONT_CONF)},
        )
    except Exception as exc:  # pragma: no cover - 环境相关跳过分支
        pytest.skip(f"无法验证中文字体映射: {exc}")
    result = check.stdout.lower()
    if "noto" not in result and "cjk" not in result:
        pytest.skip(f"中文字体映射未生效: {check.stdout.strip()}")


def test_people_home_visual_empty_workspace(tmp_path: Path) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    _ensure_playwright_zh_font_ready_or_skip()
    with _serve_workspace(tmp_path) as base_url:
        with sync_api.sync_playwright() as playwright:
            browser = _launch_browser_or_skip(playwright)
            try:
                page = browser.new_page()
                _open_page_and_screenshot(
                    page,
                    base_url=base_url,
                    screenshot_path=tmp_path / "artifacts" / "people-home-empty.png",
                )
                expect = sync_api.expect
                expect(page.locator("h2")).to_have_text("人物库")
                expect(page.locator(".person-empty-state")).to_be_visible()
                expect(page.locator(".person-card")).to_have_count(0)
                expect(page.locator(".media-viewer")).to_have_count(0)
            finally:
                browser.close()


def test_people_home_visual_seeded_workspace(tmp_path: Path) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    _ensure_playwright_zh_font_ready_or_skip()
    ws = build_seed_workspace(tmp_path)
    try:
        with _serve_workspace(ws.root) as base_url:
            with sync_api.sync_playwright() as playwright:
                browser = _launch_browser_or_skip(playwright)
                try:
                    page = browser.new_page()
                    _open_page_and_screenshot(
                        page,
                        base_url=base_url,
                        screenshot_path=tmp_path / "artifacts" / "people-home-seeded.png",
                    )
                    expect = sync_api.expect
                    expect(page.locator(".person-empty-state")).to_have_count(0)
                    expect(page.locator(".person-card").first).to_be_visible()
                    expect(page.locator(".media-viewer")).to_have_count(0)

                    cover = page.locator(".person-card .person-card-cover").first
                    box = cover.bounding_box()
                    assert box is not None
                    ratio = float(box["height"]) / float(box["width"])
                    assert 1.15 <= ratio <= 1.35
                finally:
                    browser.close()
    finally:
        ws.close()
