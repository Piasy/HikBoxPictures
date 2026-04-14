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
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="用 Playwright 检查 /exports 页面布局与基础交互。")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="指定已有工作区；默认优先使用 repo 内的 sample/workspace。",
    )
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
        "--runner-dir",
        type=Path,
        default=None,
        help="Node Playwright runner 目录；默认写入 output-dir/node-runner。",
    )
    parser.add_argument(
        "--install-browser",
        action="store_true",
        help="运行前执行一次 playwright chromium 安装。",
    )
    return parser


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run_command(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
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


def _read_command_stdout(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    return _run_command(command, cwd=cwd, env=env)


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


def _ensure_local_no_proxy(*, host: str) -> dict[str, str]:
    hosts = [host, "127.0.0.1", "localhost"]
    merged_hosts = ",".join(dict.fromkeys(item for item in hosts if item))
    no_proxy = [os.environ.get("NO_PROXY", "").strip(), merged_hosts]
    no_proxy_lower = [os.environ.get("no_proxy", "").strip(), merged_hosts]
    return {
        "NO_PROXY": ",".join(part for part in no_proxy if part),
        "no_proxy": ",".join(part for part in no_proxy_lower if part),
    }


def _wait_for_health(host: str, port: int, *, timeout_seconds: float = 20.0) -> None:
    deadline = time.time() + timeout_seconds
    url = f"http://{host}:{port}/api/health"
    opener = build_opener(ProxyHandler({}))
    while time.time() < deadline:
        try:
            with opener.open(url, timeout=1.0) as response:
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
    env.update(_ensure_local_no_proxy(host=host))
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


def _resolve_default_workspace() -> Path | None:
    sample_workspace = REPO_ROOT / "sample" / "workspace"
    if sample_workspace.exists() and sample_workspace.is_dir():
        return sample_workspace
    return None


def _capture_once(
    *,
    node_modules_dir: Path,
    browser_env: dict[str, str],
    host: str,
    url: str,
    screenshot_path: Path,
) -> dict[str, Any]:
    report_path = screenshot_path.with_suffix(".json")
    env = os.environ.copy()
    env["NODE_PATH"] = str(node_modules_dir)
    env.update(browser_env)
    env.update(_ensure_local_no_proxy(host=host))
    _run_command(
        [
            "node",
            str(REPO_ROOT / "tools" / "export_page_playwright_capture.cjs"),
            "--url",
            url,
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

    requested_workspace = args.workspace.resolve() if args.workspace else _resolve_default_workspace()
    if requested_workspace is None:
        raise RuntimeError("未提供 --workspace，且 sample/workspace 不存在")

    if args.output_dir is None:
        output_dir = Path(tempfile.mkdtemp(prefix="hikbox-export-visual-"))
    else:
        output_dir = args.output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
    runner_dir = args.runner_dir.resolve() if args.runner_dir else (output_dir / "node-runner")

    node_modules_dir = _ensure_node_playwright_runner(
        runner_dir=runner_dir,
        install_browser=bool(args.install_browser),
    )
    browser_env: dict[str, str] = {}
    browser_env.update(_ensure_local_runtime_libs(runner_dir=runner_dir))
    if not _system_has_cjk_font():
        browser_env.update(_ensure_local_cjk_font(runner_dir=runner_dir))

    port = _pick_free_port()
    host = str(args.host)
    base_url = f"http://{host}:{port}"
    exports_url = f"{base_url}/exports"
    output_dir.mkdir(parents=True, exist_ok=True)

    with _run_server(
        workspace=requested_workspace,
        host=host,
        port=port,
        log_path=output_dir / "server.log",
    ):
        desktop = _capture_once(
            node_modules_dir=node_modules_dir,
            browser_env=browser_env,
            host=host,
            url=exports_url,
            screenshot_path=output_dir / "exports-desktop.png",
        )

    summary = {
        "output_dir": output_dir,
        "runner_dir": runner_dir,
        "workspace": requested_workspace,
        "base_url": base_url,
        "font_env": browser_env,
        "desktop": desktop,
    }
    summary_path = output_dir / "export-page-visual-summary.json"
    summary_path.write_text(
        json.dumps(_jsonable(summary), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[export-visual] 输出目录: {output_dir}")
    print(f"[export-visual] 汇总报告: {summary_path}")
    print(f"[export-visual] 桌面截图: {desktop['screenshot']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
