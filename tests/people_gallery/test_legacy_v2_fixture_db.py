from __future__ import annotations

from pathlib import Path
import shutil

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.db.migrator import apply_migrations


FIXTURE_DB = Path(__file__).resolve().parents[1] / "data" / "legacy-v2-small.db"


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
    return int(row["c"] if isinstance(row, sqlite3.Row) else row[0])


def test_legacy_v2_small_db_can_drive_real_upgrade_path(tmp_path: Path) -> None:
    assert FIXTURE_DB.exists(), f"缺少旧库 fixture: {FIXTURE_DB}"

    db_path = tmp_path / "legacy-v2-small.db"
    shutil.copy2(FIXTURE_DB, db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        before_versions = [
            int(row["version"])
            for row in conn.execute("SELECT version FROM schema_migration ORDER BY version").fetchall()
        ]
        assert before_versions == [1, 2, 3]

        before_counts = {
            "person": _table_count(conn, "person"),
            "person_face_assignment": _table_count(conn, "person_face_assignment"),
            "review_item": _table_count(conn, "review_item"),
            "export_template": _table_count(conn, "export_template"),
        }

        apply_migrations(conn)

        after_counts = {
            "person": _table_count(conn, "person"),
            "person_face_assignment": _table_count(conn, "person_face_assignment"),
            "review_item": _table_count(conn, "review_item"),
            "export_template": _table_count(conn, "export_template"),
        }
        assert after_counts == before_counts

        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert fk_violations == []

        applied_versions = [
            int(row["version"])
            for row in conn.execute("SELECT version FROM schema_migration ORDER BY version").fetchall()
        ]
        assert applied_versions[:3] == [1, 2, 3]
        assert len(applied_versions) >= 3
    finally:
        conn.close()
