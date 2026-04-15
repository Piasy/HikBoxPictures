from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

_TOOL_PATH = Path(__file__).resolve().parents[2] / "tools" / "review_queue_playwright_check.py"
_DATASET_ROOT = Path(__file__).resolve().parents[1] / "data" / "e2e-face-input"
_TOOL_SPEC = spec_from_file_location("people_gallery_review_queue_playwright_check", _TOOL_PATH)
if _TOOL_SPEC is None or _TOOL_SPEC.loader is None:
    raise RuntimeError(f"无法加载 reviews Playwright 工具: {_TOOL_PATH}")
_TOOL_MODULE = module_from_spec(_TOOL_SPEC)
sys.modules[_TOOL_SPEC.name] = _TOOL_MODULE
_TOOL_SPEC.loader.exec_module(_TOOL_MODULE)


def test_review_queue_runtime_lib_bootstrap_is_linux_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_TOOL_MODULE.sys, "platform", "darwin")

    def _unexpected_run_command(*args, **kwargs):
        raise AssertionError("非 Linux 平台不应尝试下载运行库")

    monkeypatch.setattr(_TOOL_MODULE, "_run_command", _unexpected_run_command)

    assert _TOOL_MODULE._ensure_local_runtime_libs(runner_dir=tmp_path) == {}


def test_review_queue_font_bootstrap_is_linux_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_TOOL_MODULE.sys, "platform", "darwin")
    monkeypatch.setattr(_TOOL_MODULE, "_system_has_cjk_font", lambda: False)

    def _unexpected_local_font(*, runner_dir: Path):
        raise AssertionError("非 Linux 平台不应尝试下载 Linux 字体包")

    monkeypatch.setattr(_TOOL_MODULE, "_ensure_local_cjk_font", _unexpected_local_font)

    assert _TOOL_MODULE._ensure_font_env(runner_dir=tmp_path) == {}


def test_review_queue_prepare_seeded_workspace_uses_e2e_face_input(tmp_path: Path) -> None:
    workspace = tmp_path / "seeded"

    meta = _TOOL_MODULE._prepare_seeded_workspace(workspace)

    assert workspace.joinpath(".hikbox", "config.json").is_file()
    assert meta["dataset_dir"] == _DATASET_ROOT.resolve()
    assert meta["review_id"] > 0
    assert len(meta["asset_ids"]) == 3


def test_review_queue_visual_check_smoke(tmp_path: Path) -> None:
    if os.environ.get("RUN_PLAYWRIGHT_VISUAL") != "1":
        pytest.skip("未启用 RUN_PLAYWRIGHT_VISUAL=1，跳过视觉检查 smoke。")
    if shutil.which("node") is None:
        pytest.skip("缺少 node，跳过视觉检查 smoke。")
    if shutil.which("npm") is None:
        pytest.skip("缺少 npm，跳过视觉检查 smoke。")

    repo_root = Path(__file__).resolve().parents[2]
    output_dir = tmp_path / "review-visual"
    command = [
        sys.executable,
        str(repo_root / "tools" / "review_queue_playwright_check.py"),
        "--output-dir",
        str(output_dir),
    ]
    result = subprocess.run(
        command,
        cwd=str(repo_root),
        env={**os.environ, "PYTHONPATH": "src"},
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(
            "review_queue_playwright_check 执行失败\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

    summary_path = output_dir / "review-queue-visual-summary.json"
    assert summary_path.exists()
    assert (output_dir / "captures" / "seeded" / "reviews-desktop.png").exists()
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    result_entry = next(item for item in summary["results"] if item["mode"] == "seeded")
    assert Path(summary["seeded_meta"]["dataset_dir"]) == _DATASET_ROOT.resolve()
    assert result_entry["desktop"]["metrics"]["queue_open_blocks"] == 0
    assert result_entry["desktop"]["expanded_metrics"]["first_queue_open"] is True
    sticky_metrics = result_entry["desktop"]["sticky_metrics"]
    assert isinstance(sticky_metrics["item_ids"], list)
    assert sticky_metrics["item_count"] == len(sticky_metrics["item_ids"])
