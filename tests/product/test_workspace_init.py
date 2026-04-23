import json
import sqlite3
from pathlib import Path

import pytest

from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.db.connection import connect_sqlite


def _fetch_meta_map(db_path: Path, table: str) -> dict[str, str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"SELECT key, value FROM {table}").fetchall()
        return {str(row[0]): str(row[1]) for row in rows}
    finally:
        conn.close()


def _fetch_table_columns(db_path: Path, table: str) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return [str(row[1]) for row in rows]
    finally:
        conn.close()


def _fetch_index_names(db_path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
        return {str(row[1]) for row in rows}
    finally:
        conn.close()


def _fetch_index_list(db_path: Path, table: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(f"PRAGMA index_list({table})").fetchall()
    finally:
        conn.close()


def _fetch_index_columns(db_path: Path, index_name: str) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(f"PRAGMA index_info({index_name})").fetchall()
        return [str(row[2]) for row in rows]
    finally:
        conn.close()


def _fetch_index_sql(db_path: Path, index_name: str) -> str | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,),
        ).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


def _has_index(db_path: Path, table: str, index_name: str) -> bool:
    return index_name in _fetch_index_names(db_path, table)


def _insert_minimal_face_observation(conn: sqlite3.Connection) -> tuple[int, int]:
    cursor = conn.execute(
        "INSERT INTO library_source(root_path, label) VALUES (?, ?)",
        ("/tmp/source", "源目录"),
    )
    library_source_id = int(cursor.lastrowid)
    cursor = conn.execute(
        """
        INSERT INTO photo_asset(
            library_source_id, primary_path, primary_fingerprint, fingerprint_algo, file_size, mtime_ns
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (library_source_id, "a.jpg", "fp-1", "sha256", 10, 20),
    )
    photo_asset_id = int(cursor.lastrowid)
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
            quality_score
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            photo_asset_id,
            0,
            "crops/a_0.jpg",
            "aligned/a_0.jpg",
            "context/a_0.jpg",
            1.0,
            2.0,
            3.0,
            4.0,
            0.99,
            0.2,
            0.8,
            0.85,
        ),
    )
    face_observation_id = int(cursor.lastrowid)
    return library_source_id, face_observation_id


def _insert_minimal_person(conn: sqlite3.Connection, *, person_uuid: str) -> int:
    cursor = conn.execute(
        "INSERT INTO person(person_uuid, status) VALUES (?, ?)",
        (person_uuid, "active"),
    )
    return int(cursor.lastrowid)


def _insert_minimal_export_template(conn: sqlite3.Connection, *, name: str) -> int:
    cursor = conn.execute(
        "INSERT INTO export_template(name, output_root) VALUES (?, ?)",
        (name, f"/tmp/{name}"),
    )
    return int(cursor.lastrowid)


def _insert_minimal_export_run(conn: sqlite3.Connection, *, template_id: int) -> int:
    cursor = conn.execute(
        """
        INSERT INTO export_run(template_id, status, summary_json, started_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (template_id, "running", "{}"),
    )
    return int(cursor.lastrowid)


def _insert_minimal_assignment_run(conn: sqlite3.Connection) -> tuple[int, int]:
    cursor = conn.execute(
        """
        INSERT INTO scan_session(run_kind, status, triggered_by)
        VALUES (?, ?, ?)
        """,
        ("scan_full", "completed", "manual_cli"),
    )
    scan_session_id = int(cursor.lastrowid)
    cursor = conn.execute(
        """
        INSERT INTO assignment_run(
            scan_session_id, algorithm_version, param_snapshot_json, run_kind, started_at, status
        ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
        """,
        (scan_session_id, "algo-v1", "{}", "scan_full", "completed"),
    )
    assignment_run_id = int(cursor.lastrowid)
    return scan_session_id, assignment_run_id


def _read_db_schema_doc() -> str:
    return (Path(__file__).resolve().parents[2] / "docs" / "db_schema.md").read_text(encoding="utf-8")


def test_init_workspace_creates_two_databases_and_config(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"

    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)

    assert layout.hikbox_root == workspace_root / ".hikbox"
    assert layout.library_db.exists()
    assert layout.embedding_db.exists()
    assert layout.config_json.exists()

    config = json.loads(layout.config_json.read_text(encoding="utf-8"))
    assert config["external_root"] == str(external_root)

    library_meta = _fetch_meta_map(layout.library_db, "schema_meta")
    assert library_meta["schema_version"] == "1"
    assert library_meta["product_schema_name"] == "people_gallery_v1"

    embedding_meta = _fetch_meta_map(layout.embedding_db, "embedding_meta")
    assert embedding_meta["schema_version"] == "1"
    assert embedding_meta["vector_dim"] == "512"
    assert embedding_meta["vector_dtype"] == "float32"


def test_init_workspace_reuses_existing_databases(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)

    library_conn = sqlite3.connect(layout.library_db)
    try:
        library_conn.execute("CREATE TABLE IF NOT EXISTS user_data(id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        library_conn.execute("INSERT INTO user_data(name) VALUES (?)", ("keep-me",))
        library_conn.commit()
    finally:
        library_conn.close()

    embedding_conn = sqlite3.connect(layout.embedding_db)
    try:
        embedding_conn.execute(
            """
            INSERT INTO face_embedding(
                face_observation_id, feature_type, model_key, variant, dim, dtype, vector_blob, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (7, "face", "test-model", "main", 512, "float32", b"\x00\x01"),
        )
        embedding_conn.commit()
    finally:
        embedding_conn.close()

    second_layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)

    verify_conn = sqlite3.connect(second_layout.library_db)
    try:
        row = verify_conn.execute("SELECT name FROM user_data ORDER BY id LIMIT 1").fetchone()
    finally:
        verify_conn.close()

    assert row is not None
    assert row[0] == "keep-me"

    verify_embedding_conn = sqlite3.connect(second_layout.embedding_db)
    try:
        embedding_row = verify_embedding_conn.execute(
            "SELECT model_key, variant, dim, dtype FROM face_embedding WHERE face_observation_id=?",
            (7,),
        ).fetchone()
    finally:
        verify_embedding_conn.close()

    assert embedding_row is not None
    assert tuple(embedding_row) == ("test-model", "main", 512, "float32")


def test_init_workspace_creates_task7_to_task9_schema_tables(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"

    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)

    assert _fetch_table_columns(layout.library_db, "person_face_exclusion") == [
        "id",
        "person_id",
        "face_observation_id",
        "reason",
        "active",
        "created_at",
        "updated_at",
    ]
    exclusion_indexes = {row["name"]: row for row in _fetch_index_list(layout.library_db, "person_face_exclusion")}
    assert "uq_person_face_exclusion_active" in exclusion_indexes
    assert exclusion_indexes["uq_person_face_exclusion_active"]["unique"] == 1
    assert exclusion_indexes["uq_person_face_exclusion_active"]["partial"] == 1
    assert _fetch_index_columns(layout.library_db, "uq_person_face_exclusion_active") == [
        "person_id",
        "face_observation_id",
    ]
    exclusion_index_sql = _fetch_index_sql(layout.library_db, "uq_person_face_exclusion_active")
    assert exclusion_index_sql is not None
    assert "ON person_face_exclusion(person_id, face_observation_id)" in exclusion_index_sql
    assert "WHERE active = 1" in exclusion_index_sql
    assert "idx_exclusion_face" in exclusion_indexes
    assert _fetch_index_columns(layout.library_db, "idx_exclusion_face") == ["face_observation_id", "active"]

    assert _fetch_table_columns(layout.library_db, "merge_operation") == [
        "id",
        "selected_person_ids_json",
        "winner_person_id",
        "winner_person_uuid",
        "status",
        "created_at",
        "undone_at",
    ]
    assert _fetch_table_columns(layout.library_db, "merge_operation_person_delta") == [
        "id",
        "merge_operation_id",
        "person_id",
        "before_snapshot_json",
        "after_snapshot_json",
    ]
    assert _fetch_index_names(layout.library_db, "merge_operation_person_delta") >= {
        "idx_merge_operation_person_delta_merge_operation",
    }
    assert _fetch_index_columns(
        layout.library_db,
        "idx_merge_operation_person_delta_merge_operation",
    ) == ["merge_operation_id"]
    assert _fetch_table_columns(layout.library_db, "merge_operation_assignment_delta") == [
        "id",
        "merge_operation_id",
        "face_observation_id",
        "before_assignment_json",
        "after_assignment_json",
    ]
    assert _fetch_index_names(layout.library_db, "merge_operation_assignment_delta") >= {
        "idx_merge_operation_assignment_delta_merge_operation",
    }
    assert _fetch_index_columns(
        layout.library_db,
        "idx_merge_operation_assignment_delta_merge_operation",
    ) == ["merge_operation_id"]
    assert _fetch_table_columns(layout.library_db, "merge_operation_exclusion_delta") == [
        "id",
        "merge_operation_id",
        "person_id",
        "face_observation_id",
        "before_exclusion_json",
        "after_exclusion_json",
    ]
    assert _fetch_index_names(layout.library_db, "merge_operation_exclusion_delta") >= {
        "idx_merge_operation_exclusion_delta_merge_operation",
    }
    assert _fetch_index_columns(
        layout.library_db,
        "idx_merge_operation_exclusion_delta_merge_operation",
    ) == ["merge_operation_id"]

    assert _fetch_table_columns(layout.library_db, "export_template") == [
        "id",
        "name",
        "output_root",
        "enabled",
        "created_at",
        "updated_at",
    ]
    assert _fetch_table_columns(layout.library_db, "export_template_person") == [
        "id",
        "template_id",
        "person_id",
        "created_at",
    ]

    assert _fetch_table_columns(layout.library_db, "export_run") == [
        "id",
        "template_id",
        "status",
        "summary_json",
        "started_at",
        "finished_at",
    ]
    assert _fetch_index_names(layout.library_db, "export_run") >= {
        "idx_export_run_status",
        "idx_export_run_template",
    }
    assert _fetch_index_columns(layout.library_db, "idx_export_run_template") == ["template_id", "started_at"]

    assert _fetch_table_columns(layout.library_db, "export_delivery") == [
        "id",
        "export_run_id",
        "photo_asset_id",
        "media_kind",
        "bucket",
        "month_key",
        "destination_path",
        "delivery_status",
        "error_message",
        "created_at",
    ]
    assert _fetch_index_names(layout.library_db, "export_delivery") >= {
        "idx_export_delivery_status",
    }

    assert _fetch_table_columns(layout.library_db, "ops_event") == [
        "id",
        "event_type",
        "severity",
        "scan_session_id",
        "export_run_id",
        "payload_json",
        "created_at",
    ]
    assert _fetch_index_names(layout.library_db, "ops_event") >= {
        "idx_ops_event_type_created",
        "idx_ops_event_scan",
        "idx_ops_event_export_run",
    }
    assert _fetch_index_columns(layout.library_db, "idx_ops_event_type_created") == ["event_type", "created_at"]
    assert _fetch_index_columns(layout.library_db, "idx_ops_event_export_run") == ["export_run_id"]

    assert _fetch_table_columns(layout.library_db, "scan_audit_item") == [
        "id",
        "scan_session_id",
        "assignment_run_id",
        "audit_type",
        "face_observation_id",
        "person_id",
        "evidence_json",
        "created_at",
    ]
    assert _fetch_index_names(layout.library_db, "scan_audit_item") >= {
        "idx_scan_audit_session",
    }
    assert _fetch_index_columns(layout.library_db, "idx_scan_audit_session") == ["scan_session_id", "audit_type"]


def test_init_workspace_enforces_schema_unique_constraints(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)

    conn = connect_sqlite(layout.library_db)
    try:
        _library_source_id, face_observation_id = _insert_minimal_face_observation(conn)
        conn.execute("INSERT INTO person(person_uuid, status) VALUES (?, ?)", ("person-a", "active"))
        conn.execute(
            """
            INSERT INTO person_face_exclusion(person_id, face_observation_id, reason, active)
            VALUES (?, ?, ?, ?)
            """,
            (1, face_observation_id, "manual_exclude", 1),
        )
        conn.execute(
            """
            INSERT INTO person_face_exclusion(person_id, face_observation_id, reason, active)
            VALUES (?, ?, ?, ?)
            """,
            (1, face_observation_id, "manual_exclude", 0),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO person_face_exclusion(person_id, face_observation_id, reason, active)
                VALUES (?, ?, ?, ?)
                """,
                (1, face_observation_id, "manual_exclude", 1),
            )

        conn.execute(
            "INSERT INTO export_template(name, output_root) VALUES (?, ?)",
            ("模板一", "/tmp/export-one"),
        )
        conn.execute("INSERT INTO person(person_uuid, status) VALUES (?, ?)", ("person-b", "active"))
        conn.execute(
            "INSERT INTO export_template_person(template_id, person_id) VALUES (?, ?)",
            (1, 2),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO export_template_person(template_id, person_id) VALUES (?, ?)",
                (1, 2),
            )

        conn.execute(
            """
            INSERT INTO export_run(template_id, status, summary_json, started_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (1, "running", "{}"),
        )
        conn.execute(
            """
            INSERT INTO export_delivery(
                export_run_id, photo_asset_id, media_kind, bucket, month_key, destination_path, delivery_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (1, 1, "photo", "only", "2026-04", "/tmp/export-one/a.jpg", "exported"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO export_delivery(
                    export_run_id, photo_asset_id, media_kind, bucket, month_key, destination_path, delivery_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (1, 1, "photo", "group", "2026-04", "/tmp/export-one/a.jpg", "failed"),
            )
    finally:
        conn.close()


def test_init_workspace_enforces_schema_checks_and_foreign_keys(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)

    conn = connect_sqlite(layout.library_db)
    try:
        person_id = _insert_minimal_person(conn, person_uuid="person-check")
        export_template_id = _insert_minimal_export_template(conn, name="模板-check")
        export_run_id = _insert_minimal_export_run(conn, template_id=export_template_id)
        scan_session_id, assignment_run_id = _insert_minimal_assignment_run(conn)
        _library_source_id, face_observation_id = _insert_minimal_face_observation(conn)

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO merge_operation(
                    selected_person_ids_json, winner_person_id, winner_person_uuid, status
                ) VALUES (?, ?, ?, ?)
                """,
                ("[1]", person_id, "person-check", "invalid"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO merge_operation(
                    selected_person_ids_json, winner_person_id, winner_person_uuid, status
                ) VALUES (?, ?, ?, ?)
                """,
                ("[999]", 999, "missing", "applied"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO export_run(template_id, status, summary_json, started_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (export_template_id, "invalid", "{}"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO export_run(template_id, status, summary_json, started_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (999, "running", "{}"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO ops_event(event_type, severity, export_run_id, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                ("export", "invalid", export_run_id, "{}"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO ops_event(event_type, severity, export_run_id, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                ("export", "info", 999, "{}"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO scan_audit_item(
                    scan_session_id, assignment_run_id, audit_type, face_observation_id, evidence_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (scan_session_id, assignment_run_id, "invalid", face_observation_id, "{}"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO scan_audit_item(
                    scan_session_id, assignment_run_id, audit_type, face_observation_id, evidence_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (scan_session_id, 999, "low_margin_auto_assign", face_observation_id, "{}"),
            )
    finally:
        conn.close()


def test_init_workspace_replays_missing_tables_on_existing_library_db(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)

    conn = connect_sqlite(layout.library_db)
    try:
        conn.execute("INSERT INTO library_source(root_path, label) VALUES (?, ?)", ("/tmp/legacy", "legacy"))
        conn.execute("DROP INDEX IF EXISTS idx_ops_event_export_run")
        conn.execute("DROP TABLE IF EXISTS scan_audit_item")
        conn.execute("DROP TABLE IF EXISTS ops_event")
        conn.execute("DROP TABLE IF EXISTS export_delivery")
        conn.execute("DROP TABLE IF EXISTS export_run")
        conn.execute("DROP TABLE IF EXISTS export_template_person")
        conn.execute("DROP TABLE IF EXISTS export_template")
        conn.execute("DROP TABLE IF EXISTS merge_operation_exclusion_delta")
        conn.execute("DROP TABLE IF EXISTS merge_operation_assignment_delta")
        conn.execute("DROP TABLE IF EXISTS merge_operation_person_delta")
        conn.execute("DROP TABLE IF EXISTS merge_operation")
        conn.execute("DROP TABLE IF EXISTS person_face_exclusion")
        conn.commit()
    finally:
        conn.close()

    replayed_layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)

    verify_conn = sqlite3.connect(replayed_layout.library_db)
    try:
        row = verify_conn.execute("SELECT root_path, label FROM library_source").fetchone()
    finally:
        verify_conn.close()

    assert row == ("/tmp/legacy", "legacy")
    assert _fetch_table_columns(replayed_layout.library_db, "person_face_exclusion") == [
        "id",
        "person_id",
        "face_observation_id",
        "reason",
        "active",
        "created_at",
        "updated_at",
    ]
    assert _fetch_index_columns(
        replayed_layout.library_db,
        "idx_merge_operation_person_delta_merge_operation",
    ) == ["merge_operation_id"]
    assert _fetch_index_columns(
        replayed_layout.library_db,
        "idx_merge_operation_assignment_delta_merge_operation",
    ) == ["merge_operation_id"]
    assert _fetch_index_columns(
        replayed_layout.library_db,
        "idx_merge_operation_exclusion_delta_merge_operation",
    ) == ["merge_operation_id"]
    assert _fetch_index_columns(replayed_layout.library_db, "idx_ops_event_export_run") == ["export_run_id"]


def test_init_workspace_replays_missing_indexes_on_existing_library_db(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)

    conn = connect_sqlite(layout.library_db)
    try:
        conn.execute("DROP INDEX IF EXISTS idx_ops_event_export_run")
        conn.commit()
    finally:
        conn.close()

    assert not _has_index(layout.library_db, "ops_event", "idx_ops_event_export_run")

    replayed_layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)

    assert _fetch_index_columns(replayed_layout.library_db, "idx_ops_event_export_run") == ["export_run_id"]


def test_db_schema_doc_mentions_reviewed_indexes() -> None:
    doc = _read_db_schema_doc()

    assert "- `idx_merge_operation_person_delta_merge_operation(merge_operation_id)`" in doc
    assert "- `idx_merge_operation_assignment_delta_merge_operation(merge_operation_id)`" in doc
    assert "- `idx_merge_operation_exclusion_delta_merge_operation(merge_operation_id)`" in doc
    assert "- `idx_ops_event_export_run(export_run_id)`" in doc
