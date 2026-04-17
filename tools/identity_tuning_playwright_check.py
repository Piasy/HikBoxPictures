from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
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
    parser = argparse.ArgumentParser(description="用 Playwright 检查 identity tuning 页面视觉与关键节点。")
    parser.add_argument(
        "--workspace",
        type=Path,
        required=True,
        help="工作区路径（必填）。",
    )
    parser.add_argument(
        "--run-id",
        type=int,
        default=None,
        help="可选 run_id，不传则走页面默认 review target run。",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="输出目录（必填）。",
    )
    parser.add_argument(
        "--runner-dir",
        type=Path,
        default=None,
        help="Node Playwright runner 目录；默认 output-dir/node-runner。",
    )
    parser.add_argument(
        "--install-browser",
        action="store_true",
        help="运行前执行一次 playwright webkit 安装。",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="本地服务监听地址。",
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


def _is_linux_platform() -> bool:
    return sys.platform.startswith("linux")


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
            ["node", str(node_modules / "playwright" / "cli.js"), "install", "webkit"],
            cwd=runner_dir,
        )
        _run_command(
            ["node", str(node_modules / "playwright" / "cli.js"), "install", "chromium"],
            cwd=runner_dir,
        )
    return node_modules


def _capture_once(
    *,
    node_modules_dir: Path,
    host: str,
    url: str,
    run_id: int | None,
    screenshot_path: Path,
    browser_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    report_path = screenshot_path.with_suffix(".json")
    env = os.environ.copy()
    env["NODE_PATH"] = str(node_modules_dir)
    env.update(_ensure_local_no_proxy(host=host))
    if browser_env:
        env.update(browser_env)
    command = [
        "node",
        str(REPO_ROOT / "tools" / "identity_tuning_playwright_capture.cjs"),
        "--url",
        url,
        "--screenshot",
        str(screenshot_path),
        "--report",
        str(report_path),
    ]
    if run_id is not None:
        command.extend(["--run-id", str(run_id)])
    _run_command(command, cwd=REPO_ROOT, env=env)
    return json.loads(report_path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    workspace = args.workspace.expanduser().resolve()
    if not workspace.exists():
        raise FileNotFoundError(f"workspace 不存在: {workspace}")

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
    browser_env = _ensure_local_runtime_libs(runner_dir=runner_dir)

    host = str(args.host)
    port = _pick_free_port()
    base_url = f"http://{host}:{port}"
    requested_url = f"{base_url}/identity-tuning"
    if args.run_id is not None:
        requested_url = f"{requested_url}?run_id={int(args.run_id)}"

    capture_dir = output_dir / "captures"
    capture_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = capture_dir / "identity-tuning-desktop.png"
    server_log_path = output_dir / "identity-tuning-server.log"

    with _run_server(workspace=workspace, host=host, port=port, log_path=server_log_path):
        desktop_report = _capture_once(
            node_modules_dir=node_modules_dir,
            host=host,
            url=requested_url,
            run_id=args.run_id,
            screenshot_path=screenshot_path,
            browser_env=browser_env,
        )

    summary = {
        "workspace": workspace,
        "output_dir": output_dir,
        "runner_dir": runner_dir,
        "server_log": server_log_path,
        "requested_run_id": args.run_id,
        "requested_url": requested_url,
        "final_request_url": desktop_report.get("final_url"),
        "captured_run_id": desktop_report.get("run_id"),
        "desktop": desktop_report,
    }
    summary_path = output_dir / "identity-tuning-visual-summary.json"
    summary_path.write_text(
        json.dumps(_jsonable(summary), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"[identity-tuning-visual] 输出目录: {output_dir}")
    print(f"[identity-tuning-visual] 服务日志: {server_log_path}")
    print(f"[identity-tuning-visual] 截图: {screenshot_path}")
    print(f"[identity-tuning-visual] 汇总报告: {summary_path}")
    print(f"[identity-tuning-visual] 最终请求 URL: {desktop_report.get('final_url')}")
    print(f"[identity-tuning-visual] run_id: {desktop_report.get('run_id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
