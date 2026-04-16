#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migration (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


# 该选择集来自 2026-04-16 本地旧库快照，用于生成体积小且关系完整的 v2 测试库。
SELECTION = {
    "library_source": [1, 2],
    "scan_session": [1, 2],
    "scan_session_source": [1, 2, 4, 5],
    "photo_asset": [1, 4, 10, 205, 206],
    "face_observation": [1, 2, 3, 5, 8, 25, 219, 220],
    "person": [1, 2, 3],
    "person_face_assignment": [1, 2, 28, 29, 59, 72],
    "person_prototype": [3, 33, 34],
    "review_item": [1, 5, 219],
    "export_template": [1],
    "export_template_person": [1, 2],
}


def _load_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [str(row[1]) for row in rows]


def _copy_by_ids(
    *,
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    table: str,
    ids: list[int],
    id_col: str = "id",
) -> int:
    if not ids:
        return 0

    columns = _load_columns(src, table)
    col_sql = ", ".join(columns)
    placeholders = ",".join("?" for _ in ids)
    rows = src.execute(
        f"SELECT {col_sql} FROM {table} WHERE {id_col} IN ({placeholders}) ORDER BY {id_col} ASC",
        tuple(ids),
    ).fetchall()
    if not rows:
        return 0

    value_marks = ", ".join("?" for _ in columns)
    insert_sql = f"INSERT INTO {table}({col_sql}) VALUES ({value_marks})"
    for row in rows:
        dst.execute(insert_sql, tuple(row[idx] for idx in range(len(columns))))
    return len(rows)


def _copy_by_where(
    *,
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    table: str,
    where_sql: str,
    params: tuple[object, ...] = (),
) -> int:
    columns = _load_columns(src, table)
    col_sql = ", ".join(columns)
    rows = src.execute(
        f"SELECT {col_sql} FROM {table} WHERE {where_sql}",
        params,
    ).fetchall()
    if not rows:
        return 0

    value_marks = ", ".join("?" for _ in columns)
    insert_sql = f"INSERT INTO {table}({col_sql}) VALUES ({value_marks})"
    for row in rows:
        dst.execute(insert_sql, tuple(row[idx] for idx in range(len(columns))))
    return len(rows)


def _init_v2_schema(dst: sqlite3.Connection, repo_root: Path) -> None:
    dst.execute(MIGRATION_TABLE_SQL)

    scripts = [
        repo_root / "src/hikbox_pictures/db/migrations/0001_people_gallery.sql",
        repo_root / "src/hikbox_pictures/db/migrations/0002_photo_asset_progress_index.sql",
        repo_root / "src/hikbox_pictures/db/migrations/0003_person_face_exclusion.sql",
    ]
    for path in scripts:
        dst.executescript(path.read_text(encoding="utf-8"))

    for version, name in [(1, "people_gallery"), (2, "photo_asset_progress_index"), (3, "person_face_exclusion")]:
        dst.execute(
            "INSERT INTO schema_migration(version, name) VALUES (?, ?)",
            (version, name),
        )


def build_fixture(*, source_db: Path, output_db: Path, repo_root: Path) -> None:
    if not source_db.exists():
        raise FileNotFoundError(f"旧库不存在: {source_db}")

    output_db.parent.mkdir(parents=True, exist_ok=True)
    if output_db.exists():
        output_db.unlink()

    src = sqlite3.connect(source_db)
    dst = sqlite3.connect(output_db)

    try:
        _init_v2_schema(dst, repo_root)
        dst.commit()

        dst.execute("PRAGMA foreign_keys = OFF")

        copied: dict[str, int] = {}
        copied["library_source"] = _copy_by_ids(src=src, dst=dst, table="library_source", ids=SELECTION["library_source"])
        copied["scan_session"] = _copy_by_ids(src=src, dst=dst, table="scan_session", ids=SELECTION["scan_session"])
        copied["scan_session_source"] = _copy_by_ids(src=src, dst=dst, table="scan_session_source", ids=SELECTION["scan_session_source"])

        copied["scan_checkpoint"] = _copy_by_where(
            src=src,
            dst=dst,
            table="scan_checkpoint",
            where_sql="scan_session_source_id IN (1, 2, 4, 5)",
        )

        copied["photo_asset"] = _copy_by_ids(src=src, dst=dst, table="photo_asset", ids=SELECTION["photo_asset"])
        copied["face_observation"] = _copy_by_ids(src=src, dst=dst, table="face_observation", ids=SELECTION["face_observation"])
        copied["face_embedding"] = _copy_by_ids(
            src=src,
            dst=dst,
            table="face_embedding",
            ids=SELECTION["face_observation"],
            id_col="face_observation_id",
        )

        copied["person"] = _copy_by_ids(src=src, dst=dst, table="person", ids=SELECTION["person"])
        copied["person_face_assignment"] = _copy_by_ids(
            src=src,
            dst=dst,
            table="person_face_assignment",
            ids=SELECTION["person_face_assignment"],
        )
        copied["person_prototype"] = _copy_by_ids(src=src, dst=dst, table="person_prototype", ids=SELECTION["person_prototype"])
        copied["review_item"] = _copy_by_ids(src=src, dst=dst, table="review_item", ids=SELECTION["review_item"])

        copied["export_template"] = _copy_by_ids(src=src, dst=dst, table="export_template", ids=SELECTION["export_template"])
        copied["export_template_person"] = _copy_by_ids(
            src=src,
            dst=dst,
            table="export_template_person",
            ids=SELECTION["export_template_person"],
        )

        # 当前抽样不包含这些表的业务数据，保留空表结构即可。
        _copy_by_where(src=src, dst=dst, table="person_face_exclusion", where_sql="0")
        _copy_by_where(src=src, dst=dst, table="export_run", where_sql="0")
        _copy_by_where(src=src, dst=dst, table="export_delivery", where_sql="0")
        _copy_by_where(src=src, dst=dst, table="ops_event", where_sql="0")

        dst.commit()
        dst.execute("PRAGMA foreign_keys = ON")

        violations = dst.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"fixture 外键校验失败: {violations[:5]}")

        dst.execute("VACUUM")
        size = output_db.stat().st_size
        print(f"已生成: {output_db}")
        print(f"文件大小: {size} bytes")
        for table, count in copied.items():
            print(f"{table}: {count}")
    finally:
        src.close()
        dst.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="从旧版大库构建最小 v2 测试库 fixture")
    parser.add_argument(
        "--source-db",
        type=Path,
        default=Path(".hikbox/.hikbox/library.db"),
        help="源旧库路径（默认: .hikbox/.hikbox/library.db）",
    )
    parser.add_argument(
        "--output-db",
        type=Path,
        default=Path("tests/data/legacy-v2-small.db"),
        help="输出 fixture 路径（默认: tests/data/legacy-v2-small.db）",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    build_fixture(source_db=args.source_db.resolve(), output_db=(repo_root / args.output_db).resolve(), repo_root=repo_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
