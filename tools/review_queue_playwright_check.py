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
from importlib.metadata import PackageNotFoundError, version as package_version
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener


REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
DEFAULT_DATASET_ROOT = REPO_ROOT / "tests" / "data" / "e2e-face-input"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def _load_fixture_module():
    fixture_path = REPO_ROOT / "tests" / "people_gallery" / "fixtures_workspace.py"
    fixture_spec = spec_from_file_location("people_gallery_fixtures_workspace_review_visual", fixture_path)
    if fixture_spec is None or fixture_spec.loader is None:
        raise RuntimeError(f"无法加载测试夹具文件: {fixture_path}")
    fixture_module = module_from_spec(fixture_spec)
    sys.modules[fixture_spec.name] = fixture_module
    fixture_spec.loader.exec_module(fixture_module)
    return fixture_module


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="用 Playwright 检查 /reviews 页面视觉与基础交互质量。")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="指定已有工作区；不传时会基于 tests/data/e2e-face-input 构建临时 workspace。",
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


def _is_linux_platform() -> bool:
    return sys.platform.startswith("linux")


def _ensure_local_runtime_libs(*, runner_dir: Path) -> dict[str, str]:
    if not _is_linux_platform():
        return {}

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


def _desired_node_playwright_spec() -> str:
    try:
        return f"playwright@{package_version('playwright')}"
    except PackageNotFoundError:
        return "playwright"


def _read_installed_node_playwright_version(node_modules: Path) -> str | None:
    package_json = node_modules / "playwright" / "package.json"
    if not package_json.exists():
        return None
    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    version = str(payload.get("version", "")).strip()
    return version or None


def _ensure_node_playwright_runner(*, runner_dir: Path, install_browser: bool) -> Path:
    runner_dir.mkdir(parents=True, exist_ok=True)
    package_json = runner_dir / "package.json"
    if not package_json.exists():
        _run_command(["npm", "init", "-y"], cwd=runner_dir)

    node_modules = runner_dir / "node_modules"
    desired_spec = _desired_node_playwright_spec()
    desired_version = desired_spec.partition("@")[2] or None
    installed_version = _read_installed_node_playwright_version(node_modules)
    if installed_version != desired_version:
        _run_command(["npm", "install", desired_spec], cwd=runner_dir)

    if install_browser:
        _run_command(
            ["node", str(node_modules / "playwright" / "cli.js"), "install", "chromium"],
            cwd=runner_dir,
        )
    return node_modules


def _ensure_font_env(*, runner_dir: Path) -> dict[str, str]:
    if not _is_linux_platform():
        return {}
    if _system_has_cjk_font():
        return {}
    return _ensure_local_cjk_font(runner_dir=runner_dir)


def _resolve_default_dataset_root() -> Path:
    dataset_root = DEFAULT_DATASET_ROOT.resolve()
    if not dataset_root.is_dir():
        raise FileNotFoundError(f"缺少 reviews Playwright 默认数据集: {dataset_root}")
    return dataset_root


def _prepare_seeded_workspace(path: Path) -> dict[str, Any]:
    fixture_module = _load_fixture_module()
    inject_mock_embeddings_for_assets = fixture_module.inject_mock_embeddings_for_assets
    path.mkdir(parents=True, exist_ok=True)
    dataset_dir = _resolve_default_dataset_root()
    result = inject_mock_embeddings_for_assets(
        path,
        dataset_dir=dataset_dir,
        person_specs=[
            {
                "file_name": "raw/person_a_001.jpg",
                "display_name": "人物A",
                "vector": [0.11, 0.12, 0.13, 0.14],
                "locked": True,
            },
            {
                "file_name": "raw/person_b_001.jpg",
                "display_name": "人物B",
                "vector": [0.21, 0.22, 0.23, 0.24],
                "locked": True,
            },
            {
                "file_name": "raw/person_c_001.jpg",
                "display_name": "人物C",
                "vector": [0.31, 0.32, 0.33, 0.34],
                "locked": True,
            },
        ],
        template_name="review-visual-template",
    )
    result["dataset_dir"] = dataset_dir
    return result


def _capture_once(
    *,
    node_modules_dir: Path,
    browser_env: dict[str, str],
    host: str,
    url: str,
    mode: str,
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
            str(REPO_ROOT / "tools" / "review_queue_playwright_capture.cjs"),
            "--url",
            url,
            "--mode",
            mode,
            "--screenshot",
            str(screenshot_path),
            "--report",
            str(report_path),
        ],
        cwd=REPO_ROOT,
        env=env,
    )
    return json.loads(report_path.read_text(encoding="utf-8"))


def _run_scenario(
    *,
    mode: str,
    host: str,
    node_modules_dir: Path,
    browser_env: dict[str, str],
    workspace: Path,
    scenario_dir: Path,
) -> dict[str, Any]:
    port = _pick_free_port()
    log_path = scenario_dir / "server.log"
    scenario_dir.mkdir(parents=True, exist_ok=True)
    base_url = f"http://{host}:{port}"
    url = f"{base_url}/reviews"

    with _run_server(workspace=workspace, host=host, port=port, log_path=log_path):
        desktop_report = _capture_once(
            node_modules_dir=node_modules_dir,
            browser_env=browser_env,
            host=host,
            url=url,
            mode=mode,
            screenshot_path=scenario_dir / "reviews-desktop.png",
        )

    return {
        "mode": mode,
        "workspace": workspace,
        "base_url": base_url,
        "server_log": log_path,
        "desktop": desktop_report,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.output_dir is None:
        output_dir = Path(tempfile.mkdtemp(prefix="hikbox-review-visual-"))
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
    browser_env.update(_ensure_font_env(runner_dir=runner_dir))

    seeded_meta: dict[str, Any] | None = None
    if args.workspace is not None:
        requested_workspace = args.workspace.resolve()
        capture_name = "workspace"
    else:
        workspace_root = output_dir / "workspaces"
        seeded_workspace = workspace_root / "seeded"
        seeded_meta = _prepare_seeded_workspace(seeded_workspace)
        requested_workspace = seeded_workspace
        capture_name = "seeded"

    workspace_result = _run_scenario(
        mode="seeded",
        host=args.host,
        node_modules_dir=node_modules_dir,
        browser_env=browser_env,
        workspace=requested_workspace,
        scenario_dir=output_dir / "captures" / capture_name,
    )
    results = [workspace_result]
    summary = {
        "output_dir": output_dir,
        "runner_dir": runner_dir,
        "font_env": browser_env,
        "workspace": requested_workspace,
        "results": results,
    }
    if seeded_meta is not None:
        summary["seeded_meta"] = seeded_meta

    summary_path = output_dir / "review-queue-visual-summary.json"
    summary_path.write_text(
        json.dumps(_jsonable(summary), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[review-visual] 输出目录: {output_dir}")
    print(f"[review-visual] 汇总报告: {summary_path}")
    if args.workspace is not None:
        print(f"[review-visual] workspace 截图: {workspace_result['desktop']['screenshot']}")
    else:
        print(f"[review-visual] seed 截图: {workspace_result['desktop']['screenshot']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
