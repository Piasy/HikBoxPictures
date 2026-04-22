from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .conftest import query_one, run_cli


def test_export_template_list_create_update_and_run(cli_bin: str, seeded_workspace: Path) -> None:
    output_root = (seeded_workspace / "exports" / "named-only").resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    create_tpl = run_cli(
        cli_bin,
        "--json",
        "export",
        "template",
        "create",
        "--name",
        "named-only",
        "--output-root",
        str(output_root),
        "--person-ids",
        "1",
        "--workspace",
        str(seeded_workspace),
    )
    assert create_tpl.returncode == 0
    template_id = int(json.loads(create_tpl.stdout)["data"]["template_id"])

    update_tpl = run_cli(
        cli_bin,
        "--json",
        "export",
        "template",
        "update",
        str(template_id),
        "--name",
        "named-only-v2",
        "--workspace",
        str(seeded_workspace),
    )
    assert update_tpl.returncode == 0
    update_data = json.loads(update_tpl.stdout)["data"]
    assert update_data["template_id"] == template_id
    assert update_data["updated"] is True
    assert query_one(seeded_workspace, "SELECT name FROM export_template WHERE id=?", [template_id])[0] == "named-only-v2"

    list_tpl = run_cli(cli_bin, "--json", "export", "template", "list", "--workspace", str(seeded_workspace))
    assert list_tpl.returncode == 0
    list_items = json.loads(list_tpl.stdout)["data"]["items"]
    db_path = seeded_workspace / ".hikbox" / "library.db"
    with sqlite3.connect(db_path) as conn:
        db_templates = {
            int(row[0]): {
                "template_id": int(row[0]),
                "name": str(row[1]),
                "output_root": str(row[2]),
                "enabled": bool(int(row[3])),
                "person_ids": [
                    int(item[0])
                    for item in conn.execute(
                        "SELECT person_id FROM export_template_person WHERE template_id=? ORDER BY person_id",
                        [int(row[0])],
                    ).fetchall()
                ],
            }
            for row in conn.execute("SELECT id, name, output_root, enabled FROM export_template ORDER BY id")
        }
    assert {int(item["template_id"]) for item in list_items} == set(db_templates.keys())
    for item in list_items:
        assert item == db_templates[int(item["template_id"])]

    run_export = run_cli(
        cli_bin,
        "--json",
        "export",
        "run",
        str(template_id),
        "--workspace",
        str(seeded_workspace),
    )
    assert run_export.returncode == 0
    run_data = json.loads(run_export.stdout)["data"]
    export_run_id = int(run_data["export_run_id"])
    assert run_data["status"] == "completed"
    assert query_one(seeded_workspace, "SELECT COUNT(*) FROM export_run WHERE id=? AND template_id=?", [export_run_id, template_id])[0] == 1
