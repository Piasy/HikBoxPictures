from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from hikbox_pictures.cli import main as cli_main


_FIXTURE_PATH = REPO_ROOT / "tests" / "people_gallery" / "fixtures_workspace.py"
_FIXTURE_SPEC = spec_from_file_location("people_gallery_fixtures_workspace_visual", _FIXTURE_PATH)
if _FIXTURE_SPEC is None or _FIXTURE_SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_FIXTURE_MODULE = module_from_spec(_FIXTURE_SPEC)
sys.modules[_FIXTURE_SPEC.name] = _FIXTURE_MODULE
_FIXTURE_SPEC.loader.exec_module(_FIXTURE_MODULE)
build_seed_workspace_with_mock_embeddings = _FIXTURE_MODULE.build_seed_workspace_with_mock_embeddings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="用 Playwright 检查人物库首页视觉效果。")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="输出目录；默认写入系统临时目录。",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="本地服务监听地址。",
    )
    parser.add_argument(
        "--install-browser",
        action="store_true",
        help="运行前执行一次 Chromium 安装。",
    )
    parser.add_argument(
        "--runner-dir",
        type=Path,
        default=None,
        help="Node Playwright runner 目录；默认写入输出目录下的 node-runner。",
    )
    return parser


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _wait_for_health(host: str, port: int, *, timeout_seconds: float = 20.0) -> None:
    deadline = time.time() + timeout_seconds
    url = f"http://{host}:{port}/api/health"
    while time.time() < deadline:
        try:
            with urlopen(url, timeout=1.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("ok") is True:
                return
        except (OSError, URLError, json.JSONDecodeError):
            time.sleep(0.2)
    raise TimeoutError(f"服务未能在 {timeout_seconds:.1f}s 内启动: {url}")


@contextmanager
def _run_server(*, workspace: Path, host: str, port: int, log_path: Path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC_ROOT)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "hikbox_pictures.cli",
                "serve",
                "--workspace",
                str(workspace),
                "--host",
                host,
                "--port",
                str(port),
            ],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_for_health(host, port)
            yield process
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _prepare_empty_workspace(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    result = cli_main(["init", "--workspace", str(path)])
    if result != 0:
        raise RuntimeError(f"初始化空工作区失败: {path}")


def _prepare_seeded_workspace(path: Path) -> dict[str, Any]:
    path.mkdir(parents=True, exist_ok=True)
    return build_seed_workspace_with_mock_embeddings(path)


def _run_command(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    result = subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"命令执行失败: {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


def _read_command_stdout(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"命令执行失败: {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result.stdout


def _system_has_cjk_font() -> bool:
    try:
        output = _read_command_stdout(["fc-list", ":lang=zh-cn", "family"], cwd=REPO_ROOT)
    except Exception:
        return False
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return False
    return any("DejaVu" not in line for line in lines)


def _ensure_local_runtime_libs(*, runner_dir: Path) -> dict[str, str]:
    libs_root = runner_dir / "local-libs"
    package_names = ("libgbm1", "libwayland-server0")
    extracted_dirs: list[Path] = []

    for package_name in package_names:
        package_root = libs_root / package_name
        lib_dir = package_root / "usr" / "lib" / "x86_64-linux-gnu"
        if not lib_dir.exists():
            libs_root.mkdir(parents=True, exist_ok=True)
            _run_command(["apt-get", "download", package_name], cwd=libs_root)
            debs = sorted(libs_root.glob(f"{package_name}_*.deb"))
            if not debs:
                raise RuntimeError(f"未能下载运行库包: {package_name}")
            package_root.mkdir(parents=True, exist_ok=True)
            _run_command(["dpkg-deb", "-x", str(debs[-1]), str(package_root)], cwd=libs_root)
        extracted_dirs.append(lib_dir)

    existing = os.environ.get("LD_LIBRARY_PATH", "").strip()
    paths = [str(path) for path in extracted_dirs]
    if existing:
        paths.append(existing)
    return {"LD_LIBRARY_PATH": ":".join(paths)}


def _ensure_local_cjk_font(*, runner_dir: Path) -> dict[str, str]:
    fonts_root = runner_dir / "local-fonts"
    package_root = fonts_root / "fonts-wqy-microhei"
    font_path = package_root / "usr" / "share" / "fonts" / "truetype" / "wqy" / "wqy-microhei.ttc"
    font_conf_dir = fonts_root / "fontconfig"
    font_conf_file = font_conf_dir / "fonts.conf"
    font_cache_dir = fonts_root / "cache"

    if not font_path.exists():
        fonts_root.mkdir(parents=True, exist_ok=True)
        _run_command(["apt-get", "download", "fonts-wqy-microhei"], cwd=fonts_root)
        debs = sorted(fonts_root.glob("fonts-wqy-microhei_*_all.deb"))
        if not debs:
            raise RuntimeError("未能下载 fonts-wqy-microhei 字体包")
        package_root.mkdir(parents=True, exist_ok=True)
        _run_command(["dpkg-deb", "-x", str(debs[-1]), str(package_root)], cwd=fonts_root)

    font_conf_dir.mkdir(parents=True, exist_ok=True)
    font_cache_dir.mkdir(parents=True, exist_ok=True)
    font_conf_file.write_text(
        f"""<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <include ignore_missing="yes">/etc/fonts/fonts.conf</include>
  <dir>{font_path.parent}</dir>

  <match target="pattern">
    <test qual="any" name="family">
      <string>Noto Sans SC</string>
    </test>
    <edit name="family" mode="assign_replace">
      <string>WenQuanYi Micro Hei</string>
    </edit>
  </match>

  <match target="pattern">
    <test qual="any" name="family">
      <string>Noto Sans CJK SC</string>
    </test>
    <edit name="family" mode="assign_replace">
      <string>WenQuanYi Micro Hei</string>
    </edit>
  </match>

  <match target="pattern">
    <test qual="any" name="family">
      <string>PingFang SC</string>
    </test>
    <edit name="family" mode="assign_replace">
      <string>WenQuanYi Micro Hei</string>
    </edit>
  </match>

  <alias>
    <family>sans-serif</family>
    <prefer>
      <family>WenQuanYi Micro Hei</family>
    </prefer>
  </alias>
</fontconfig>
""",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["FONTCONFIG_FILE"] = str(font_conf_file)
    env["XDG_CACHE_HOME"] = str(font_cache_dir)
    _run_command(["fc-cache", "-f"], cwd=REPO_ROOT, env=env)
    return {
        "FONTCONFIG_FILE": str(font_conf_file),
    }


def _ensure_node_playwright_runner(*, runner_dir: Path, install_browser: bool) -> Path:
    runner_dir.mkdir(parents=True, exist_ok=True)
    package_json = runner_dir / "package.json"
    if not package_json.exists():
        _run_command(["npm", "init", "-y"], cwd=runner_dir)

    node_modules = runner_dir / "node_modules"
    if not (node_modules / "playwright").exists():
        _run_command(["npm", "install", "playwright"], cwd=runner_dir)

    if install_browser:
        _run_command(
            ["node", str(node_modules / "playwright" / "cli.js"), "install", "chromium"],
            cwd=runner_dir,
        )
    return node_modules


def _capture_scenario(
    *,
    base_url: str,
    screenshot_path: Path,
    mode: str,
    node_modules_dir: Path,
    viewport: str,
    browser_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    report_path = screenshot_path.with_suffix(".json")
    env = os.environ.copy()
    env["NODE_PATH"] = str(node_modules_dir)
    if browser_env:
        env.update(browser_env)
    _run_command(
        [
            "node",
            str(REPO_ROOT / "tools" / "people_home_playwright_capture.cjs"),
            "--url",
            base_url,
            "--mode",
            mode,
            "--viewport",
            viewport,
            "--screenshot",
            str(screenshot_path),
            "--report",
            str(report_path),
        ],
        cwd=REPO_ROOT,
        env=env,
    )
    return json.loads(report_path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.output_dir is None:
        output_dir = Path(tempfile.mkdtemp(prefix="hikbox-people-home-playwright-"))
    else:
        output_dir = args.output_dir.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

    runner_dir = (
        args.runner_dir.expanduser().resolve()
        if args.runner_dir is not None
        else output_dir / "node-runner"
    )
    node_modules_dir = _ensure_node_playwright_runner(
        runner_dir=runner_dir,
        install_browser=bool(args.install_browser),
    )
    browser_env: dict[str, str] = {}
    browser_env.update(_ensure_local_runtime_libs(runner_dir=runner_dir))
    if not _system_has_cjk_font():
        browser_env.update(_ensure_local_cjk_font(runner_dir=runner_dir))

    empty_workspace = output_dir / "workspaces" / "empty"
    seeded_workspace = output_dir / "workspaces" / "seeded"
    _prepare_empty_workspace(empty_workspace)
    seeded_meta = _prepare_seeded_workspace(seeded_workspace)

    host = str(args.host)
    empty_port = _pick_free_port()
    seeded_port = _pick_free_port()

    report: dict[str, Any] = {
        "output_dir": str(output_dir),
        "empty_workspace": str(empty_workspace),
        "seeded_workspace": str(seeded_workspace),
        "seeded_meta": _jsonable(seeded_meta),
    }
    viewports = ("desktop", "mobile")

    with _run_server(
        workspace=empty_workspace,
        host=host,
        port=empty_port,
        log_path=output_dir / "empty-server.log",
    ):
        report["empty"] = {}
        for viewport in viewports:
            report["empty"][viewport] = _capture_scenario(
                base_url=f"http://{host}:{empty_port}/",
                screenshot_path=output_dir / f"people-home-empty-{viewport}.png",
                mode="empty",
                node_modules_dir=node_modules_dir,
                viewport=viewport,
                browser_env=browser_env,
            )

    with _run_server(
        workspace=seeded_workspace,
        host=host,
        port=seeded_port,
        log_path=output_dir / "seeded-server.log",
    ):
        report["seeded"] = {}
        for viewport in viewports:
            report["seeded"][viewport] = _capture_scenario(
                base_url=f"http://{host}:{seeded_port}/",
                screenshot_path=output_dir / f"people-home-seeded-{viewport}.png",
                mode="seeded",
                node_modules_dir=node_modules_dir,
                viewport=viewport,
                browser_env=browser_env,
            )

    report_path = output_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Playwright 视觉检查完成: {report_path}")
    print(f"空库桌面截图: {output_dir / 'people-home-empty-desktop.png'}")
    print(f"空库手机截图: {output_dir / 'people-home-empty-mobile.png'}")
    print(f"种子桌面截图: {output_dir / 'people-home-seeded-desktop.png'}")
    print(f"种子手机截图: {output_dir / 'people-home-seeded-mobile.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
