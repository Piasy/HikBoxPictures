from __future__ import annotations

from pathlib import Path

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]


MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migration (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def _migration_dir() -> Path:
    return Path(__file__).resolve().parent / "migrations"


def _parse_version_and_name(path: Path) -> tuple[int, str]:
    stem = path.stem
    if "_" not in stem:
        raise ValueError(f"迁移文件命名非法: {path.name}")
    version_text, name = stem.split("_", 1)
    return int(version_text), name


def _iter_migration_files() -> list[tuple[int, str, Path]]:
    directory = _migration_dir()
    if not directory.exists():
        return []

    parsed: list[tuple[int, str, Path]] = []
    seen_versions: set[int] = set()
    for path in directory.glob("*.sql"):
        if not path.is_file():
            continue
        version, name = _parse_version_and_name(path)
        if version in seen_versions:
            raise ValueError(f"迁移版本重复: {version:04d}")
        seen_versions.add(version)
        parsed.append((version, name, path))

    return sorted(parsed, key=lambda item: item[0])


def _split_sql_statements(script: str) -> list[str]:
    statements: list[str] = []
    chunks: list[str] = []

    for line in script.splitlines(keepends=True):
        chunks.append(line)
        candidate = "".join(chunks).strip()
        if not candidate:
            continue
        if sqlite3.complete_statement(candidate):
            statements.append(candidate)
            chunks = []

    tail = "".join(chunks).strip()
    if tail:
        raise ValueError("迁移 SQL 语句不完整，缺少结束分号或括号未闭合")

    return statements


def apply_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(MIGRATION_TABLE_SQL)

    for version, name, migration_path in _iter_migration_files():
        sql_text = migration_path.read_text(encoding="utf-8")
        statements = _split_sql_statements(sql_text)
        try:
            conn.execute("BEGIN IMMEDIATE")
            applied = conn.execute(
                "SELECT 1 FROM schema_migration WHERE version=?",
                (version,),
            ).fetchone()
            if applied is not None:
                conn.execute("COMMIT")
                continue

            for statement in statements:
                conn.execute(statement)

            conn.execute(
                "INSERT INTO schema_migration(version, name) VALUES (?, ?)",
                (version, name),
            )
            conn.execute("COMMIT")
        except Exception as exc:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise RuntimeError(
                f"迁移执行失败: version={version:04d}, file={migration_path.name}, error={exc}"
            ) from exc
