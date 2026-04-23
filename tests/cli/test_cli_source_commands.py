from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from tests.cli.conftest import 读取_json输出


def test_config_show_返回当前配置并与_config真值一致(
    已初始化工作区: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
) -> None:
    result = 运行_cli(["--json", "config", "show", "--workspace", str(已初始化工作区)])
    payload = 读取_json输出(result.stdout)
    db_config = json.loads((已初始化工作区 / ".hikbox" / "config.json").read_text(encoding="utf-8"))

    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"] == db_config


def test_config_set_external_root_更新配置并与_config真值一致(
    已初始化工作区: Path,
    tmp_path: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
) -> None:
    external_root = (tmp_path / "external-reset").resolve()

    result = 运行_cli(
        [
            "--json",
            "config",
            "set-external-root",
            str(external_root),
            "--workspace",
            str(已初始化工作区),
        ]
    )
    payload = 读取_json输出(result.stdout)
    db_config = json.loads((已初始化工作区 / ".hikbox" / "config.json").read_text(encoding="utf-8"))

    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"]["external_root"] == str(external_root)
    assert db_config["external_root"] == str(external_root)


def test_source_add_写入_source并与_db真值一致(
    已初始化工作区: Path,
    tmp_path: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    查询行: Callable[[Path, str, Sequence[object]], tuple[object, ...] | None],
) -> None:
    source_root = (tmp_path / "family").resolve()
    source_root.mkdir(parents=True, exist_ok=True)

    result = 运行_cli(
        [
            "--json",
            "source",
            "add",
            str(source_root),
            "--label",
            "family",
            "--workspace",
            str(已初始化工作区),
        ]
    )
    payload = 读取_json输出(result.stdout)
    source_id = int(payload["data"]["source_id"])
    db_row = 查询行(
        已初始化工作区,
        "SELECT id, root_path, label, enabled FROM library_source WHERE id=?",
        (source_id,),
    )

    assert db_row is not None
    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"] == {
        "source_id": db_row[0],
        "root_path": db_row[1],
        "label": db_row[2],
        "enabled": bool(db_row[3]),
    }


def test_source_disable_enable_relabel_remove_逐步修改并与_db真值一致(
    已初始化工作区: Path,
    tmp_path: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    查询行: Callable[[Path, str, Sequence[object]], tuple[object, ...] | None],
) -> None:
    source_root = (tmp_path / "family-edit").resolve()
    source_root.mkdir(parents=True, exist_ok=True)
    add_result = 运行_cli(
        [
            "--json",
            "source",
            "add",
            str(source_root),
            "--label",
            "family-edit",
            "--workspace",
            str(已初始化工作区),
        ]
    )
    source_id = int(读取_json输出(add_result.stdout)["data"]["source_id"])

    disable_result = 运行_cli(["--json", "source", "disable", str(source_id), "--workspace", str(已初始化工作区)])
    disable_payload = 读取_json输出(disable_result.stdout)
    disable_row = 查询行(已初始化工作区, "SELECT enabled FROM library_source WHERE id=?", (source_id,))

    enable_result = 运行_cli(["--json", "source", "enable", str(source_id), "--workspace", str(已初始化工作区)])
    enable_payload = 读取_json输出(enable_result.stdout)
    enable_row = 查询行(已初始化工作区, "SELECT enabled FROM library_source WHERE id=?", (source_id,))

    relabel_result = 运行_cli(
        [
            "--json",
            "source",
            "relabel",
            str(source_id),
            "family-2026",
            "--workspace",
            str(已初始化工作区),
        ]
    )
    relabel_payload = 读取_json输出(relabel_result.stdout)
    relabel_row = 查询行(已初始化工作区, "SELECT label FROM library_source WHERE id=?", (source_id,))

    remove_result = 运行_cli(["--json", "source", "remove", str(source_id), "--workspace", str(已初始化工作区)])
    remove_payload = 读取_json输出(remove_result.stdout)
    remove_row = 查询行(
        已初始化工作区,
        "SELECT enabled, removed_at IS NOT NULL FROM library_source WHERE id=?",
        (source_id,),
    )

    assert disable_row is not None and enable_row is not None and relabel_row is not None and remove_row is not None
    assert disable_result.returncode == 0
    assert disable_payload["data"]["enabled"] is False
    assert disable_row == (0,)

    assert enable_result.returncode == 0
    assert enable_payload["data"]["enabled"] is True
    assert enable_row == (1,)

    assert relabel_result.returncode == 0
    assert relabel_payload["data"]["label"] == "family-2026"
    assert relabel_row == ("family-2026",)

    assert remove_result.returncode == 0
    assert remove_payload["data"]["enabled"] is False
    assert remove_row == (0, 1)
