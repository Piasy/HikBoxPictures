from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .connection import connect_sqlite
from .schema_meta import EMBEDDING_REQUIRED_META, LIBRARY_REQUIRED_META


SQL_DIR = Path(__file__).resolve().parent / "sql"


def bootstrap_databases(library_db_path: Path, embedding_db_path: Path) -> None:
    bootstrap_library_schema(library_db_path)
    bootstrap_embedding_schema(embedding_db_path)


def bootstrap_library_schema(db_path: Path) -> None:
    with connect_sqlite(db_path) as conn:
        conn.executescript(_load_sql("library_v1.sql"))
        _insert_missing_meta(conn, "schema_meta", LIBRARY_REQUIRED_META)
        conn.commit()


def bootstrap_embedding_schema(db_path: Path) -> None:
    with connect_sqlite(db_path) as conn:
        conn.executescript(_load_sql("embedding_v1.sql"))
        _insert_missing_meta(conn, "embedding_meta", EMBEDDING_REQUIRED_META)
        conn.commit()


def _load_sql(filename: str) -> str:
    return (SQL_DIR / filename).read_text(encoding="utf-8")


def _insert_missing_meta(conn: sqlite3.Connection, table_name: str, values: dict[str, str]) -> None:
    now = _utc_now()
    for key, value in values.items():
        conn.execute(
            f"""
            INSERT INTO {table_name} (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO NOTHING
            """,
            (key, value, now),
        )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
