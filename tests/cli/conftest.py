from __future__ import annotations

import json
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from hikbox_pictures.product.config import WorkspaceLayout
from tests.product.task6_test_support import create_task6_workspace, seed_face_observations


@pytest.fixture
def 仓库根目录() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture
def cli_python(仓库根目录: Path) -> Path:
    current = 仓库根目录
    for candidate_root in [current, *current.parents]:
        python_bin = candidate_root / ".venv" / "bin" / "python"
        if python_bin.exists():
            return python_bin
    raise AssertionError(f"找不到 Python 可执行文件: {仓库根目录 / '.venv' / 'bin' / 'python'}")


@pytest.fixture
def cli命令前缀(cli_python: Path) -> list[str]:
    return [str(cli_python), "-m", "hikbox_pictures.cli"]


@pytest.fixture
def 运行_cli(
    仓库根目录: Path,
    cli命令前缀: list[str],
) -> Callable[[Sequence[str]], subprocess.CompletedProcess[str]]:
    def _运行(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*cli命令前缀, *args],
            cwd=仓库根目录,
            text=True,
            capture_output=True,
            check=False,
        )

    return _运行


@pytest.fixture
def 启动_cli进程(
    仓库根目录: Path,
    cli命令前缀: list[str],
) -> Callable[[Sequence[str]], subprocess.Popen[str]]:
    def _启动(args: Sequence[str]) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [*cli命令前缀, *args],
            cwd=仓库根目录,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    return _启动


@pytest.fixture
def 等待_http_ok() -> Callable[[str], bool]:
    def _等待(url: str, *, timeout_seconds: float = 8.0) -> bool:
        deadline = time.time() + timeout_seconds
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=0.5) as response:
                    if 200 <= response.status < 500:
                        return True
            except (OSError, urllib.error.URLError) as exc:
                last_error = exc
                time.sleep(0.1)
        if last_error is not None:
            raise AssertionError(f"等待 HTTP 服务就绪失败: {last_error}") from last_error
        return False

    return _等待


@pytest.fixture
def 查询单值() -> Callable[[Path, str, Sequence[object]], object]:
    def _查询(workspace: Path, sql: str, params: Sequence[object] = ()) -> object:
        db_path = workspace / ".hikbox" / "library.db"
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(sql, tuple(params)).fetchone()
        finally:
            conn.close()
        assert row is not None, f"SQL 未返回结果: {sql}"
        return row[0]

    return _查询


@pytest.fixture
def 查询行() -> Callable[[Path, str, Sequence[object]], tuple[object, ...] | None]:
    def _查询(workspace: Path, sql: str, params: Sequence[object] = ()) -> tuple[object, ...] | None:
        db_path = workspace / ".hikbox" / "library.db"
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(sql, tuple(params)).fetchone()
        finally:
            conn.close()
        return None if row is None else tuple(row)

    return _查询


@pytest.fixture
def 查询多行() -> Callable[[Path, str, Sequence[object]], list[tuple[object, ...]]]:
    def _查询(workspace: Path, sql: str, params: Sequence[object] = ()) -> list[tuple[object, ...]]:
        db_path = workspace / ".hikbox" / "library.db"
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(sql, tuple(params)).fetchall()
        finally:
            conn.close()
        return [tuple(row) for row in rows]

    return _查询


@pytest.fixture
def 插入扫描会话() -> Callable[[Path, str, str, str], int]:
    def _插入(
        workspace: Path,
        *,
        status: str,
        run_kind: str = "scan_full",
        triggered_by: str = "manual_cli",
    ) -> int:
        db_path = workspace / ".hikbox" / "library.db"
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.execute(
                """
                INSERT INTO scan_session(
                  run_kind, status, triggered_by, resume_from_session_id, started_at, finished_at, last_error,
                  created_at, updated_at
                ) VALUES (?, ?, ?, NULL, NULL, NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (run_kind, status, triggered_by),
            )
            conn.commit()
            return int(cursor.lastrowid)
        finally:
            conn.close()

    return _插入


@pytest.fixture
def 已初始化工作区(
    tmp_path: Path,
    运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]],
) -> Path:
    workspace = tmp_path / "workspace"
    result = 运行_cli(["init", "--workspace", str(workspace)])
    assert result.returncode == 0, result.stderr
    return workspace


@pytest.fixture
def 已播种工作区(tmp_path: Path) -> Path:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    face_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (210, 180, 160)},
            {"asset_index": 0, "color": (220, 190, 170)},
            {"asset_index": 1, "color": (180, 180, 210)},
            {"asset_index": 1, "color": (170, 170, 220), "pending_reassign": True},
        ],
    )
    _seed_cli_data(layout=layout, session_id=session_id, face_ids=face_ids)
    _execute_sql(
        layout.workspace_root,
        "UPDATE scan_session SET status='completed', finished_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (session_id,),
    )
    return layout.workspace_root


@pytest.fixture
def seeded_workspace(已播种工作区: Path) -> Path:
    return 已播种工作区


@pytest.fixture
def cli帮助输出(运行_cli: Callable[[Sequence[str]], subprocess.CompletedProcess[str]]) -> Callable[[Sequence[str]], str]:
    def _读取(args: Sequence[str]) -> str:
        result = 运行_cli([*args, "--help"])
        assert result.returncode == 0
        assert result.stderr == ""
        return result.stdout

    return _读取


def 读取_json输出(stdout: str) -> dict[str, object]:
    lines = [line.strip() for line in str(stdout).splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return json.loads(stdout)


def _seed_cli_data(*, layout: WorkspaceLayout, session_id: int, face_ids: list[int]) -> None:
    conn = sqlite3.connect(layout.library_db)
    try:
        rename_person_id = _insert_person(conn, display_name="Rename Me", is_named=True)
        exclude_person_id = _insert_person(conn, display_name="Exclude One", is_named=True)
        exclude_batch_person_id = _insert_person(conn, display_name="Exclude Batch", is_named=True)
        merge_winner_person_id = _insert_person(conn, display_name="Winner", is_named=True)
        merge_loser_person_id = _insert_person(conn, display_name="Loser", is_named=True)
        template_person_id = _insert_person(conn, display_name="Template Person", is_named=True)
        _insert_person(conn, display_name="anonymous-7", is_named=False)

        assignment_run_id = int(
            conn.execute(
                """
                INSERT INTO assignment_run(
                  scan_session_id, algorithm_version, param_snapshot_json, run_kind, started_at, finished_at, status, updated_at
                ) VALUES (?, 'frozen_v5', ?, 'scan_full', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'completed', CURRENT_TIMESTAMP)
                """,
                (
                    session_id,
                    json.dumps({"det_size": 640, "workers": 4, "batch_size": 300}, ensure_ascii=False),
                ),
            ).lastrowid
        )

        conn.executemany(
            """
            INSERT INTO person_face_assignment(
              person_id, face_observation_id, assignment_run_id, assignment_source, active, confidence, margin, created_at, updated_at
            ) VALUES (?, ?, ?, 'hdbscan', 1, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            [
                (exclude_person_id, face_ids[0], assignment_run_id, 0.93, 0.11),
                (exclude_batch_person_id, face_ids[1], assignment_run_id, 0.94, 0.12),
                (exclude_batch_person_id, face_ids[2], assignment_run_id, 0.95, 0.13),
                (merge_winner_person_id, face_ids[3], assignment_run_id, 0.96, 0.14),
            ],
        )

        merge_extra_face_id = int(
            conn.execute(
                """
                INSERT INTO face_observation(
                  photo_asset_id, face_index, crop_relpath, aligned_relpath, context_relpath,
                  bbox_x1, bbox_y1, bbox_x2, bbox_y2,
                  detector_confidence, face_area_ratio, magface_quality, quality_score,
                  active, inactive_reason, pending_reassign, created_at, updated_at
                ) VALUES (2, 99, 'artifacts/crops/m99.jpg', 'artifacts/aligned/m99.png', 'artifacts/context/m99.jpg',
                  10, 10, 80, 80, 0.98, 0.3, 1.2, 0.9, 1, NULL, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO person_face_assignment(
              person_id, face_observation_id, assignment_run_id, assignment_source, active, confidence, margin, created_at, updated_at
            ) VALUES (?, ?, ?, 'hdbscan', 1, 0.97, 0.15, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (merge_loser_person_id, merge_extra_face_id, assignment_run_id),
        )

        template_id = int(
            conn.execute(
                """
                INSERT INTO export_template(name, output_root, enabled, created_at, updated_at)
                VALUES ('模板一', ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (str(layout.workspace_root / "exports-template"),),
            ).lastrowid
        )
        conn.execute(
            """
            INSERT INTO export_template_person(template_id, person_id, created_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            """,
            (template_id, template_person_id),
        )

        export_run_id = int(
            conn.execute(
                """
                INSERT INTO export_run(template_id, status, summary_json, started_at, finished_at)
                VALUES (?, 'running', ?, CURRENT_TIMESTAMP, NULL)
                """,
                (
                    template_id,
                    json.dumps(
                        {"exported_count": 0, "skipped_exists_count": 0, "failed_count": 0},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            ).lastrowid
        )
        conn.execute(
            """
            UPDATE export_run
            SET status='completed', finished_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (export_run_id,),
        )

        conn.execute(
            """
            INSERT INTO scan_audit_item(
              scan_session_id, assignment_run_id, audit_type, face_observation_id, person_id, evidence_json, created_at
            ) VALUES (?, ?, 'reassign_after_exclusion', ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                session_id,
                assignment_run_id,
                face_ids[0],
                exclude_person_id,
                json.dumps({"person_id": exclude_person_id, "face_observation_id": face_ids[0]}, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _execute_sql(workspace: Path, sql: str, params: Sequence[object]) -> None:
    db_path = workspace / ".hikbox" / "library.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(sql, tuple(params))
        conn.commit()
    finally:
        conn.close()


def _insert_person(conn: sqlite3.Connection, *, display_name: str, is_named: bool) -> int:
    return int(
        conn.execute(
            """
            INSERT INTO person(person_uuid, display_name, is_named, status, created_at, updated_at)
            VALUES (?, ?, ?, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (str(uuid.uuid4()), display_name, 1 if is_named else 0),
        ).lastrowid
    )
