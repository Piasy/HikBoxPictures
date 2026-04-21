from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .conftest import query_one, run_cli


def test_people_commands_and_db_truth(cli_bin: str, seeded_workspace: Path) -> None:
    people_list = run_cli(cli_bin, "--json", "people", "list", "--workspace", str(seeded_workspace))
    list_body = json.loads(people_list.stdout)
    assert people_list.returncode == 0
    assert list_body["ok"] is True
    items = list_body["data"]["items"]
    assert list_body["data"]["total"] == len(items)
    with sqlite3.connect(seeded_workspace / ".hikbox" / "library.db") as conn:
        db_people = {
            row[0]: (
                row[1],
                row[2],
                bool(row[3]),
                row[4],
                row[5],
                row[6],
                row[7],
            )
            for row in conn.execute(
                "SELECT id, person_uuid, display_name, is_named, status, merged_into_person_id, created_at, updated_at FROM person"
            )
        }
        db_active_count = int(conn.execute("SELECT COUNT(*) FROM person WHERE status='active'").fetchone()[0])
        db_named_count = int(conn.execute("SELECT COUNT(*) FROM person WHERE status='active' AND is_named=1").fetchone()[0])
        db_anonymous_count = int(conn.execute("SELECT COUNT(*) FROM person WHERE status='active' AND is_named=0").fetchone()[0])
    assert list_body["data"]["total"] == db_active_count
    assert {item["person_id"] for item in items} == set(db_people.keys())
    for item in items:
        db = db_people[int(item["person_id"])]
        assert item["person_uuid"] == db[0]
        assert item["display_name"] == db[1]
        assert item["is_named"] is db[2]
        assert item["status"] == db[3]
        assert item["merged_into_person_id"] == db[4]
        assert item["created_at"] == db[5]
        assert item["updated_at"] == db[6]

    named_list = run_cli(cli_bin, "--json", "people", "list", "--named", "--workspace", str(seeded_workspace))
    assert named_list.returncode == 0
    named_data = json.loads(named_list.stdout)["data"]
    assert named_data["total"] == db_named_count
    assert named_data["total"] == len(named_data["items"])
    assert all(item["is_named"] is True for item in named_data["items"])
    for item in named_data["items"]:
        assert query_one(seeded_workspace, "SELECT is_named, status FROM person WHERE id=?", [item["person_id"]]) == (1, "active")

    anonymous_list = run_cli(cli_bin, "--json", "people", "list", "--anonymous", "--workspace", str(seeded_workspace))
    assert anonymous_list.returncode == 0
    anonymous_data = json.loads(anonymous_list.stdout)["data"]
    assert anonymous_data["total"] == db_anonymous_count
    assert anonymous_data["total"] == len(anonymous_data["items"])
    assert all(item["is_named"] is False for item in anonymous_data["items"])
    for item in anonymous_data["items"]:
        assert query_one(seeded_workspace, "SELECT is_named, status FROM person WHERE id=?", [item["person_id"]]) == (0, "active")

    named = next(item for item in items if item["display_name"] == "甲")
    person_id = int(named["person_id"])

    show = run_cli(cli_bin, "--json", "people", "show", str(person_id), "--workspace", str(seeded_workspace))
    show_body = json.loads(show.stdout)
    assert show.returncode == 0
    show_data = show_body["data"]
    assert show_data["person_id"] == person_id
    db_person = db_people[person_id]
    assert show_data["person_uuid"] == db_person[0]
    assert show_data["display_name"] == db_person[1]
    assert show_data["is_named"] is db_person[2]
    assert show_data["status"] == db_person[3]
    assert show_data["merged_into_person_id"] == db_person[4]
    assert show_data["created_at"] == db_person[5]
    assert show_data["updated_at"] == db_person[6]

    rename = run_cli(cli_bin, "--json", "people", "rename", str(person_id), "甲-新", "--workspace", str(seeded_workspace))
    assert rename.returncode == 0
    rename_data = json.loads(rename.stdout)["data"]
    assert rename_data["person_id"] == person_id
    assert rename_data["display_name"] == "甲-新"
    assert rename_data["is_named"] is True
    db_rename = query_one(seeded_workspace, "SELECT display_name, is_named FROM person WHERE id=?", [person_id])
    assert db_rename == ("甲-新", 1)

    face_id = int(
        query_one(
            seeded_workspace,
            "SELECT face_observation_id FROM person_face_assignment WHERE person_id=? AND active=1 ORDER BY id LIMIT 1",
            [person_id],
        )[0]
    )
    exclude = run_cli(
        cli_bin,
        "--json",
        "people",
        "exclude",
        str(person_id),
        "--face-observation-id",
        str(face_id),
        "--workspace",
        str(seeded_workspace),
    )
    exclude_body = json.loads(exclude.stdout)
    assert exclude.returncode == 0
    assert exclude_body["data"]["person_id"] == person_id
    assert exclude_body["data"]["face_observation_id"] == face_id
    assert exclude_body["data"]["pending_reassign"] == 1
    assert query_one(
        seeded_workspace,
        "SELECT active FROM person_face_assignment WHERE person_id=? AND face_observation_id=? ORDER BY id DESC LIMIT 1",
        [person_id, face_id],
    )[0] == 0
    assert query_one(
        seeded_workspace,
        "SELECT pending_reassign FROM face_observation WHERE id=?",
        [face_id],
    )[0] == 1

    remaining_faces = [
        int(row[0])
        for row in (
            query_one(
                seeded_workspace,
                "SELECT face_observation_id FROM person_face_assignment WHERE person_id=? AND active=1 ORDER BY face_observation_id LIMIT 1",
                [person_id],
            ),
        )
    ]
    exclude_batch = run_cli(
        cli_bin,
        "--json",
        "people",
        "exclude-batch",
        str(person_id),
        "--face-observation-ids",
        ",".join(str(v) for v in remaining_faces),
        "--workspace",
        str(seeded_workspace),
    )
    assert exclude_batch.returncode == 0
    exclude_batch_data = json.loads(exclude_batch.stdout)["data"]
    assert exclude_batch_data["person_id"] == person_id
    assert exclude_batch_data["excluded_count"] == len(remaining_faces)
    excluded_rows = query_one(
        seeded_workspace,
        "SELECT COUNT(*) FROM person_face_exclusion WHERE person_id=? AND face_observation_id IN ({}) AND active=1".format(
            ",".join("?" for _ in remaining_faces)
        ),
        [person_id, *remaining_faces],
    )[0]
    assert excluded_rows == len(remaining_faces)

    with sqlite3.connect(seeded_workspace / ".hikbox" / "library.db") as conn:
        ids = [int(row[0]) for row in conn.execute("SELECT id FROM person WHERE status='active' ORDER BY id LIMIT 2").fetchall()]
    merge = run_cli(
        cli_bin,
        "--json",
        "people",
        "merge",
        "--selected-person-ids",
        ",".join(str(v) for v in ids),
        "--workspace",
        str(seeded_workspace),
    )
    merge_body = json.loads(merge.stdout)
    assert merge.returncode == 0
    merge_data = merge_body["data"]
    merge_operation_id = int(merge_data["merge_operation_id"])
    db_merge = query_one(
        seeded_workspace,
        "SELECT winner_person_id, winner_person_uuid, status FROM merge_operation WHERE id=?",
        [merge_operation_id],
    )
    assert merge_data["winner_person_id"] == db_merge[0]
    assert merge_data["winner_person_uuid"] == db_merge[1]
    assert db_merge[2] == "applied"

    undo = run_cli(cli_bin, "--json", "people", "undo-last-merge", "--workspace", str(seeded_workspace))
    undo_body = json.loads(undo.stdout)
    assert undo.returncode == 0
    assert int(undo_body["data"]["merge_operation_id"]) == merge_operation_id
    assert undo_body["data"]["status"] == "undone"
    assert query_one(seeded_workspace, "SELECT status FROM merge_operation WHERE id=?", [merge_operation_id])[0] == "undone"
