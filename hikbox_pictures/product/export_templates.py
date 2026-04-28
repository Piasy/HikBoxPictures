from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import uuid

from hikbox_pictures.product.scan_shared import utc_now_text
from hikbox_pictures.product.sources import WorkspaceContext


class ExportTemplateError(RuntimeError):
    """导出模板数据访问失败。"""


class ExportTemplateValidationError(ExportTemplateError):
    """导出模板校验失败。"""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class EligiblePerson:
    person_id: str
    display_name: str
    sample_count: int


@dataclass(frozen=True)
class ExportTemplateListItem:
    template_id: str
    name: str
    output_root: str
    status: str
    created_at: str
    person_count: int
    person_ids: list[str]
    person_names: list[str]


@dataclass(frozen=True)
class ExportTemplateCreateResult:
    template_id: str


def load_eligible_persons_for_template(
    workspace_context: WorkspaceContext,
) -> list[EligiblePerson]:
    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT
              person.id,
              person.display_name,
              COUNT(person_face_assignments.id) AS sample_count
            FROM person
            INNER JOIN person_face_assignments
              ON person_face_assignments.person_id = person.id
             AND person_face_assignments.active = 1
            WHERE person.status = 'active'
              AND person.display_name IS NOT NULL
            GROUP BY person.id, person.display_name
            ORDER BY person.display_name COLLATE NOCASE ASC
            """
        ).fetchall()
    except sqlite3.Error as exc:
        raise ExportTemplateError("可用人选读取失败。") from exc
    finally:
        connection.close()

    return [
        EligiblePerson(
            person_id=str(row["id"]),
            display_name=str(row["display_name"]),
            sample_count=int(row["sample_count"]),
        )
        for row in rows
    ]


def load_export_templates_list(
    workspace_context: WorkspaceContext,
) -> list[ExportTemplateListItem]:
    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        template_rows = connection.execute(
            """
            SELECT
              export_template.template_id,
              export_template.name,
              export_template.output_root,
              export_template.status,
              export_template.created_at,
              COUNT(export_template_person.person_id) AS person_count
            FROM export_template
            LEFT JOIN export_template_person
              ON export_template_person.template_id = export_template.template_id
            GROUP BY
              export_template.template_id,
              export_template.name,
              export_template.output_root,
              export_template.status,
              export_template.created_at
            ORDER BY export_template.created_at DESC
            """
        ).fetchall()

        person_rows = connection.execute(
            """
            SELECT
              export_template_person.template_id,
              export_template_person.person_id,
              person.display_name
            FROM export_template_person
            INNER JOIN person
              ON person.id = export_template_person.person_id
            ORDER BY person.display_name COLLATE NOCASE ASC
            """
        ).fetchall()
    except sqlite3.Error as exc:
        raise ExportTemplateError("导出模板列表读取失败。") from exc
    finally:
        connection.close()

    persons_by_template: dict[str, list[str]] = {}
    names_by_template: dict[str, list[str]] = {}
    for template_id, person_id, display_name in person_rows:
        tid = str(template_id)
        persons_by_template.setdefault(tid, []).append(str(person_id))
        names_by_template.setdefault(tid, []).append(str(display_name) if display_name else "")

    return [
        ExportTemplateListItem(
            template_id=str(row["template_id"]),
            name=str(row["name"]),
            output_root=str(row["output_root"]),
            status=str(row["status"]),
            created_at=str(row["created_at"]),
            person_count=int(row["person_count"]),
            person_ids=persons_by_template.get(str(row["template_id"]), []),
            person_names=names_by_template.get(str(row["template_id"]), []),
        )
        for row in template_rows
    ]


def create_export_template(
    workspace_context: WorkspaceContext,
    *,
    name: str,
    person_ids: list[str],
    output_root: str,
) -> ExportTemplateCreateResult:
    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")

        normalized_name = name.strip()
        if not normalized_name:
            raise ExportTemplateValidationError("模板名称不能为空。", code="blank_name")

        normalized_person_ids = [pid.strip() for pid in person_ids if pid.strip()]
        if len(normalized_person_ids) < 2:
            raise ExportTemplateValidationError(
                "至少选择 2 个人物。", code="insufficient_persons"
            )

        if len(set(normalized_person_ids)) != len(normalized_person_ids):
            raise ExportTemplateValidationError(
                "不能重复选择同一个人物。", code="duplicate_person"
            )

        output_path = Path(output_root)
        if not output_path.is_absolute():
            raise ExportTemplateValidationError(
                "输出目录必须是绝对路径。", code="relative_path"
            )

        try:
            output_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ExportTemplateValidationError(
                f"无法创建输出目录：{exc}", code="output_dir_creation_failed"
            ) from exc

        placeholders = ", ".join("?" for _ in normalized_person_ids)
        valid_person_rows = connection.execute(
            f"""
            SELECT id, display_name, status
            FROM person
            WHERE id IN ({placeholders})
            """,
            tuple(normalized_person_ids),
        ).fetchall()

        if len(valid_person_rows) != len(normalized_person_ids):
            raise ExportTemplateValidationError(
                "所选人物包含不存在的人物。", code="person_not_found"
            )

        for row in valid_person_rows:
            if str(row["status"]) != "active":
                raise ExportTemplateValidationError(
                    "所选人物包含已失效的人物。", code="inactive_person"
                )
            if row["display_name"] is None:
                raise ExportTemplateValidationError(
                    "所选人物包含未命名的匿名人物。", code="anonymous_person"
                )

        sorted_person_ids = sorted(normalized_person_ids)
        dedup_person_ids_str = ",".join(sorted_person_ids)
        dedup_key = f"{str(output_path.resolve())}:{dedup_person_ids_str}"

        template_id = str(uuid.uuid4())
        now = utc_now_text()

        try:
            connection.execute(
                """
                INSERT INTO export_template (template_id, name, output_root, status, created_at, dedup_key)
                VALUES (?, ?, ?, 'active', ?, ?)
                """,
                (template_id, normalized_name, str(output_path.resolve()), now, dedup_key),
            )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint failed" in str(exc) and "dedup_key" in str(exc):
                connection.rollback()
                raise ExportTemplateValidationError(
                    "相同配置模板已存在。", code="duplicate_template"
                ) from exc
            connection.rollback()
            raise ExportTemplateError("导出模板创建失败。") from exc

        for person_id in sorted_person_ids:
            connection.execute(
                """
                INSERT INTO export_template_person (template_id, person_id)
                VALUES (?, ?)
                """,
                (template_id, person_id),
            )

        connection.commit()
    except ExportTemplateValidationError:
        connection.rollback()
        raise
    except sqlite3.Error as exc:
        connection.rollback()
        raise ExportTemplateError("导出模板创建失败。") from exc
    finally:
        connection.close()

    return ExportTemplateCreateResult(template_id=template_id)


def invalidate_templates_for_person(
    connection: sqlite3.Connection,
    *,
    person_id: str,
) -> None:
    """将包含指定 person_id 的所有 active 模板标记为 invalid。

    应在同一事务中调用，确保级联与触发操作原子提交。
    """
    connection.execute(
        """
        UPDATE export_template
        SET status = 'invalid'
        WHERE status = 'active'
          AND template_id IN (
            SELECT template_id
            FROM export_template_person
            WHERE person_id = ?
          )
        """,
        (person_id,),
    )


def invalidate_templates_for_persons_if_inactive_or_anonymous(
    connection: sqlite3.Connection,
    *,
    person_ids: list[str],
) -> None:
    """检查指定人物列表，若有人变为 inactive 或 display_name 为 NULL，
    则将其关联的 active 模板标记为 invalid。
    """
    if not person_ids:
        return
    placeholders = ", ".join("?" for _ in person_ids)
    rows = connection.execute(
        f"""
        SELECT id
        FROM person
        WHERE id IN ({placeholders})
          AND (status != 'active' OR display_name IS NULL)
        """,
        tuple(person_ids),
    ).fetchall()
    for row in rows:
        invalidate_templates_for_person(connection, person_id=str(row[0]))
