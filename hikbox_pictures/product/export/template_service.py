from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from hikbox_pictures.product.db.connection import connect_sqlite

from . import ExportValidationError, ensure_export_schema


@dataclass(frozen=True)
class ExportTemplateRecord:
    id: int
    name: str
    output_root: str
    enabled: bool
    person_ids: list[int]
    created_at: str
    updated_at: str


class ExportTemplateService:
    def __init__(self, library_db_path: Path) -> None:
        self._library_db_path = library_db_path

    def create_template(self, *, name: str, output_root: Path, person_ids: Sequence[int]) -> ExportTemplateRecord:
        cleaned_name = str(name).strip()
        if not cleaned_name:
            raise ExportValidationError("模板名称不能为空")
        normalized_output_root = _normalize_output_root(output_root)
        normalized_person_ids = _normalize_person_ids(person_ids)
        now = _utc_now()
        with connect_sqlite(self._library_db_path) as conn:
            ensure_export_schema(conn)
            self._validate_template_persons(conn, normalized_person_ids)
            cursor = conn.execute(
                """
                INSERT INTO export_template(name, output_root, enabled, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?)
                """,
                (cleaned_name, str(normalized_output_root), now, now),
            )
            template_id = int(cursor.lastrowid)
            for person_id in normalized_person_ids:
                conn.execute(
                    """
                    INSERT INTO export_template_person(template_id, person_id, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (template_id, person_id, now),
                )
            conn.commit()
            return self._load_template(conn, template_id)

    def list_templates(self) -> list[ExportTemplateRecord]:
        with connect_sqlite(self._library_db_path) as conn:
            ensure_export_schema(conn)
            rows = conn.execute(
                """
                SELECT id
                FROM export_template
                ORDER BY id
                """
            ).fetchall()
            return [self._load_template(conn, int(row[0])) for row in rows]

    def update_template(
        self,
        *,
        template_id: int,
        name: str | None = None,
        output_root: Path | None = None,
        enabled: bool | None = None,
        person_ids: Sequence[int] | None = None,
    ) -> ExportTemplateRecord:
        with connect_sqlite(self._library_db_path) as conn:
            ensure_export_schema(conn)
            current = self._load_template(conn, int(template_id))

            next_name = current.name if name is None else str(name).strip()
            if not next_name:
                raise ExportValidationError("模板名称不能为空")

            if output_root is None:
                next_output_root = current.output_root
            else:
                next_output_root = str(_normalize_output_root(output_root))
            next_enabled = int(current.enabled if enabled is None else bool(enabled))

            conn.execute(
                """
                UPDATE export_template
                SET name=?, output_root=?, enabled=?, updated_at=?
                WHERE id=?
                """,
                (next_name, next_output_root, next_enabled, _utc_now(), int(template_id)),
            )

            if person_ids is not None:
                normalized_person_ids = _normalize_person_ids(person_ids)
                self._validate_template_persons(conn, normalized_person_ids)
                conn.execute("DELETE FROM export_template_person WHERE template_id=?", (int(template_id),))
                now = _utc_now()
                for person_id in normalized_person_ids:
                    conn.execute(
                        """
                        INSERT INTO export_template_person(template_id, person_id, created_at)
                        VALUES (?, ?, ?)
                        """,
                        (int(template_id), person_id, now),
                    )

            conn.commit()
            return self._load_template(conn, int(template_id))

    def _validate_template_persons(self, conn, person_ids: Sequence[int]) -> None:
        placeholders = ",".join("?" for _ in person_ids)
        rows = conn.execute(
            f"""
            SELECT id
            FROM person
            WHERE id IN ({placeholders})
              AND is_named=1
              AND status='active'
            """,
            tuple(person_ids),
        ).fetchall()
        valid_ids = {int(row[0]) for row in rows}
        invalid_ids = [person_id for person_id in person_ids if person_id not in valid_ids]
        if invalid_ids:
            raise ExportValidationError(
                f"模板人物必须满足 is_named=1 且 status='active'，非法 person_id={invalid_ids}"
            )

    def _load_template(self, conn, template_id: int) -> ExportTemplateRecord:
        row = conn.execute(
            """
            SELECT id, name, output_root, enabled, created_at, updated_at
            FROM export_template
            WHERE id=?
            """,
            (int(template_id),),
        ).fetchone()
        if row is None:
            raise ExportValidationError(f"模板不存在: template_id={template_id}")
        person_rows = conn.execute(
            """
            SELECT person_id
            FROM export_template_person
            WHERE template_id=?
            ORDER BY person_id
            """,
            (int(template_id),),
        ).fetchall()
        return ExportTemplateRecord(
            id=int(row[0]),
            name=str(row[1]),
            output_root=str(row[2]),
            enabled=bool(int(row[3])),
            person_ids=[int(person_row[0]) for person_row in person_rows],
            created_at=str(row[4]),
            updated_at=str(row[5]),
        )


def _normalize_output_root(output_root: Path) -> Path:
    root = Path(output_root)
    if not root.is_absolute():
        raise ExportValidationError("output_root 必须是绝对路径")
    return root.resolve()


def _normalize_person_ids(person_ids: Sequence[int]) -> list[int]:
    normalized: list[int] = []
    seen: set[int] = set()
    for person_id in person_ids:
        value = int(person_id)
        if value <= 0:
            raise ExportValidationError(f"person_id 非法: {person_id}")
        if value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    if not normalized:
        raise ExportValidationError("模板必须至少选择一位人物")
    return normalized


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "ExportTemplateRecord",
    "ExportTemplateService",
    "ExportValidationError",
]
