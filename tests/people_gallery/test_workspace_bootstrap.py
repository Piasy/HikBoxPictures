from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from hikbox_pictures.db import connection as db_connection
from hikbox_pictures.db import migrator as db_migrator
from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.db.migrator import apply_migrations
from hikbox_pictures.workspace import init_workspace_layout, load_workspace_paths


def _init_paths(tmp_path):
    return init_workspace_layout(tmp_path / "workspace", tmp_path / "external-root")


def test_workspace_layout_and_tables(tmp_path):
    paths = _init_paths(tmp_path)

    assert paths.root == (tmp_path / "workspace").resolve()
    assert paths.external_root == (tmp_path / "external-root").resolve()
    assert paths.config_path == tmp_path / "workspace" / ".hikbox" / "config.json"
    assert paths.db_path == tmp_path / "workspace" / ".hikbox" / "library.db"
    assert paths.db_path.exists()
    assert paths.config_path.exists()
    assert (tmp_path / "external-root" / "artifacts" / "ann").exists()
    assert (tmp_path / "external-root" / "artifacts" / "thumbs").exists()
    assert (tmp_path / "external-root" / "artifacts" / "face-crops").exists()
    assert (tmp_path / "external-root" / "logs" / "runs").exists()
    assert (tmp_path / "external-root" / "exports").exists()

    loaded = load_workspace_paths(tmp_path / "workspace")
    assert loaded == paths

    conn = connect_db(paths.db_path)
    apply_migrations(conn)

    table_names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    index_names = {row[1] for row in conn.execute("PRAGMA index_list('photo_asset')").fetchall()}
    required = {
        "library_source",
        "scan_session",
        "scan_session_source",
        "scan_checkpoint",
        "photo_asset",
        "face_observation",
        "face_embedding",
        "auto_cluster_batch",
        "auto_cluster",
        "auto_cluster_member",
        "identity_threshold_profile",
        "identity_observation_profile",
        "identity_observation_snapshot",
        "identity_observation_pool_entry",
        "identity_cluster_profile",
        "identity_cluster_run",
        "identity_cluster",
        "identity_cluster_lineage",
        "identity_cluster_member",
        "identity_cluster_resolution",
        "person",
        "person_cluster_origin",
        "person_face_assignment",
        "person_face_exclusion",
        "person_trusted_sample",
        "person_prototype",
        "review_item",
        "export_template",
        "export_template_person",
        "export_run",
        "export_delivery",
        "ops_event",
    }
    assert required <= table_names
    assert "idx_photo_asset_source_status" in index_names
    schema_0005 = conn.execute(
        "SELECT COUNT(*) FROM schema_migration WHERE version = 5"
    ).fetchone()
    assert int(schema_0005[0]) == 1


def test_workspace_and_external_root_can_be_same_path(tmp_path):
    paths = init_workspace_layout(tmp_path, tmp_path)

    assert paths.db_path == tmp_path / ".hikbox" / "library.db"
    assert paths.artifacts_dir == tmp_path / "artifacts"
    assert paths.logs_dir == tmp_path / "logs"
    assert paths.exports_dir == tmp_path / "exports"


def test_load_workspace_paths_requires_config_json(tmp_path):
    with pytest.raises(FileNotFoundError, match="config.json"):
        load_workspace_paths(tmp_path)



def test_scan_session_only_single_running_allowed(tmp_path):
    paths = _init_paths(tmp_path)
    conn = connect_db(paths.db_path)
    apply_migrations(conn)

    conn.execute("INSERT INTO scan_session(mode, status) VALUES ('incremental', 'running')")
    conn.commit()
    with pytest.raises(db_connection.sqlite3.IntegrityError):
        conn.execute("INSERT INTO scan_session(mode, status) VALUES ('resume', 'running')")



def test_assignment_active_uniqueness_keeps_inactive_history(tmp_path):
    paths = _init_paths(tmp_path)
    conn = connect_db(paths.db_path)
    apply_migrations(conn)

    conn.execute("INSERT INTO person(display_name, status) VALUES ('人物A', 'active')")
    conn.execute("INSERT INTO person(display_name, status) VALUES ('人物B', 'active')")
    conn.execute("INSERT INTO library_source(name, root_path, active) VALUES ('s1', '/tmp/source1', 1)")
    conn.execute("INSERT INTO photo_asset(library_source_id, primary_path, processing_status) VALUES (1, '/tmp/a.jpg', 'discovered')")
    conn.execute(
        """
        INSERT INTO face_observation(photo_asset_id, bbox_top, bbox_right, bbox_bottom, bbox_left)
        VALUES (1, 0, 10, 10, 0)
        """
    )

    conn.execute(
        """
        INSERT INTO person_face_assignment(person_id, face_observation_id, assignment_source, active)
        VALUES (1, 1, 'manual', 0)
        """
    )
    conn.execute(
        """
        INSERT INTO person_face_assignment(person_id, face_observation_id, assignment_source, active)
        VALUES (2, 1, 'manual', 0)
        """
    )

    conn.execute(
        """
        INSERT INTO person_face_assignment(person_id, face_observation_id, assignment_source, active)
        VALUES (1, 1, 'manual', 1)
        """
    )
    conn.commit()

    with pytest.raises(db_connection.sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO person_face_assignment(person_id, face_observation_id, assignment_source, active)
            VALUES (2, 1, 'manual', 1)
            """
        )



def test_library_source_allows_same_root_when_old_row_inactive(tmp_path):
    paths = _init_paths(tmp_path)
    conn = connect_db(paths.db_path)
    apply_migrations(conn)

    conn.execute("INSERT INTO library_source(name, root_path, active) VALUES ('old', '/data/a', 0)")
    conn.execute("INSERT INTO library_source(name, root_path, active) VALUES ('new', '/data/a', 1)")
    conn.commit()

    with pytest.raises(db_connection.sqlite3.IntegrityError):
        conn.execute("INSERT INTO library_source(name, root_path, active) VALUES ('dup', '/data/a', 1)")


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO scan_session(mode, status) VALUES ('bad', 'pending')",
        "INSERT INTO scan_session(mode, status) VALUES ('incremental', 'bad')",
        "INSERT INTO person(display_name, status) VALUES ('人物X', 'bad')",
        "INSERT INTO person_face_assignment(person_id, face_observation_id, assignment_source, active) VALUES (1, 1, 'bad', 1)",
        "INSERT INTO review_item(review_type, payload_json, status) VALUES ('bad', '{}', 'open')",
        "INSERT INTO export_delivery(template_id, spec_hash, photo_asset_id, asset_variant, bucket, target_path, status) VALUES (1, 's', 1, 'bad', 'only', '/tmp/x', 'ok')",
    ],
)
def test_enum_checks_reject_invalid_values(tmp_path, sql: str):
    paths = _init_paths(tmp_path)
    conn = connect_db(paths.db_path)
    apply_migrations(conn)

    conn.execute("INSERT INTO library_source(name, root_path, active) VALUES ('s1', '/tmp/source', 1)")
    conn.execute("INSERT INTO photo_asset(library_source_id, primary_path, processing_status) VALUES (1, '/tmp/a.jpg', 'discovered')")
    conn.execute("INSERT INTO face_observation(photo_asset_id, bbox_top, bbox_right, bbox_bottom, bbox_left) VALUES (1, 0, 10, 10, 0)")
    conn.execute("INSERT INTO person(display_name, status) VALUES ('人物A', 'active')")
    conn.execute("INSERT INTO export_template(name, output_root) VALUES ('模板', '/tmp/out')")
    conn.commit()

    with pytest.raises(db_connection.sqlite3.IntegrityError):
        conn.execute(sql)



def test_apply_migrations_is_idempotent(tmp_path):
    paths = _init_paths(tmp_path)
    conn = connect_db(paths.db_path)

    apply_migrations(conn)
    apply_migrations(conn)

    row = conn.execute("SELECT COUNT(*) FROM schema_migration WHERE version=1").fetchone()
    assert int(row[0]) == 1


def test_apply_migrations_does_not_require_write_lock_when_schema_is_current(tmp_path):
    paths = _init_paths(tmp_path)
    bootstrap_conn = connect_db(paths.db_path)
    apply_migrations(bootstrap_conn)
    bootstrap_conn.close()

    writer_conn = connect_db(paths.db_path)
    check_conn = connect_db(paths.db_path)
    check_conn.execute("PRAGMA busy_timeout=100")

    writer_conn.execute("BEGIN IMMEDIATE")
    try:
        apply_migrations(check_conn)
    finally:
        writer_conn.rollback()
        writer_conn.close()
        check_conn.close()


def test_apply_migrations_rolls_back_partial_failed_script(tmp_path, monkeypatch):
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir(parents=True, exist_ok=True)
    (migration_dir / "0001_partial_fail.sql").write_text(
        """
        CREATE TABLE tx_partial_success (
            id INTEGER PRIMARY KEY
        );
        INSERT INTO missing_table(col) VALUES (1);
        """,
        encoding="utf-8",
    )

    monkeypatch.setattr(db_migrator, "_migration_dir", lambda: migration_dir)

    paths = _init_paths(tmp_path)
    conn = connect_db(paths.db_path)

    with pytest.raises(RuntimeError):
        apply_migrations(conn)

    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tx_partial_success'"
    ).fetchone()
    assert table is None
    applied = conn.execute("SELECT COUNT(*) FROM schema_migration").fetchone()
    assert int(applied[0]) == 0


def test_apply_migrations_rejects_duplicate_versions(tmp_path, monkeypatch):
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir(parents=True, exist_ok=True)
    (migration_dir / "0001_a.sql").write_text("CREATE TABLE t1 (id INTEGER PRIMARY KEY);", encoding="utf-8")
    (migration_dir / "0001_b.sql").write_text("CREATE TABLE t2 (id INTEGER PRIMARY KEY);", encoding="utf-8")

    monkeypatch.setattr(db_migrator, "_migration_dir", lambda: migration_dir)

    paths = _init_paths(tmp_path)
    conn = connect_db(paths.db_path)

    with pytest.raises(ValueError, match="迁移版本重复"):
        apply_migrations(conn)


def test_apply_migrations_rejects_invalid_migration_filename(tmp_path, monkeypatch):
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir(parents=True, exist_ok=True)
    (migration_dir / "bad.sql").write_text("SELECT 1;", encoding="utf-8")

    monkeypatch.setattr(db_migrator, "_migration_dir", lambda: migration_dir)

    paths = _init_paths(tmp_path)
    conn = connect_db(paths.db_path)

    with pytest.raises(ValueError, match="迁移文件命名非法"):
        apply_migrations(conn)


def test_apply_migrations_supports_trigger_statement(tmp_path, monkeypatch):
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir(parents=True, exist_ok=True)
    (migration_dir / "0001_trigger.sql").write_text(
        """
        CREATE TABLE trigger_source (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            value TEXT NOT NULL
        );
        CREATE TABLE trigger_target (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            value TEXT NOT NULL
        );
        CREATE TRIGGER trigger_copy_after_insert
        AFTER INSERT ON trigger_source
        BEGIN
            INSERT INTO trigger_target(value) VALUES (NEW.value);
        END;
        """,
        encoding="utf-8",
    )

    monkeypatch.setattr(db_migrator, "_migration_dir", lambda: migration_dir)

    paths = _init_paths(tmp_path)
    conn = connect_db(paths.db_path)
    apply_migrations(conn)

    conn.execute("INSERT INTO trigger_source(value) VALUES ('ok')")
    conn.commit()
    row = conn.execute("SELECT value FROM trigger_target LIMIT 1").fetchone()
    assert row is not None
    assert row[0] == "ok"


def test_connect_db_concurrent_initialization_is_stable(tmp_path):
    paths = _init_paths(tmp_path)

    def open_and_close() -> None:
        conn = connect_db(paths.db_path)
        conn.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(open_and_close) for _ in range(16)]
        for future in futures:
            future.result()
