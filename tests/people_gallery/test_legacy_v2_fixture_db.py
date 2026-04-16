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
            "auto_cluster_batch": _table_count(conn, "auto_cluster_batch"),
            "auto_cluster": _table_count(conn, "auto_cluster"),
            "auto_cluster_member": _table_count(conn, "auto_cluster_member"),
        }
        assert before_counts["person"] > 0
        assert before_counts["person_face_assignment"] > 0
        assert before_counts["auto_cluster_batch"] > 0
        assert before_counts["auto_cluster"] > 0
        assert before_counts["auto_cluster_member"] > 0

        apply_migrations(conn)

        after_counts = {
            "person": _table_count(conn, "person"),
            "person_face_assignment": _table_count(conn, "person_face_assignment"),
            "review_item": _table_count(conn, "review_item"),
            "export_template": _table_count(conn, "export_template"),
            "auto_cluster_batch": _table_count(conn, "auto_cluster_batch"),
            "auto_cluster": _table_count(conn, "auto_cluster"),
            "auto_cluster_member": _table_count(conn, "auto_cluster_member"),
        }
        assert after_counts == before_counts

        pfa_cols = {str(row["name"]) for row in conn.execute("PRAGMA table_info(person_face_assignment)").fetchall()}
        assert "confidence" not in pfa_cols
        assert {"diagnostic_json", "threshold_profile_id"}.issubset(pfa_cols)
        person_cols = {str(row["name"]) for row in conn.execute("PRAGMA table_info(person)").fetchall()}
        assert "origin_cluster_id" in person_cols
        assert "cover_observation_id" in person_cols
        person_row = conn.execute("SELECT cover_observation_id FROM person WHERE id = 1").fetchone()
        assert person_row is not None
        assert int(person_row["cover_observation_id"]) == 101

        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert fk_violations == []

        applied_versions = [
            int(row["version"])
            for row in conn.execute("SELECT version FROM schema_migration ORDER BY version").fetchall()
        ]
        assert applied_versions == [1, 2, 3, 4]
    finally:
        conn.close()
