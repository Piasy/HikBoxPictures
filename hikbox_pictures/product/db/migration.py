from __future__ import annotations

import re
import sqlite3
from pathlib import Path


class MigrationError(RuntimeError):
    """数据库迁移失败。"""


SQL_DIR = Path(__file__).resolve().parent / "sql"


def migrate_to_latest(*, db_path: Path, db_name: str) -> None:
    """Migrate a database to the latest version.

    Reads the current ``schema_version`` from the ``schema_meta`` table, then
    discovers and executes all migration SQL files whose version number is
    greater than the current one.  Each migration's SQL statements and the
    ``schema_version`` update are wrapped in a single transaction.  If the
    database is already at (or beyond) the latest available migration version
    the function returns immediately with zero overhead.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.
    db_name:
        ``"library"`` or ``"embedding"`` -- selects which ``*_v{N}.sql``
        files to look for.
    """
    try:
        connection = sqlite3.connect(db_path)
    except sqlite3.Error as exc:
        raise MigrationError(
            f"数据库连接失败：{db_path}: {exc}"
        ) from exc
    try:
        current_version = _read_schema_version(connection)
        pending = _discover_migration_files(db_name, after_version=current_version)
        if not pending:
            return

        for version, sql_path in pending:
            _apply_migration(connection, version=version, sql_path=sql_path)
    finally:
        connection.close()


def _read_schema_version(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError as exc:
        raise MigrationError(
            f"schema_meta 表不存在或无法读取：{exc}"
        ) from exc
    if row is None:
        raise MigrationError("schema_version 键不存在于 schema_meta 表中")
    try:
        return int(row[0])
    except (ValueError, TypeError) as exc:
        raise MigrationError(
            f"schema_version 值无效（非整数）：{row[0]!r}"
        ) from exc


def _discover_migration_files(
    db_name: str,
    *,
    after_version: int,
) -> list[tuple[int, Path]]:
    """Return ``(version, path)`` pairs for migration files newer than *after_version*."""
    pattern = re.compile(rf"^{re.escape(db_name)}_v(\d+)\.sql$")
    migrations: list[tuple[int, Path]] = []
    if not SQL_DIR.is_dir():
        return migrations
    for entry in sorted(SQL_DIR.iterdir()):
        match = pattern.match(entry.name)
        if match:
            version = int(match.group(1))
            if version > after_version:
                migrations.append((version, entry))
    return sorted(migrations, key=lambda pair: pair[0])


def _split_sql_statements(sql_text: str) -> list[str]:
    """Split SQL text into individual statements, stripping comments and blanks."""
    statements: list[str] = []
    current: list[str] = []
    for line in sql_text.splitlines():
        stripped = line.strip()
        # Skip blank lines and pure comment lines
        if not stripped or stripped.startswith("--"):
            continue
        current.append(stripped)
        # If the line ends with a semicolon, we have a complete statement
        if stripped.endswith(";"):
            stmt = " ".join(current).strip()
            # Remove trailing semicolon for individual execute() calls
            if stmt.endswith(";"):
                stmt = stmt[:-1].strip()
            if stmt:
                statements.append(stmt)
            current = []
    # Handle any remaining content without trailing semicolon
    if current:
        stmt = " ".join(current).strip()
        if stmt.endswith(";"):
            stmt = stmt[:-1].strip()
        if stmt:
            statements.append(stmt)
    return statements


def _apply_migration(
    connection: sqlite3.Connection,
    *,
    version: int,
    sql_path: Path,
) -> None:
    """Execute a single migration SQL file and bump *schema_version*.

    Both the migration SQL statements and the ``schema_version`` update are
    wrapped in a single transaction.  If any statement fails, the transaction
    is rolled back and ``schema_version`` stays at the old value.

    Note: SQLite inherently auto-commits DDL statements (CREATE TABLE, CREATE
    INDEX, etc.) regardless of explicit transactions.  For DML statements
    (INSERT, UPDATE, DELETE), this transactional wrapping provides real
    atomicity with the version bump.
    """
    try:
        sql_text = sql_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MigrationError(
            f"migration SQL 文件读取失败：{sql_path}: {exc}"
        ) from exc

    statements = _split_sql_statements(sql_text)

    try:
        connection.execute("BEGIN")
        for stmt in statements:
            connection.execute(stmt)
        connection.execute(
            "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
            (str(version),),
        )
        connection.execute("COMMIT")
    except sqlite3.Error as exc:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise MigrationError(
            f"migration v{version} 执行失败（{sql_path.name}）：{exc}"
        ) from exc
