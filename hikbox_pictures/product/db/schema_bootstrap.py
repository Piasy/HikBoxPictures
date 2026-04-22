"""数据库 schema 初始化。"""

from __future__ import annotations

from pathlib import Path

from hikbox_pictures.product.db.connection import connect_sqlite
from hikbox_pictures.product.db.schema_meta import (
    EMBEDDING_SCHEMA_VERSION_VALUE,
    EMBEDDING_SCHEMA_VERSION_KEY,
    EMBEDDING_VECTOR_DIM_KEY,
    EMBEDDING_VECTOR_DIM_VALUE,
    EMBEDDING_VECTOR_DTYPE_KEY,
    EMBEDDING_VECTOR_DTYPE_VALUE,
    LIBRARY_SCHEMA_NAME_KEY,
    LIBRARY_SCHEMA_NAME_VALUE,
    LIBRARY_SCHEMA_VERSION_KEY,
    LIBRARY_SCHEMA_VERSION_VALUE,
)


def _read_sql_text(filename: str) -> str:
    sql_ref = Path(__file__).parent / "sql" / filename
    return sql_ref.read_text(encoding="utf-8")


def _bootstrap_db(db_path: Path, *, sql_filename: str) -> None:
    sql_text = _read_sql_text(sql_filename)
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(sql_text)
        conn.commit()
    finally:
        conn.close()


def bootstrap_library_db(db_path: Path) -> None:
    _bootstrap_db(db_path, sql_filename="library_v1.sql")
    _upsert_meta(
        db_path=db_path,
        table_name="schema_meta",
        items={
            LIBRARY_SCHEMA_VERSION_KEY: LIBRARY_SCHEMA_VERSION_VALUE,
            LIBRARY_SCHEMA_NAME_KEY: LIBRARY_SCHEMA_NAME_VALUE,
        },
    )


def bootstrap_embedding_db(db_path: Path) -> None:
    _bootstrap_db(db_path, sql_filename="embedding_v1.sql")
    _upsert_meta(
        db_path=db_path,
        table_name="embedding_meta",
        items={
            EMBEDDING_SCHEMA_VERSION_KEY: EMBEDDING_SCHEMA_VERSION_VALUE,
            EMBEDDING_VECTOR_DIM_KEY: EMBEDDING_VECTOR_DIM_VALUE,
            EMBEDDING_VECTOR_DTYPE_KEY: EMBEDDING_VECTOR_DTYPE_VALUE,
        },
    )


def _upsert_meta(db_path: Path, *, table_name: str, items: dict[str, str]) -> None:
    conn = connect_sqlite(db_path)
    try:
        for key, value in items.items():
            conn.execute(
                f"""
                INSERT INTO {table_name}(key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                  value=excluded.value,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (key, value),
            )
        conn.commit()
    finally:
        conn.close()
