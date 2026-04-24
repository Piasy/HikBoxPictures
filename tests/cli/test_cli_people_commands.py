from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from tests.cli.conftest import 读取_json输出


def test_people_list_返回全量人物并与_db真值一致(
    seeded_workspace: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    查询行: Callable[[Path, str, Sequence[object]], tuple[object, ...] | None],
    查询多行: Callable[[Path, str, Sequence[object]], list[tuple[object, ...]]],
) -> None:
    run_list = 运行_cli(["--json", "people", "list", "--workspace", str(seeded_workspace)])
    payload = 读取_json输出(run_list.stdout)
    db_rows = 查询多行(
        seeded_workspace,
        """
        SELECT id, person_uuid, display_name, is_named, status
        FROM person
        WHERE status='active'
        ORDER BY id ASC
        """,
    )

    assert run_list.returncode == 0
    assert payload["ok"] is True
    assert payload["data"]["total"] == len(db_rows)
    assert payload["data"]["items"] == [
        {
            "person_id": row[0],
            "person_uuid": row[1],
            "display_name": row[2],
            "is_named": bool(row[3]),
            "status": row[4],
        }
        for row in db_rows
    ]


def test_people_list_named_仅返回已命名人物并与_db真值一致(
    seeded_workspace: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    查询多行: Callable[[Path, str, Sequence[object]], list[tuple[object, ...]]],
) -> None:
    result = 运行_cli(["--json", "people", "list", "--named", "--workspace", str(seeded_workspace)])
    payload = 读取_json输出(result.stdout)
    db_rows = 查询多行(
        seeded_workspace,
        """
        SELECT id, person_uuid, display_name, is_named, status
        FROM person
        WHERE status='active' AND is_named=1
        ORDER BY id ASC
        """,
    )

    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"]["total"] == len(db_rows)
    assert payload["data"]["items"] == [
        {
            "person_id": row[0],
            "person_uuid": row[1],
            "display_name": row[2],
            "is_named": True,
            "status": row[4],
        }
        for row in db_rows
    ]


def test_people_list_anonymous_仅返回匿名人物并与_db真值一致(
    seeded_workspace: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    查询多行: Callable[[Path, str, Sequence[object]], list[tuple[object, ...]]],
) -> None:
    result = 运行_cli(["--json", "people", "list", "--anonymous", "--workspace", str(seeded_workspace)])
    payload = 读取_json输出(result.stdout)
    db_rows = 查询多行(
        seeded_workspace,
        """
        SELECT id, person_uuid, display_name, is_named, status
        FROM person
        WHERE status='active' AND is_named=0
        ORDER BY id ASC
        """,
    )

    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"]["total"] == len(db_rows)
    assert payload["data"]["items"] == [
        {
            "person_id": row[0],
            "person_uuid": row[1],
            "display_name": row[2],
            "is_named": False,
            "status": row[4],
        }
        for row in db_rows
    ]


def test_people_show_返回人物详情并与_db真值一致(
    seeded_workspace: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    查询行: Callable[[Path, str, Sequence[object]], tuple[object, ...] | None],
    查询多行: Callable[[Path, str, Sequence[object]], list[tuple[object, ...]]],
) -> None:
    result = 运行_cli(["--json", "people", "show", "1", "--workspace", str(seeded_workspace)])
    payload = 读取_json输出(result.stdout)
    db_person = 查询行(
        seeded_workspace,
        """
        SELECT id, person_uuid, display_name, is_named, status, created_at, updated_at
        FROM person
        WHERE id=1
        """,
    )
    db_samples = 查询多行(
        seeded_workspace,
        """
        SELECT
          f.id,
          f.crop_relpath,
          f.context_relpath,
          f.photo_asset_id,
          COALESCE(p.is_live_photo, 0),
          p.live_mov_path,
          f.quality_score,
          f.magface_quality,
          a.assignment_source,
          a.confidence,
          a.margin
        FROM face_observation AS f
        LEFT JOIN person_face_assignment AS a
          ON a.face_observation_id=f.id
         AND a.active=1
        LEFT JOIN photo_asset AS p
          ON p.id=f.photo_asset_id
        WHERE a.person_id=1
        ORDER BY f.id ASC
        """,
    )

    assert db_person is not None
    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"]["person"] == {
        "id": db_person[0],
        "person_uuid": db_person[1],
        "display_name": db_person[2],
        "is_named": db_person[3],
        "status": db_person[4],
        "created_at": db_person[5],
        "updated_at": db_person[6],
    }
    assert payload["data"]["samples"] == [
        {
            "face_observation_id": row[0],
            "crop_relpath": row[1],
            "context_relpath": row[2],
            "photo_asset_id": row[3],
            "is_live_photo": row[4],
            "live_mov_path": row[5],
            "quality_score": row[6],
            "magface_quality": row[7],
            "assignment_source": row[8],
            "confidence": row[9],
            "margin": row[10],
        }
        for row in db_samples
    ]


def test_people_rename_更新人物名称并与_db真值一致(
    seeded_workspace: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    查询行: Callable[[Path, str, Sequence[object]], tuple[object, ...] | None],
) -> None:
    result = 运行_cli(["--json", "people", "rename", "1", "family-2026", "--workspace", str(seeded_workspace)])
    payload = 读取_json输出(result.stdout)
    db_person = 查询行(
        seeded_workspace,
        "SELECT id, display_name, is_named FROM person WHERE id=1",
    )

    assert db_person is not None
    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"] == {
        "person_id": db_person[0],
        "display_name": db_person[1],
        "is_named": bool(db_person[2]),
    }


def test_people_exclude_写入单条排除并与_db真值一致(
    seeded_workspace: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    查询行: Callable[[Path, str, Sequence[object]], tuple[object, ...] | None],
) -> None:
    result = 运行_cli(
        [
            "--json",
            "people",
            "exclude",
            "2",
            "--face-observation-id",
            "1",
            "--workspace",
            str(seeded_workspace),
        ]
    )
    payload = 读取_json输出(result.stdout)
    db_exclusion = 查询行(
        seeded_workspace,
        """
        SELECT person_id, face_observation_id, active
        FROM person_face_exclusion
        WHERE person_id=2 AND face_observation_id=1
        ORDER BY id DESC
        LIMIT 1
        """,
    )
    db_face = 查询行(
        seeded_workspace,
        "SELECT pending_reassign FROM face_observation WHERE id=1",
    )

    assert db_exclusion is not None
    assert db_face is not None
    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"] == {
        "person_id": db_exclusion[0],
        "face_observation_id": db_exclusion[1],
        "pending_reassign": db_face[0],
    }
    assert db_exclusion[2] == 1


def test_people_exclude_batch_写入批量排除并与_db真值一致(
    seeded_workspace: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    查询多行: Callable[[Path, str, Sequence[object]], list[tuple[object, ...]]],
) -> None:
    result = 运行_cli(
        [
            "--json",
            "people",
            "exclude-batch",
            "3",
            "--face-observation-ids",
            "2,3",
            "--workspace",
            str(seeded_workspace),
        ]
    )
    payload = 读取_json输出(result.stdout)
    db_rows = 查询多行(
        seeded_workspace,
        """
        SELECT person_id, face_observation_id, active
        FROM person_face_exclusion
        WHERE person_id=3 AND face_observation_id IN (2, 3)
        ORDER BY face_observation_id ASC
        """,
    )

    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"] == {"person_id": 3, "excluded_count": 2}
    assert db_rows == [(3, 2, 1), (3, 3, 1)]


def test_people_merge_执行合并并与_db真值一致(
    seeded_workspace: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    查询行: Callable[[Path, str, Sequence[object]], tuple[object, ...] | None],
) -> None:
    result = 运行_cli(
        [
            "--json",
            "people",
            "merge",
            "--selected-person-ids",
            "4,5",
            "--workspace",
            str(seeded_workspace),
        ]
    )
    payload = 读取_json输出(result.stdout)
    merge_row = 查询行(
        seeded_workspace,
        "SELECT winner_person_id, winner_person_uuid, status FROM merge_operation WHERE id=?",
        (payload["data"]["merge_operation_id"],),
    )

    assert merge_row is not None
    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"] == {
        "merge_operation_id": payload["data"]["merge_operation_id"],
        "winner_person_id": merge_row[0],
        "winner_person_uuid": merge_row[1],
    }
    assert merge_row[2] == "applied"


def test_people_undo_last_merge_回滚最近一次合并并与_db真值一致(
    seeded_workspace: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
    查询行: Callable[[Path, str, Sequence[object]], tuple[object, ...] | None],
) -> None:
    merge_result = 运行_cli(
        [
            "--json",
            "people",
            "merge",
            "--selected-person-ids",
            "4,5",
            "--workspace",
            str(seeded_workspace),
        ]
    )
    merge_payload = 读取_json输出(merge_result.stdout)

    result = 运行_cli(["--json", "people", "undo-last-merge", "--workspace", str(seeded_workspace)])
    payload = 读取_json输出(result.stdout)
    merge_row = 查询行(
        seeded_workspace,
        "SELECT id, status FROM merge_operation WHERE id=?",
        (merge_payload["data"]["merge_operation_id"],),
    )

    assert merge_row is not None
    assert result.returncode == 0
    assert payload["ok"] is True
    assert payload["data"] == {"merge_operation_id": merge_row[0], "status": merge_row[1]}
    assert merge_row[1] == "undone"
