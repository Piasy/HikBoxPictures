from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from tests.cli.conftest import 读取_json输出


def test_export_template_help_与解析器中不存在_delete(
    seeded_workspace: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    cli帮助输出: Callable[[Sequence[str]], str],
) -> None:
    help_text = cli帮助输出(["export", "template"])
    parse_result = 运行_cli(["export", "template", "delete", "1", "--workspace", str(seeded_workspace)])

    assert "delete" not in help_text
    assert parse_result.returncode != 0
    assert "invalid choice" in parse_result.stderr
    assert "delete" in parse_result.stderr


def test_export_template_list_返回模板列表并与_db真值一致(
    seeded_workspace: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    查询多行: Callable[[Path, str, Sequence[object]], list[tuple[object, ...]]],
) -> None:
    result = 运行_cli(["--json", "export", "template", "list", "--workspace", str(seeded_workspace)])
    payload = 读取_json输出(result.stdout)
    db_rows = 查询多行(
        seeded_workspace,
        """
        SELECT
          t.id,
          t.name,
          t.output_root,
          t.enabled,
          GROUP_CONCAT(tp.person_id, ',') AS person_ids
        FROM export_template AS t
        LEFT JOIN export_template_person AS tp
          ON tp.template_id=t.id
        GROUP BY t.id
        ORDER BY t.id ASC
        """,
    )

    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"]["items"] == [
        {
            "template_id": row[0],
            "name": row[1],
            "output_root": row[2],
            "enabled": bool(row[3]),
            "person_ids": [] if row[4] is None else [int(item) for item in str(row[4]).split(",") if item],
        }
        for row in db_rows
    ]


def test_export_template_create_写入模板并与_db真值一致(
    seeded_workspace: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    查询行: Callable[[Path, str, Sequence[object]], tuple[object, ...] | None],
    查询多行: Callable[[Path, str, Sequence[object]], list[tuple[object, ...]]],
) -> None:
    output_root = (seeded_workspace / "exports" / "named-only").resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    result = 运行_cli(
        [
            "--json",
            "export",
            "template",
            "create",
            "--name",
            "named-only",
            "--output-root",
            str(output_root),
            "--person-ids",
            "6",
            "--workspace",
            str(seeded_workspace),
        ]
    )
    payload = 读取_json输出(result.stdout)
    template_id = int(payload["data"]["template_id"])
    db_template = 查询行(
        seeded_workspace,
        "SELECT id, name, output_root, enabled FROM export_template WHERE id=?",
        (template_id,),
    )
    db_person_rows = 查询多行(
        seeded_workspace,
        "SELECT person_id FROM export_template_person WHERE template_id=? ORDER BY person_id ASC",
        (template_id,),
    )

    assert db_template is not None
    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"] == {"template_id": template_id}
    assert db_template == (template_id, "named-only", str(output_root), 1)
    assert db_person_rows == [(6,)]


def test_export_template_create_重复模板名返回_validation退出码与重复错误码(
    seeded_workspace: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
) -> None:
    output_root = (seeded_workspace / "exports" / "dup-template").resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    first_result = 运行_cli(
        [
            "--json",
            "export",
            "template",
            "create",
            "--name",
            "dup-template",
            "--output-root",
            str(output_root),
            "--person-ids",
            "6",
            "--workspace",
            str(seeded_workspace),
        ]
    )

    duplicate_result = 运行_cli(
        [
            "--json",
            "export",
            "template",
            "create",
            "--name",
            "dup-template",
            "--output-root",
            str(output_root),
            "--person-ids",
            "6",
            "--workspace",
            str(seeded_workspace),
        ]
    )

    assert first_result.returncode == 0
    assert duplicate_result.returncode == 2
    assert duplicate_result.stdout == ""
    assert 读取_json输出(duplicate_result.stderr)["error"]["code"] == "EXPORT_TEMPLATE_DUPLICATE"


def test_export_template_update_更新模板并与_db真值一致(
    seeded_workspace: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    查询行: Callable[[Path, str, Sequence[object]], tuple[object, ...] | None],
    查询多行: Callable[[Path, str, Sequence[object]], list[tuple[object, ...]]],
) -> None:
    output_root = (seeded_workspace / "exports" / "named-update").resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    create_result = 运行_cli(
        [
            "--json",
            "export",
            "template",
            "create",
            "--name",
            "named-update",
            "--output-root",
            str(output_root),
            "--person-ids",
            "6",
            "--workspace",
            str(seeded_workspace),
        ]
    )
    template_id = int(读取_json输出(create_result.stdout)["data"]["template_id"])

    next_output_root = (seeded_workspace / "exports" / "named-update-v2").resolve()
    next_output_root.mkdir(parents=True, exist_ok=True)
    result = 运行_cli(
        [
            "--json",
            "export",
            "template",
            "update",
            str(template_id),
            "--name",
            "named-update-v2",
            "--output-root",
            str(next_output_root),
            "--enabled",
            "false",
            "--person-ids",
            "1,6",
            "--workspace",
            str(seeded_workspace),
        ]
    )
    payload = 读取_json输出(result.stdout)
    db_template = 查询行(
        seeded_workspace,
        "SELECT id, name, output_root, enabled FROM export_template WHERE id=?",
        (template_id,),
    )
    db_person_rows = 查询多行(
        seeded_workspace,
        "SELECT person_id FROM export_template_person WHERE template_id=? ORDER BY person_id ASC",
        (template_id,),
    )

    assert db_template is not None
    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"] == {"template_id": template_id, "updated": True}
    assert db_template == (template_id, "named-update-v2", str(next_output_root), 0)
    assert db_person_rows == [(1,), (6,)]


def test_export_run_触发导出运行并与_db真值一致(
    seeded_workspace: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    查询行: Callable[[Path, str, Sequence[object]], tuple[object, ...] | None],
) -> None:
    result = 运行_cli(["--json", "export", "run", "1", "--workspace", str(seeded_workspace)])
    payload = 读取_json输出(result.stdout)
    export_run_id = int(payload["data"]["export_run_id"])
    db_row = 查询行(
        seeded_workspace,
        "SELECT id, template_id, status FROM export_run WHERE id=?",
        (export_run_id,),
    )

    assert db_row is not None
    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"] == {"export_run_id": export_run_id, "status": "running"}
    assert db_row == (export_run_id, 1, "running")
