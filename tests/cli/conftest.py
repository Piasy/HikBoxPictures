from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.export import ensure_export_schema

NOW = "2026-04-22T00:00:00+00:00"


def _project_root() -> Path:
    path = Path(__file__).resolve()
    if ".worktrees" in path.parts:
        return path.parents[4]
    return path.parents[2]


@pytest.fixture
def cli_bin() -> str:
    return str(_project_root() / ".venv" / "bin" / "python")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


@pytest.fixture
def external_root(tmp_path: Path) -> Path:
    return tmp_path / "external"


@pytest.fixture
def seeded_workspace(tmp_path: Path) -> Path:
    layout = initialize_workspace(tmp_path / "workspace", tmp_path / "external")
    _seed_workspace(layout.workspace_root)
    return layout.workspace_root


@pytest.fixture
def photos_dir(tmp_path: Path) -> Path:
    root = (tmp_path / "photos").resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.jpg").write_bytes(b"photo-a")
    (root / "b.jpg").write_bytes(b"photo-b")
    return root


def run_cli(cli_bin: str, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run([cli_bin, "-m", "hikbox_pictures.cli", *args], text=True, capture_output=True, check=False, cwd=cwd)


def query_one(workspace_root: Path, sql: str, params: list[object] | tuple[object, ...] | None = None) -> tuple[object, ...]:
    db_path = workspace_root / ".hikbox" / "library.db"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(sql, tuple(params or [])).fetchone()
    if row is None:
        raise AssertionError(f"查询无结果: {sql} params={params}")
    return tuple(row)


def create_scan_session(workspace_root: Path, *, status: str, run_kind: str = "scan_full") -> int:
    db_path = workspace_root / ".hikbox" / "library.db"
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO scan_session(
              run_kind,
              status,
              triggered_by,
              resume_from_session_id,
              started_at,
              finished_at,
              last_error,
              created_at,
              updated_at
            )
            VALUES (?, ?, 'manual_cli', NULL, ?, NULL, NULL, ?, ?)
            """,
            (run_kind, status, NOW, NOW, NOW),
        )
        conn.commit()
        return int(cursor.lastrowid)


def _seed_workspace(workspace_root: Path) -> None:
    db_path = workspace_root / ".hikbox" / "library.db"
    with sqlite3.connect(db_path) as conn:
        ensure_export_schema(conn)
        source_id = _insert_source(conn, "/tmp/photos")
        photo_1 = _insert_photo(conn, source_id=source_id, name="a.heic")
        photo_2 = _insert_photo(conn, source_id=source_id, name="b.heic")
        photo_3 = _insert_photo(conn, source_id=source_id, name="c.heic")
        face_1 = _insert_face(conn, photo_id=photo_1, face_index=0)
        face_2 = _insert_face(conn, photo_id=photo_2, face_index=1)
        face_3 = _insert_face(conn, photo_id=photo_3, face_index=2)
        person_1 = _insert_person(conn, person_uuid="00000000-0000-0000-0000-000000000101", display_name="甲", is_named=1)
        person_2 = _insert_person(conn, person_uuid="00000000-0000-0000-0000-000000000102", display_name="乙", is_named=1)
        _insert_person(conn, person_uuid="00000000-0000-0000-0000-000000000103", display_name=None, is_named=0)
        session_id = _insert_scan_session_in_conn(conn, status="completed")
        run_id = _insert_assignment_run(conn, session_id=session_id)
        _insert_assignment(conn, person_id=person_1, face_id=face_1, run_id=run_id)
        _insert_assignment(conn, person_id=person_1, face_id=face_2, run_id=run_id)
        _insert_assignment(conn, person_id=person_2, face_id=face_3, run_id=run_id)
        conn.commit()


def _insert_scan_session_in_conn(conn: sqlite3.Connection, *, status: str, run_kind: str = "scan_full") -> int:
    cursor = conn.execute(
        """
        INSERT INTO scan_session(
          run_kind,
          status,
          triggered_by,
          resume_from_session_id,
          started_at,
          finished_at,
          last_error,
          created_at,
          updated_at
        )
        VALUES (?, ?, 'manual_cli', NULL, ?, NULL, NULL, ?, ?)
        """,
        (run_kind, status, NOW, NOW, NOW),
    )
    return int(cursor.lastrowid)


def _insert_source(conn: sqlite3.Connection, root: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO library_source(root_path, label, enabled, status, last_discovered_at, created_at, updated_at)
        VALUES (?, '测试源', 1, 'active', NULL, ?, ?)
        """,
        (root, NOW, NOW),
    )
    return int(cursor.lastrowid)


def _insert_photo(conn: sqlite3.Connection, *, source_id: int, name: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO photo_asset(
          library_source_id,
          primary_path,
          primary_fingerprint,
          fingerprint_algo,
          file_size,
          mtime_ns,
          capture_datetime,
          capture_month,
          is_live_photo,
          live_mov_path,
          live_mov_size,
          live_mov_mtime_ns,
          asset_status,
          created_at,
          updated_at
        )
        VALUES (?, ?, ?, 'sha256', 100, 1710000000000000000, '2026-03-14T12:00:00+08:00', '2026-03', 0, NULL, NULL, NULL, 'active', ?, ?)
        """,
        (source_id, name, f"fp-{name}", NOW, NOW),
    )
    return int(cursor.lastrowid)


def _insert_face(conn: sqlite3.Connection, *, photo_id: int, face_index: int) -> int:
    cursor = conn.execute(
        """
        INSERT INTO face_observation(
          photo_asset_id,
          face_index,
          crop_relpath,
          aligned_relpath,
          context_relpath,
          bbox_x1,
          bbox_y1,
          bbox_x2,
          bbox_y2,
          detector_confidence,
          face_area_ratio,
          magface_quality,
          quality_score,
          active,
          inactive_reason,
          pending_reassign,
          created_at,
          updated_at
        )
        VALUES (?, ?, 'crop/a.jpg', 'aligned/a.jpg', 'context/a.jpg', 0.1, 0.1, 0.9, 0.9, 0.98, 0.2, 30.0, 0.95, 1, NULL, 0, ?, ?)
        """,
        (photo_id, face_index, NOW, NOW),
    )
    return int(cursor.lastrowid)


def _insert_person(conn: sqlite3.Connection, *, person_uuid: str, display_name: str | None, is_named: int) -> int:
    cursor = conn.execute(
        """
        INSERT INTO person(person_uuid, display_name, is_named, status, merged_into_person_id, created_at, updated_at)
        VALUES (?, ?, ?, 'active', NULL, ?, ?)
        """,
        (person_uuid, display_name, is_named, NOW, NOW),
    )
    return int(cursor.lastrowid)


def _insert_assignment_run(conn: sqlite3.Connection, *, session_id: int) -> int:
    cursor = conn.execute(
        """
        INSERT INTO assignment_run(
          scan_session_id,
          algorithm_version,
          param_snapshot_json,
          run_kind,
          started_at,
          finished_at,
          status
        )
        VALUES (?, 'v5.2026-04-21', '{}', 'scan_full', ?, ?, 'completed')
        """,
        (session_id, NOW, NOW),
    )
    return int(cursor.lastrowid)


def _insert_assignment(conn: sqlite3.Connection, *, person_id: int, face_id: int, run_id: int) -> None:
    conn.execute(
        """
        INSERT INTO person_face_assignment(
          person_id,
          face_observation_id,
          assignment_run_id,
          assignment_source,
          active,
          confidence,
          margin,
          created_at,
          updated_at
        )
        VALUES (?, ?, ?, 'hdbscan', 1, 0.9, 0.2, ?, ?)
        """,
        (person_id, face_id, run_id, NOW, NOW),
    )


def parse_json_output(proc: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return json.loads(proc.stdout or "{}")
