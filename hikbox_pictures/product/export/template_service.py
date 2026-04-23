"""导出模板服务。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from hikbox_pictures.product.db.connection import connect_sqlite


class ExportError(Exception):
    """导出域基础异常。"""


class ExportValidationError(ExportError):
    """参数校验失败。"""


class ExportTemplateNotFoundError(ExportError):
    """导出模板不存在。"""


class ExportTemplateDuplicateError(ExportError):
    """导出模板名称重复。"""


@dataclass(frozen=True)
class ExportTemplateRecord:
    id: int
    name: str
    output_root: str
    enabled: bool
    person_ids: list[int]


_UNCHANGED = object()


class ExportTemplateService:
    """导出模板 create/list/update 服务。"""

    def __init__(self, library_db_path: Path):
        self._library_db_path = Path(library_db_path)

    def create_template(self, *, name: str, output_root: str, person_ids: list[int], enabled: bool = True) -> ExportTemplateRecord:
        normalized_name = _normalize_name(name)
        normalized_output_root = _normalize_output_root(output_root)
        normalized_person_ids = _normalize_person_ids(person_ids)
        conn = connect_sqlite(self._library_db_path)
        conn.row_factory = sqlite3.Row
        try:
            self._assert_name_available(conn, name=normalized_name, exclude_template_id=None)
            self._assert_selectable_people(conn, person_ids=normalized_person_ids)
            cursor = conn.execute(
                """
                INSERT INTO export_template(name, output_root, enabled, created_at, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (normalized_name, normalized_output_root, int(enabled)),
            )
            template_id = int(cursor.lastrowid)
            self._replace_template_people(conn, template_id=template_id, person_ids=normalized_person_ids)
            conn.commit()
            return self._get_template(conn, template_id)
        finally:
            conn.close()

    def list_templates(self) -> list[ExportTemplateRecord]:
        conn = connect_sqlite(self._library_db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT id
                FROM export_template
                ORDER BY id ASC
                """
            ).fetchall()
            return [self._get_template(conn, int(row["id"])) for row in rows]
        finally:
            conn.close()

    def update_template(
        self,
        template_id: int,
        *,
        name: str | object = _UNCHANGED,
        output_root: str | object = _UNCHANGED,
        enabled: bool | object = _UNCHANGED,
        person_ids: list[int] | object = _UNCHANGED,
    ) -> ExportTemplateRecord:
        conn = connect_sqlite(self._library_db_path)
        conn.row_factory = sqlite3.Row
        try:
            current = self._get_template(conn, int(template_id))
            next_name = current.name if name is _UNCHANGED else _normalize_name(str(name))
            next_output_root = current.output_root if output_root is _UNCHANGED else _normalize_output_root(str(output_root))
            next_enabled = current.enabled if enabled is _UNCHANGED else bool(enabled)
            next_person_ids = current.person_ids if person_ids is _UNCHANGED else _normalize_person_ids(list(person_ids))
            self._assert_name_available(conn, name=next_name, exclude_template_id=current.id)
            self._assert_selectable_people(conn, person_ids=next_person_ids)
            conn.execute(
                """
                UPDATE export_template
                SET name=?, output_root=?, enabled=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (next_name, next_output_root, int(next_enabled), current.id),
            )
            self._replace_template_people(conn, template_id=current.id, person_ids=next_person_ids)
            conn.commit()
            return self._get_template(conn, current.id)
        finally:
            conn.close()

    def get_template(self, template_id: int) -> ExportTemplateRecord:
        conn = connect_sqlite(self._library_db_path)
        conn.row_factory = sqlite3.Row
        try:
            return self._get_template(conn, int(template_id))
        finally:
            conn.close()

    def _assert_name_available(self, conn: sqlite3.Connection, *, name: str, exclude_template_id: int | None) -> None:
        if exclude_template_id is None:
            row = conn.execute(
                "SELECT id FROM export_template WHERE name=?",
                (name,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM export_template WHERE name=? AND id<>?",
                (name, int(exclude_template_id)),
            ).fetchone()
        if row is not None:
            raise ExportTemplateDuplicateError(f"导出模板名称重复: {name}")

    def _assert_selectable_people(self, conn: sqlite3.Connection, *, person_ids: list[int]) -> None:
        if not person_ids:
            raise ExportValidationError("模板至少选择一个人物")
        placeholders = ",".join("?" for _ in person_ids)
        rows = conn.execute(
            f"""
            SELECT id
            FROM person
            WHERE id IN ({placeholders})
              AND is_named=1
              AND status='active'
            ORDER BY id ASC
            """,
            tuple(person_ids),
        ).fetchall()
        valid_ids = {int(row["id"]) if isinstance(row, sqlite3.Row) else int(row[0]) for row in rows}
        invalid_ids = [person_id for person_id in person_ids if int(person_id) not in valid_ids]
        if invalid_ids:
            raise ExportValidationError(f"模板人物必须全部是已命名且 active: {invalid_ids}")

    def _replace_template_people(self, conn: sqlite3.Connection, *, template_id: int, person_ids: list[int]) -> None:
        conn.execute("DELETE FROM export_template_person WHERE template_id=?", (int(template_id),))
        conn.executemany(
            """
            INSERT INTO export_template_person(template_id, person_id, created_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            """,
            [(int(template_id), int(person_id)) for person_id in person_ids],
        )

    def _get_template(self, conn: sqlite3.Connection, template_id: int) -> ExportTemplateRecord:
        row = conn.execute(
            """
            SELECT id, name, output_root, enabled
            FROM export_template
            WHERE id=?
            """,
            (int(template_id),),
        ).fetchone()
        if row is None:
            raise ExportTemplateNotFoundError(f"导出模板不存在: {template_id}")
        person_rows = conn.execute(
            """
            SELECT person_id
            FROM export_template_person
            WHERE template_id=?
            ORDER BY person_id ASC
            """,
            (int(template_id),),
        ).fetchall()
        person_ids = [int(person_row["person_id"]) if isinstance(person_row, sqlite3.Row) else int(person_row[0]) for person_row in person_rows]
        return ExportTemplateRecord(
            id=int(row["id"]) if isinstance(row, sqlite3.Row) else int(row[0]),
            name=str(row["name"]) if isinstance(row, sqlite3.Row) else str(row[1]),
            output_root=str(row["output_root"]) if isinstance(row, sqlite3.Row) else str(row[2]),
            enabled=bool(row["enabled"]) if isinstance(row, sqlite3.Row) else bool(row[3]),
            person_ids=person_ids,
        )


def _normalize_name(name: str) -> str:
    normalized = name.strip()
    if not normalized:
        raise ExportValidationError("模板名称不能为空")
    return normalized


def _normalize_output_root(output_root: str) -> str:
    normalized = Path(output_root).expanduser()
    if not normalized.is_absolute():
        raise ExportValidationError("output_root 必须是显式绝对路径")
    return str(normalized)


def _normalize_person_ids(person_ids: list[int]) -> list[int]:
    normalized = sorted({int(person_id) for person_id in person_ids if int(person_id) > 0})
    if not normalized:
        raise ExportValidationError("模板至少选择一个人物")
    return normalized
