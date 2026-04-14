from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from hikbox_pictures.api.app import create_app
from hikbox_pictures.cli import main
from tests.people_gallery.real_image_helper import copy_group_face_image, copy_raw_face_image


def _write_real_photo(path: Path, *, index: int, group: bool = False) -> None:
    if group:
        copy_group_face_image(path, index=index)
        return
    copy_raw_face_image(path, index=index)


def _connect_workspace_db(workspace: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(workspace / ".hikbox" / "library.db")
    conn.row_factory = sqlite3.Row
    return conn


def test_scan_discovers_source_files_and_completes_session(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    source_root = tmp_path / "input"
    _write_real_photo(source_root / "a.jpg", index=0)
    _write_real_photo(source_root / "nested" / "b.jpg", index=1, group=True)

    assert main(["init", "--workspace", str(workspace), "--external-root", str(workspace / ".hikbox")]) == 0
    assert (
        main(
            [
                "source",
                "add",
                "--workspace",
                str(workspace),
                "--name",
                "sample-input",
                "--root-path",
                str(source_root),
            ]
        )
        == 0
    )
    assert main(["scan", "--workspace", str(workspace)]) == 0

    conn = _connect_workspace_db(workspace)
    try:
        asset_count = int(conn.execute("SELECT COUNT(*) AS c FROM photo_asset").fetchone()["c"])
        done_count = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM photo_asset WHERE processing_status = 'assignment_done'"
            ).fetchone()["c"]
        )
        latest_session = conn.execute(
            "SELECT id, status FROM scan_session ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert latest_session is not None
        session_source = conn.execute(
            """
            SELECT status, discovered_count, metadata_done_count, faces_done_count,
                   embeddings_done_count, assignment_done_count
            FROM scan_session_source
            WHERE scan_session_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(latest_session["id"]),),
        ).fetchone()
        assert session_source is not None
        phases = [
            str(row["phase"])
            for row in conn.execute(
                """
                SELECT phase
                FROM scan_checkpoint
                WHERE scan_session_source_id = (
                    SELECT id
                    FROM scan_session_source
                    WHERE scan_session_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                )
                ORDER BY id ASC
                """,
                (int(latest_session["id"]),),
            ).fetchall()
        ]

        assert asset_count == 2
        assert done_count == 2
        assert latest_session["status"] == "completed"
        assert session_source["status"] == "completed"
        assert session_source["discovered_count"] == 2
        assert session_source["metadata_done_count"] == 2
        assert session_source["faces_done_count"] == 2
        assert session_source["embeddings_done_count"] == 2
        assert session_source["assignment_done_count"] == 2
        assert phases == ["discover", "metadata", "faces", "embeddings", "assignment"]
    finally:
        conn.close()


def test_scan_pipeline_is_idempotent_on_duplicate_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    source_root = tmp_path / "input"
    _write_real_photo(source_root / "a.jpg", index=0)
    _write_real_photo(source_root / "b.jpg", index=1)

    assert main(["init", "--workspace", str(workspace), "--external-root", str(workspace / ".hikbox")]) == 0
    assert (
        main(
            [
                "source",
                "add",
                "--workspace",
                str(workspace),
                "--name",
                "sample-input",
                "--root-path",
                str(source_root),
            ]
        )
        == 0
    )

    assert main(["scan", "--workspace", str(workspace)]) == 0
    assert main(["scan", "--workspace", str(workspace)]) == 0

    conn = _connect_workspace_db(workspace)
    try:
        total_assets = int(conn.execute("SELECT COUNT(*) AS c FROM photo_asset").fetchone()["c"])
        duplicate_rows = int(
            conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM (
                    SELECT library_source_id, primary_path, COUNT(*) AS n
                    FROM photo_asset
                    GROUP BY library_source_id, primary_path
                    HAVING n > 1
                )
                """
            ).fetchone()["c"]
        )
        latest_session = conn.execute(
            "SELECT status FROM scan_session ORDER BY id DESC LIMIT 1"
        ).fetchone()

        assert total_assets == 2
        assert duplicate_rows == 0
        assert latest_session is not None
        assert latest_session["status"] == "completed"
    finally:
        conn.close()


def test_api_scan_start_or_resume_executes_real_pipeline(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    source_root = tmp_path / "input"
    _write_real_photo(source_root / "1.jpg", index=0)
    _write_real_photo(source_root / "2.jpg", index=1)

    assert main(["init", "--workspace", str(workspace), "--external-root", str(workspace / ".hikbox")]) == 0
    assert (
        main(
            [
                "source",
                "add",
                "--workspace",
                str(workspace),
                "--name",
                "api-input",
                "--root-path",
                str(source_root),
            ]
        )
        == 0
    )

    client = TestClient(create_app(workspace=workspace))
    response = client.post("/api/scan/start_or_resume")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"

    status_response = client.get("/api/scan/status")
    assert status_response.status_code == 200
    status_body = status_response.json()
    assert status_body["status"] == "completed"
    assert status_body["session_id"] == body["session_id"]
    assert len(status_body["sources"]) == 1

    conn = _connect_workspace_db(workspace)
    try:
        latest_session = conn.execute(
            "SELECT id, status FROM scan_session ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert latest_session is not None
        assert latest_session["status"] == "completed"
        source = conn.execute(
            """
            SELECT status, discovered_count, assignment_done_count
            FROM scan_session_source
            WHERE scan_session_id = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (int(latest_session["id"]),),
        ).fetchone()
        assert source is not None
        assert source["status"] == "completed"
        assert source["discovered_count"] == 2
        assert source["assignment_done_count"] == 2
    finally:
        conn.close()


def test_scan_missing_source_marks_session_failed(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "ws"
    source_root = tmp_path / "input"
    _write_real_photo(source_root / "a.jpg", index=0)

    assert main(["init", "--workspace", str(workspace), "--external-root", str(workspace / ".hikbox")]) == 0
    assert (
        main(
            [
                "source",
                "add",
                "--workspace",
                str(workspace),
                "--name",
                "broken-input",
                "--root-path",
                str(source_root),
            ]
        )
        == 0
    )

    (source_root / "a.jpg").unlink()
    source_root.rmdir()
    capsys.readouterr()

    assert main(["scan", "--workspace", str(workspace)]) == 1
    out = capsys.readouterr().out
    assert "scan session_id=" in out
    assert "status=failed" in out

    conn = _connect_workspace_db(workspace)
    try:
        latest_session = conn.execute(
            "SELECT id, status FROM scan_session ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert latest_session is not None
        assert latest_session["status"] == "failed"
        session_source = conn.execute(
            """
            SELECT status, cursor_json
            FROM scan_session_source
            WHERE scan_session_id = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (int(latest_session["id"]),),
        ).fetchone()
        assert session_source is not None
        assert session_source["status"] == "failed"
        assert "扫描源目录不存在或不可访问" in str(session_source["cursor_json"])
    finally:
        conn.close()


def test_scan_stops_after_source_becomes_abandoned(tmp_path: Path) -> None:
    from hikbox_pictures.db.connection import connect_db
    from hikbox_pictures.services.scan_execution_service import ScanExecutionService
    from hikbox_pictures.services.scan_orchestrator import ScanOrchestrator

    workspace = tmp_path / "ws"
    source_root = tmp_path / "input"
    _write_real_photo(source_root / "1.jpg", index=0)
    _write_real_photo(source_root / "2.jpg", index=1)

    assert main(["init", "--workspace", str(workspace), "--external-root", str(workspace / ".hikbox")]) == 0
    assert (
        main(
            [
                "source",
                "add",
                "--workspace",
                str(workspace),
                "--name",
                "abandon-input",
                "--root-path",
                str(source_root),
            ]
        )
        == 0
    )

    db_path = workspace / ".hikbox" / "library.db"
    conn = connect_db(db_path)
    try:
        orchestrator = ScanOrchestrator(conn)
        session_id = orchestrator.start_or_resume()
        session_sources = orchestrator.scan_repo.list_session_sources(session_id)
        assert len(session_sources) == 1
        session_source_id = int(session_sources[0]["id"])

        def checkpoint_writer(source_id: int, phase: str, cursor_json: str | None, pending: int) -> int:
            checkpoint_id = orchestrator.write_checkpoint(
                source_id,
                phase=phase,
                cursor_json=cursor_json,
                pending_asset_count=pending,
            )
            if phase == "discover":
                other = connect_db(db_path)
                try:
                    other.execute(
                        """
                        UPDATE scan_session_source
                        SET status = 'abandoned',
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (int(source_id),),
                    )
                    other.commit()
                finally:
                    other.close()
            return checkpoint_id

        result = ScanExecutionService(conn, checkpoint_writer=checkpoint_writer).run_session(session_id)
        assert result["completed_source_count"] == 0
        assert result["failed_source_count"] == 0
        assert result["session_completed"] == 1

        latest_session = orchestrator.scan_repo.get_session(session_id)
        assert latest_session is not None
        assert latest_session["status"] == "completed"

        source_state = orchestrator.scan_repo.get_session_source(session_source_id)
        assert source_state is not None
        assert source_state["status"] == "abandoned"
        assert source_state["discovered_count"] == 2
        assert source_state["metadata_done_count"] == 0
        assert source_state["faces_done_count"] == 0
        assert source_state["embeddings_done_count"] == 0
        assert source_state["assignment_done_count"] == 0

        phases = [
            str(row["phase"])
            for row in conn.execute(
                """
                SELECT phase
                FROM scan_checkpoint
                WHERE scan_session_source_id = ?
                ORDER BY id ASC
                """,
                (session_source_id,),
            ).fetchall()
        ]
        assert phases == ["discover"]
    finally:
        conn.close()
