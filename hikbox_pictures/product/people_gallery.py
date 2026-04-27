from __future__ import annotations

from dataclasses import dataclass
from math import ceil
import os
from pathlib import Path
import sqlite3
import time
import uuid

from hikbox_pictures.product.scan_shared import utc_now_text
from hikbox_pictures.product.sources import WorkspaceContext


class PeopleGalleryError(RuntimeError):
    """人物库浏览数据访问失败。"""


REQUIRED_WEBUI_TABLES = (
    "assets",
    "scan_sessions",
    "face_observations",
    "person",
    "person_face_assignments",
    "person_face_exclusions",
    "person_name_events",
    "person_merge_operations",
    "person_merge_operation_assignments",
)
REQUIRED_WEBUI_COLUMNS = {
    "assets": {
        "id",
        "live_photo_mov_path",
    },
    "scan_sessions": {
        "id",
        "status",
    },
    "face_observations": {
        "id",
        "asset_id",
        "context_path",
    },
    "person": {
        "id",
        "display_name",
        "is_named",
        "status",
        "write_revision",
        "created_at",
        "updated_at",
    },
    "person_face_assignments": {
        "id",
        "person_id",
        "face_observation_id",
        "active",
        "updated_at",
    },
    "person_face_exclusions": {
        "id",
        "face_observation_id",
        "excluded_person_id",
        "source_assignment_id",
        "created_at",
    },
    "person_name_events": {
        "id",
        "person_id",
        "event_type",
        "old_display_name",
        "new_display_name",
        "created_at",
    },
    "person_merge_operations": {
        "id",
        "winner_person_id",
        "loser_person_id",
        "winner_display_name_before",
        "winner_is_named_before",
        "winner_status_before",
        "loser_display_name_before",
        "loser_is_named_before",
        "loser_status_before",
        "winner_write_revision_after_merge",
        "loser_write_revision_after_merge",
        "merged_at",
        "undone_at",
    },
    "person_merge_operation_assignments": {
        "id",
        "merge_operation_id",
        "assignment_id",
        "person_role",
    },
}


@dataclass(frozen=True)
class PersonCard:
    person_id: str
    display_label: str
    is_named: bool
    sample_count: int
    cover_assignment_id: int


@dataclass(frozen=True)
class PeopleHomePage:
    named_people: list[PersonCard]
    anonymous_people: list[PersonCard]
    can_undo_latest_merge: bool = False

    @property
    def has_people(self) -> bool:
        return bool(self.named_people or self.anonymous_people)


@dataclass(frozen=True)
class PersonSample:
    assignment_id: int
    face_observation_id: int
    asset_id: int
    context_path: Path
    is_live: bool


@dataclass(frozen=True)
class PersonDetailPage:
    person_id: str
    display_label: str
    current_display_name: str | None
    is_named: bool
    sample_count: int
    current_page: int
    total_pages: int
    page_size: int
    samples: list[PersonSample]

    @property
    def page_numbers(self) -> list[int]:
        return list(range(1, self.total_pages + 1))


@dataclass(frozen=True)
class PersonNameChangeResult:
    outcome: str


@dataclass(frozen=True)
class PersonMergeResult:
    winner_person_id: str
    loser_person_id: str


@dataclass(frozen=True)
class PersonMergeUndoResult:
    merge_operation_id: int
    winner_person_id: str
    loser_person_id: str


@dataclass(frozen=True)
class PersonExclusionResult:
    person_id: str
    remaining_sample_count: int


class PersonNameValidationError(PeopleGalleryError):
    """人物命名校验失败。"""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class PersonMergeValidationError(PeopleGalleryError):
    """人物合并校验失败。"""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class PersonMergeUndoValidationError(PeopleGalleryError):
    """最近一次合并撤销校验失败。"""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class PersonExclusionValidationError(PeopleGalleryError):
    """人物详情页批量排除校验失败。"""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class MergeCandidate:
    person_id: str
    display_name: str | None
    is_named: bool
    status: str
    sample_count: int
    write_revision: int


@dataclass(frozen=True)
class MergeOperationSnapshot:
    merge_operation_id: int
    winner_person_id: str
    loser_person_id: str
    winner_display_name_before: str | None
    winner_is_named_before: bool
    winner_status_before: str
    loser_display_name_before: str | None
    loser_is_named_before: bool
    loser_status_before: str
    winner_write_revision_after_merge: int
    loser_write_revision_after_merge: int
    undone_at: str | None


def ensure_webui_schema_ready(workspace_context: WorkspaceContext) -> None:
    missing_tables = _find_missing_tables(
        db_path=workspace_context.library_db_path,
        required_tables=REQUIRED_WEBUI_TABLES,
    )
    if missing_tables:
        raise PeopleGalleryError(
            "当前工作区缺少 WebUI 依赖的 schema："
            f"{', '.join(missing_tables)}。"
            "该工作区不支持自动升级，请使用当前版本重新执行 hikbox-pictures init。"
        )
    missing_columns = _find_missing_columns(
        db_path=workspace_context.library_db_path,
        required_columns=REQUIRED_WEBUI_COLUMNS,
    )
    if missing_columns:
        raise PeopleGalleryError(
            "当前工作区缺少 WebUI 依赖的 schema 列："
            f"{', '.join(missing_columns)}。"
            "该工作区不支持自动升级，请使用当前版本重新执行 hikbox-pictures init。"
        )


def ensure_no_running_scan(workspace_context: WorkspaceContext) -> None:
    connection = sqlite3.connect(workspace_context.library_db_path)
    try:
        row = connection.execute(
            """
            SELECT id
            FROM scan_sessions
            WHERE status = 'running'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    except sqlite3.Error as exc:
        raise PeopleGalleryError("扫描状态读取失败，无法确认是否允许启动 WebUI。") from exc
    finally:
        connection.close()

    if row is not None:
        raise PeopleGalleryError("当前存在运行中的扫描会话，scan 运行中不能启动 WebUI。")


def load_people_home_page(workspace_context: WorkspaceContext) -> PeopleHomePage:
    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT
              person.id,
              person.display_name,
              person.is_named,
              COUNT(person_face_assignments.id) AS sample_count,
              MIN(person_face_assignments.id) AS cover_assignment_id
            FROM person
            INNER JOIN person_face_assignments
              ON person_face_assignments.person_id = person.id
             AND person_face_assignments.active = 1
            WHERE person.status = 'active'
            GROUP BY person.id, person.display_name, person.is_named, person.created_at
            ORDER BY
              person.is_named DESC,
              sample_count DESC,
              CASE
                WHEN person.display_name IS NULL THEN ''
                ELSE person.display_name
              END COLLATE NOCASE ASC,
              person.created_at ASC,
              person.id ASC
            """
        ).fetchall()
        can_undo_latest_merge = _latest_merge_operation_can_be_undone(connection)
    except sqlite3.Error as exc:
        raise PeopleGalleryError("人物首页数据读取失败。") from exc
    finally:
        connection.close()

    named_people: list[PersonCard] = []
    anonymous_people: list[PersonCard] = []
    for row in rows:
        person_id = str(row["id"])
        is_named = bool(row["is_named"])
        card = PersonCard(
            person_id=person_id,
            display_label=(
                str(row["display_name"])
                if is_named and row["display_name"] is not None
                else build_anonymous_label(person_id)
            ),
            is_named=is_named,
            sample_count=int(row["sample_count"]),
            cover_assignment_id=int(row["cover_assignment_id"]),
        )
        if is_named:
            named_people.append(card)
        else:
            anonymous_people.append(card)
    return PeopleHomePage(
        named_people=named_people,
        anonymous_people=anonymous_people,
        can_undo_latest_merge=can_undo_latest_merge,
    )


def load_person_detail_page(
    workspace_context: WorkspaceContext,
    *,
    person_id: str,
    page: int,
    page_size: int,
) -> PersonDetailPage | None:
    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        header_row = connection.execute(
            """
            SELECT
              person.id,
              person.display_name,
              person.is_named,
              person.status,
              person.write_revision,
              COUNT(person_face_assignments.id) AS sample_count
            FROM person
            LEFT JOIN person_face_assignments
              ON person_face_assignments.person_id = person.id
             AND person_face_assignments.active = 1
            WHERE person.id = ?
            GROUP BY person.id, person.display_name, person.is_named, person.status
            """,
            (person_id,),
        ).fetchone()
        if header_row is None or str(header_row["status"]) != "active":
            return None

        sample_count = int(header_row["sample_count"])
        if sample_count < 1:
            return None

        total_pages = int(ceil(sample_count / page_size))
        if page > total_pages:
            return None

        offset = (page - 1) * page_size
        sample_rows = connection.execute(
            """
            SELECT
              person_face_assignments.id,
              face_observations.id AS face_observation_id,
              assets.id AS asset_id,
              face_observations.context_path,
              assets.live_photo_mov_path
            FROM person_face_assignments
            INNER JOIN face_observations
              ON face_observations.id = person_face_assignments.face_observation_id
            INNER JOIN assets
              ON assets.id = face_observations.asset_id
            WHERE person_face_assignments.person_id = ?
              AND person_face_assignments.active = 1
            ORDER BY person_face_assignments.id ASC
            LIMIT ? OFFSET ?
            """,
            (person_id, page_size, offset),
        ).fetchall()
    except sqlite3.Error as exc:
        raise PeopleGalleryError(f"人物详情读取失败：{person_id}") from exc
    finally:
        connection.close()

    return PersonDetailPage(
        person_id=person_id,
        display_label=(
            str(header_row["display_name"])
            if bool(header_row["is_named"]) and header_row["display_name"] is not None
            else build_anonymous_label(person_id)
        ),
        current_display_name=(
            str(header_row["display_name"])
            if bool(header_row["is_named"]) and header_row["display_name"] is not None
            else None
        ),
        is_named=bool(header_row["is_named"]),
        sample_count=sample_count,
        current_page=page,
        total_pages=total_pages,
        page_size=page_size,
        samples=[
            PersonSample(
                assignment_id=int(row["id"]),
                face_observation_id=int(row["face_observation_id"]),
                asset_id=int(row["asset_id"]),
                context_path=Path(str(row["context_path"])),
                is_live=bool(row["live_photo_mov_path"]),
            )
            for row in sample_rows
        ],
    )


def load_assignment_context_path(
    workspace_context: WorkspaceContext,
    *,
    assignment_id: int,
) -> Path | None:
    connection = sqlite3.connect(workspace_context.library_db_path)
    try:
        row = connection.execute(
            """
            SELECT face_observations.context_path
            FROM person_face_assignments
            INNER JOIN face_observations
              ON face_observations.id = person_face_assignments.face_observation_id
            INNER JOIN person
              ON person.id = person_face_assignments.person_id
            WHERE person_face_assignments.id = ?
              AND person_face_assignments.active = 1
              AND person.status = 'active'
            """,
            (assignment_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise PeopleGalleryError(f"人物样本图片读取失败：assignment_id={assignment_id}") from exc
    finally:
        connection.close()

    if row is None:
        return None
    return Path(str(row[0]))


def submit_person_name(
    workspace_context: WorkspaceContext,
    *,
    person_id: str,
    display_name: str,
) -> PersonNameChangeResult:
    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        person_row = connection.execute(
            """
            SELECT
              person.id,
              person.display_name,
              person.is_named,
              person.status,
              person.write_revision,
              COUNT(person_face_assignments.id) AS sample_count
            FROM person
            LEFT JOIN person_face_assignments
              ON person_face_assignments.person_id = person.id
             AND person_face_assignments.active = 1
            WHERE person.id = ?
            GROUP BY person.id, person.display_name, person.is_named, person.status
            """,
            (person_id,),
        ).fetchone()
        if person_row is None or str(person_row["status"]) != "active" or int(person_row["sample_count"]) < 1:
            raise PersonNameValidationError(
                f"未找到 person_id={person_id} 对应的人物。",
                code="person_not_found",
            )

        normalized_name = display_name.strip()
        if not normalized_name:
            raise PersonNameValidationError("名称不能为空。", code="blank_name")

        current_name = (
            str(person_row["display_name"])
            if bool(person_row["is_named"]) and person_row["display_name"] is not None
            else None
        )
        if current_name == normalized_name:
            connection.commit()
            return PersonNameChangeResult(outcome="noop")

        duplicate_row = connection.execute(
            """
            SELECT id
            FROM person
            WHERE id != ?
              AND status = 'active'
              AND is_named = 1
              AND display_name = ?
            LIMIT 1
            """,
            (person_id, normalized_name),
        ).fetchone()
        if duplicate_row is not None:
            raise PersonNameValidationError("名称已存在，请使用其他名称。", code="duplicate_name")

        now = utc_now_text()
        connection.execute(
            """
            UPDATE person
            SET display_name = ?,
                is_named = 1,
                write_revision = write_revision + 1,
                updated_at = ?
            WHERE id = ?
            """,
            (normalized_name, now, person_id),
        )
        connection.execute(
            """
            INSERT INTO person_name_events (
              person_id,
              event_type,
              old_display_name,
              new_display_name,
              created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                person_id,
                "person_renamed" if current_name is not None else "person_named",
                current_name,
                normalized_name,
                now,
            ),
        )
        connection.commit()
    except PersonNameValidationError:
        connection.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise PersonNameValidationError("名称已存在，请使用其他名称。", code="duplicate_name") from exc
    except sqlite3.Error as exc:
        connection.rollback()
        raise PeopleGalleryError(f"人物命名写入失败：{person_id}") from exc
    finally:
        connection.close()

    if current_name is None:
        return PersonNameChangeResult(outcome="named")
    return PersonNameChangeResult(outcome="renamed")


def submit_people_merge(
    workspace_context: WorkspaceContext,
    *,
    person_ids: list[str],
) -> PersonMergeResult:
    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        normalized_person_ids = [person_id.strip() for person_id in person_ids if person_id.strip()]
        if len(normalized_person_ids) != 2:
            raise PersonMergeValidationError("必须恰好选择 2 个人物。", code="invalid_count")
        if normalized_person_ids[0] == normalized_person_ids[1]:
            raise PersonMergeValidationError("不能重复选择同一个人物。", code="duplicate_person")

        person_rows = connection.execute(
            """
            SELECT
              person.id,
              person.display_name,
              person.is_named,
              person.status,
              person.write_revision,
              COUNT(person_face_assignments.id) AS sample_count
            FROM person
            LEFT JOIN person_face_assignments
              ON person_face_assignments.person_id = person.id
             AND person_face_assignments.active = 1
            WHERE person.id IN (?, ?)
            GROUP BY person.id, person.display_name, person.is_named, person.status, person.write_revision
            """,
            (normalized_person_ids[0], normalized_person_ids[1]),
        ).fetchall()
        if len(person_rows) != 2:
            raise PersonMergeValidationError("未找到可合并的人物。", code="person_not_found")

        candidates_by_id = {
            str(row["id"]): MergeCandidate(
                person_id=str(row["id"]),
                display_name=None if row["display_name"] is None else str(row["display_name"]),
                is_named=bool(row["is_named"]),
                status=str(row["status"]),
                sample_count=int(row["sample_count"]),
                write_revision=int(row["write_revision"]),
            )
            for row in person_rows
        }
        candidates = [candidates_by_id[person_id] for person_id in normalized_person_ids]
        for candidate in candidates:
            if candidate.status != "active":
                raise PersonMergeValidationError("不能合并已失效的人物。", code="inactive_person")
            if candidate.sample_count < 1:
                raise PersonMergeValidationError("未找到可合并的人物。", code="person_not_found")

        winner, loser = _pick_merge_winner(candidates)
        winner_assignment_ids_before = _load_active_assignment_ids_for_update(connection, winner.person_id)
        loser_assignment_ids_before = _load_active_assignment_ids_for_update(connection, loser.person_id)
        now = utc_now_text()
        winner_write_revision_after_merge = winner.write_revision + 1
        loser_write_revision_after_merge = loser.write_revision + 1

        connection.execute(
            """
            UPDATE person_face_assignments
            SET person_id = ?,
                updated_at = ?
            WHERE person_id = ?
              AND active = 1
            """,
            (winner.person_id, now, loser.person_id),
        )
        _maybe_inject_merge_failure("after_assignment_migration")

        connection.execute(
            """
            UPDATE person
            SET write_revision = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (winner_write_revision_after_merge, now, winner.person_id),
        )
        connection.execute(
            """
            UPDATE person
            SET status = 'inactive',
                write_revision = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (loser_write_revision_after_merge, now, loser.person_id),
        )
        _maybe_inject_merge_failure("after_loser_inactivation")

        cursor = connection.execute(
            """
            INSERT INTO person_merge_operations (
              winner_person_id,
              loser_person_id,
              winner_display_name_before,
              winner_is_named_before,
              winner_status_before,
              loser_display_name_before,
              loser_is_named_before,
              loser_status_before,
              winner_write_revision_after_merge,
              loser_write_revision_after_merge,
              merged_at,
              undone_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                winner.person_id,
                loser.person_id,
                winner.display_name,
                int(winner.is_named),
                winner.status,
                loser.display_name,
                int(loser.is_named),
                loser.status,
                winner_write_revision_after_merge,
                loser_write_revision_after_merge,
                now,
            ),
        )
        merge_operation_id = int(cursor.lastrowid)
        _maybe_inject_merge_failure("after_merge_operation_insert")

        for assignment_id in winner_assignment_ids_before:
            connection.execute(
                """
                INSERT INTO person_merge_operation_assignments (
                  merge_operation_id,
                  assignment_id,
                  person_role
                )
                VALUES (?, ?, 'winner')
                """,
                (merge_operation_id, assignment_id),
            )
        for assignment_id in loser_assignment_ids_before:
            connection.execute(
                """
                INSERT INTO person_merge_operation_assignments (
                  merge_operation_id,
                  assignment_id,
                  person_role
                )
                VALUES (?, ?, 'loser')
                """,
                (merge_operation_id, assignment_id),
            )
        _maybe_inject_merge_failure("after_merge_operation_assignments")
        connection.commit()
    except PersonMergeValidationError:
        connection.rollback()
        raise
    except sqlite3.Error as exc:
        connection.rollback()
        raise PeopleGalleryError("人物合并失败，请稍后重试。") from exc
    except RuntimeError as exc:
        connection.rollback()
        raise PeopleGalleryError("人物合并失败，请稍后重试。") from exc
    finally:
        connection.close()

    return PersonMergeResult(
        winner_person_id=winner.person_id,
        loser_person_id=loser.person_id,
    )


def submit_person_exclusions(
    workspace_context: WorkspaceContext,
    *,
    person_id: str,
    assignment_ids: list[str],
) -> PersonExclusionResult:
    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        person_row = connection.execute(
            """
            SELECT
              person.id,
              person.status,
              person.write_revision,
              COUNT(person_face_assignments.id) AS sample_count
            FROM person
            LEFT JOIN person_face_assignments
              ON person_face_assignments.person_id = person.id
             AND person_face_assignments.active = 1
            WHERE person.id = ?
            GROUP BY person.id, person.status, person.write_revision
            """,
            (person_id,),
        ).fetchone()
        if person_row is None or str(person_row["status"]) != "active" or int(person_row["sample_count"]) < 1:
            raise PersonExclusionValidationError(
                f"未找到 person_id={person_id} 对应的人物。",
                code="person_not_found",
            )

        normalized_assignment_ids: list[int] = []
        for raw_assignment_id in assignment_ids:
            normalized = raw_assignment_id.strip()
            if not normalized:
                continue
            try:
                normalized_assignment_ids.append(int(normalized))
            except ValueError as exc:
                raise PersonExclusionValidationError(
                    "选择的样本无效，请刷新页面后重试。",
                    code="invalid_assignment_id",
                ) from exc
        if not normalized_assignment_ids:
            raise PersonExclusionValidationError(
                "至少选择 1 条样本后才能批量排除。",
                code="empty_selection",
            )
        if len(set(normalized_assignment_ids)) != len(normalized_assignment_ids):
            raise PersonExclusionValidationError(
                "同一次请求中不能重复选择同一个样本。",
                code="duplicate_assignment",
            )

        placeholders = ", ".join("?" for _ in normalized_assignment_ids)
        assignment_rows = connection.execute(
            f"""
            SELECT
              id,
              person_id,
              face_observation_id,
              active
            FROM person_face_assignments
            WHERE id IN ({placeholders})
            ORDER BY id ASC
            """,
            tuple(normalized_assignment_ids),
        ).fetchall()
        if len(assignment_rows) != len(normalized_assignment_ids):
            raise PersonExclusionValidationError(
                "未找到所选样本，无法批量排除。",
                code="assignment_not_found",
            )

        selected_face_ids: list[int] = []
        for row in assignment_rows:
            if str(row["person_id"]) != person_id:
                raise PersonExclusionValidationError(
                    "选择的样本不属于当前人物，无法批量排除。",
                    code="assignment_wrong_person",
                )
            if not bool(row["active"]):
                raise PersonExclusionValidationError(
                    "选择的样本已不是 active 样本，无法批量排除。",
                    code="assignment_inactive",
                )
            selected_face_ids.append(int(row["face_observation_id"]))

        duplicate_exclusions = connection.execute(
            f"""
            SELECT face_observation_id
            FROM person_face_exclusions
            WHERE excluded_person_id = ?
              AND face_observation_id IN ({placeholders})
            ORDER BY face_observation_id ASC
            """,
            (person_id, *selected_face_ids),
        ).fetchall()
        if duplicate_exclusions:
            raise PersonExclusionValidationError(
                "选择的样本已经排除过，不能重复提交。",
                code="duplicate_exclusion",
            )

        now = utc_now_text()
        for row_index, row in enumerate(assignment_rows):
            assignment_id = int(row["id"])
            face_observation_id = int(row["face_observation_id"])
            connection.execute(
                """
                INSERT INTO person_face_exclusions (
                  face_observation_id,
                  excluded_person_id,
                  source_assignment_id,
                  created_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (face_observation_id, person_id, assignment_id, now),
            )
            deactivate_cursor = connection.execute(
                """
                UPDATE person_face_assignments
                SET active = 0,
                    updated_at = ?
                WHERE id = ?
                  AND person_id = ?
                  AND active = 1
                """,
                (now, assignment_id, person_id),
            )
            if deactivate_cursor.rowcount != 1:
                raise PeopleGalleryError("批量排除失败，请稍后重试。")
            _maybe_inject_exclusion_failure(
                "after_first_exclusion_insert",
                row_index=row_index,
            )

        remaining_sample_count = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM person_face_assignments
                WHERE person_id = ?
                  AND active = 1
                """,
                (person_id,),
            ).fetchone()[0]
        )
        if remaining_sample_count > 0:
            connection.execute(
                """
                UPDATE person
                SET write_revision = write_revision + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, person_id),
            )
        else:
            connection.execute(
                """
                UPDATE person
                SET status = 'inactive',
                    write_revision = write_revision + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, person_id),
            )
        connection.commit()
    except PersonExclusionValidationError:
        connection.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        if "person_face_exclusions.face_observation_id, person_face_exclusions.excluded_person_id" in str(exc):
            raise PersonExclusionValidationError(
                "选择的样本已经排除过，不能重复提交。",
                code="duplicate_exclusion",
            ) from exc
        raise PeopleGalleryError("批量排除失败，请稍后重试。") from exc
    except sqlite3.Error as exc:
        connection.rollback()
        raise PeopleGalleryError("批量排除失败，请稍后重试。") from exc
    except RuntimeError as exc:
        connection.rollback()
        raise PeopleGalleryError("批量排除失败，请稍后重试。") from exc
    finally:
        connection.close()

    return PersonExclusionResult(
        person_id=person_id,
        remaining_sample_count=remaining_sample_count,
    )


def submit_people_merge_undo(
    workspace_context: WorkspaceContext,
) -> PersonMergeUndoResult:
    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    trace_request_id = uuid.uuid4().hex
    _record_undo_trace("handler_enter", request_id=trace_request_id)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _record_undo_trace("transaction_acquired", request_id=trace_request_id)
        merge_operation = _load_latest_merge_operation(connection)
        if merge_operation is None:
            raise PersonMergeUndoValidationError(
                "当前没有可撤销的最近一次合并。",
                code="no_merge_operation",
            )
        if merge_operation.undone_at is not None:
            raise PersonMergeUndoValidationError(
                "最近一次成功合并已经撤销。",
                code="already_undone",
            )
        _maybe_break_latest_merge_snapshot_for_testing(
            connection,
            merge_operation_id=merge_operation.merge_operation_id,
        )

        winner_assignment_ids = _load_merge_operation_assignment_ids(
            connection,
            merge_operation_id=merge_operation.merge_operation_id,
            person_role="winner",
        )
        loser_assignment_ids = _load_merge_operation_assignment_ids(
            connection,
            merge_operation_id=merge_operation.merge_operation_id,
            person_role="loser",
        )
        if not winner_assignment_ids or not loser_assignment_ids:
            raise PersonMergeUndoValidationError(
                "最近一次合并快照不完整，无法撤销。",
                code="snapshot_incomplete",
            )

        current_revisions = _load_person_write_revisions(
            connection,
            person_ids=[merge_operation.winner_person_id, merge_operation.loser_person_id],
        )
        if len(current_revisions) != 2:
            raise PersonMergeUndoValidationError(
                "最近一次合并快照不完整，无法撤销。",
                code="snapshot_incomplete",
            )
        if (
            current_revisions[merge_operation.winner_person_id]
            != merge_operation.winner_write_revision_after_merge
            or current_revisions[merge_operation.loser_person_id]
            != merge_operation.loser_write_revision_after_merge
        ):
            raise PersonMergeUndoValidationError(
                "最近一次合并之后已发生新的人物相关写入，无法撤销。",
                code="merge_changed_afterwards",
            )
        if not _merge_snapshot_matches_current_state(
            connection,
            merge_operation=merge_operation,
            winner_assignment_ids=winner_assignment_ids,
            loser_assignment_ids=loser_assignment_ids,
        ):
            raise PersonMergeUndoValidationError(
                "最近一次合并快照不完整，无法撤销。",
                code="snapshot_incomplete",
            )

        now = utc_now_text()
        _maybe_hold_undo_transaction()
        loser_placeholders = ", ".join("?" for _ in loser_assignment_ids)
        loser_assignment_cursor = connection.execute(
            f"""
            UPDATE person_face_assignments
            SET person_id = ?,
                updated_at = ?
            WHERE id IN ({loser_placeholders})
              AND active = 1
              AND person_id = ?
            """,
            (
                merge_operation.loser_person_id,
                now,
                *loser_assignment_ids,
                merge_operation.winner_person_id,
            ),
        )
        if loser_assignment_cursor.rowcount != len(loser_assignment_ids):
            raise PeopleGalleryError("撤销最近一次合并失败，请稍后重试。")
        _maybe_inject_undo_failure("after_assignment_restore")

        winner_write_revision_after_undo = current_revisions[merge_operation.winner_person_id] + 1
        loser_write_revision_after_undo = current_revisions[merge_operation.loser_person_id] + 1
        connection.execute(
            """
            UPDATE person
            SET display_name = ?,
                is_named = ?,
                status = ?,
                write_revision = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                merge_operation.winner_display_name_before,
                int(merge_operation.winner_is_named_before),
                merge_operation.winner_status_before,
                winner_write_revision_after_undo,
                now,
                merge_operation.winner_person_id,
            ),
        )
        connection.execute(
            """
            UPDATE person
            SET display_name = ?,
                is_named = ?,
                status = ?,
                write_revision = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                merge_operation.loser_display_name_before,
                int(merge_operation.loser_is_named_before),
                merge_operation.loser_status_before,
                loser_write_revision_after_undo,
                now,
                merge_operation.loser_person_id,
            ),
        )
        _maybe_inject_undo_failure("after_person_restore")

        undo_cursor = connection.execute(
            """
            UPDATE person_merge_operations
            SET undone_at = ?
            WHERE id = ?
              AND undone_at IS NULL
            """,
            (now, merge_operation.merge_operation_id),
        )
        if undo_cursor.rowcount != 1:
            raise PersonMergeUndoValidationError(
                "最近一次成功合并已经撤销。",
                code="already_undone",
            )
        _maybe_inject_undo_failure("after_merge_operation_mark_undone")
        connection.commit()
        _record_undo_trace("request_succeeded", request_id=trace_request_id)
    except PersonMergeUndoValidationError:
        connection.rollback()
        _record_undo_trace("validation_failed", request_id=trace_request_id)
        raise
    except sqlite3.Error as exc:
        connection.rollback()
        _record_undo_trace("request_failed", request_id=trace_request_id)
        raise PeopleGalleryError("撤销最近一次合并失败，请稍后重试。") from exc
    except RuntimeError as exc:
        connection.rollback()
        _record_undo_trace("request_failed", request_id=trace_request_id)
        raise PeopleGalleryError("撤销最近一次合并失败，请稍后重试。") from exc
    finally:
        connection.close()

    return PersonMergeUndoResult(
        merge_operation_id=merge_operation.merge_operation_id,
        winner_person_id=merge_operation.winner_person_id,
        loser_person_id=merge_operation.loser_person_id,
    )


def _latest_merge_operation_can_be_undone(connection: sqlite3.Connection) -> bool:
    merge_operation = _load_latest_merge_operation(connection)
    if merge_operation is None or merge_operation.undone_at is not None:
        return False
    winner_assignment_ids = _load_merge_operation_assignment_ids(
        connection,
        merge_operation_id=merge_operation.merge_operation_id,
        person_role="winner",
    )
    loser_assignment_ids = _load_merge_operation_assignment_ids(
        connection,
        merge_operation_id=merge_operation.merge_operation_id,
        person_role="loser",
    )
    if not winner_assignment_ids or not loser_assignment_ids:
        return False
    current_revisions = _load_person_write_revisions(
        connection,
        person_ids=[merge_operation.winner_person_id, merge_operation.loser_person_id],
    )
    revisions_match = (
        len(current_revisions) == 2
        and current_revisions[merge_operation.winner_person_id]
        == merge_operation.winner_write_revision_after_merge
        and current_revisions[merge_operation.loser_person_id]
        == merge_operation.loser_write_revision_after_merge
    )
    if not revisions_match:
        return False
    return _merge_snapshot_matches_current_state(
        connection,
        merge_operation=merge_operation,
        winner_assignment_ids=winner_assignment_ids,
        loser_assignment_ids=loser_assignment_ids,
    )


def _merge_snapshot_matches_current_state(
    connection: sqlite3.Connection,
    *,
    merge_operation: MergeOperationSnapshot,
    winner_assignment_ids: list[int],
    loser_assignment_ids: list[int],
) -> bool:
    winner_assignment_set = set(winner_assignment_ids)
    loser_assignment_set = set(loser_assignment_ids)
    if (
        len(winner_assignment_set) != len(winner_assignment_ids)
        or len(loser_assignment_set) != len(loser_assignment_ids)
        or winner_assignment_set & loser_assignment_set
    ):
        return False
    current_assignment_rows = connection.execute(
        """
        SELECT id, person_id
        FROM person_face_assignments
        WHERE active = 1
          AND person_id IN (?, ?)
        ORDER BY id ASC
        """,
        (merge_operation.winner_person_id, merge_operation.loser_person_id),
    ).fetchall()
    current_winner_assignment_set = {
        int(row["id"])
        for row in current_assignment_rows
        if str(row["person_id"]) == merge_operation.winner_person_id
    }
    current_loser_assignment_set = {
        int(row["id"])
        for row in current_assignment_rows
        if str(row["person_id"]) == merge_operation.loser_person_id
    }
    if current_loser_assignment_set:
        return False
    if current_winner_assignment_set != winner_assignment_set | loser_assignment_set:
        return False
    person_rows = connection.execute(
        """
        SELECT id, status
        FROM person
        WHERE id IN (?, ?)
        """,
        (merge_operation.winner_person_id, merge_operation.loser_person_id),
    ).fetchall()
    person_status_by_id = {str(row["id"]): str(row["status"]) for row in person_rows}
    return (
        person_status_by_id.get(merge_operation.winner_person_id) == "active"
        and person_status_by_id.get(merge_operation.loser_person_id) == "inactive"
    )


def _load_latest_merge_operation(connection: sqlite3.Connection) -> MergeOperationSnapshot | None:
    row = connection.execute(
        """
        SELECT
          id,
          winner_person_id,
          loser_person_id,
          winner_display_name_before,
          winner_is_named_before,
          winner_status_before,
          loser_display_name_before,
          loser_is_named_before,
          loser_status_before,
          winner_write_revision_after_merge,
          loser_write_revision_after_merge,
          undone_at
        FROM person_merge_operations
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return MergeOperationSnapshot(
        merge_operation_id=int(row["id"]),
        winner_person_id=str(row["winner_person_id"]),
        loser_person_id=str(row["loser_person_id"]),
        winner_display_name_before=(
            None if row["winner_display_name_before"] is None else str(row["winner_display_name_before"])
        ),
        winner_is_named_before=bool(row["winner_is_named_before"]),
        winner_status_before=str(row["winner_status_before"]),
        loser_display_name_before=(
            None if row["loser_display_name_before"] is None else str(row["loser_display_name_before"])
        ),
        loser_is_named_before=bool(row["loser_is_named_before"]),
        loser_status_before=str(row["loser_status_before"]),
        winner_write_revision_after_merge=int(row["winner_write_revision_after_merge"]),
        loser_write_revision_after_merge=int(row["loser_write_revision_after_merge"]),
        undone_at=None if row["undone_at"] is None else str(row["undone_at"]),
    )


def _load_merge_operation_assignment_ids(
    connection: sqlite3.Connection,
    *,
    merge_operation_id: int,
    person_role: str,
) -> list[int]:
    return [
        int(row[0])
        for row in connection.execute(
            """
            SELECT assignment_id
            FROM person_merge_operation_assignments
            WHERE merge_operation_id = ?
              AND person_role = ?
            ORDER BY assignment_id ASC
            """,
            (merge_operation_id, person_role),
        ).fetchall()
    ]


def _load_person_write_revisions(
    connection: sqlite3.Connection,
    *,
    person_ids: list[str],
) -> dict[str, int]:
    if not person_ids:
        return {}
    placeholders = ", ".join("?" for _ in person_ids)
    rows = connection.execute(
        f"""
        SELECT id, write_revision
        FROM person
        WHERE id IN ({placeholders})
        """,
        person_ids,
    ).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def build_anonymous_label(person_id: str) -> str:
    normalized = person_id.replace("-", "")
    return f"匿名人物 #{normalized[:8]}"


def _pick_merge_winner(candidates: list[MergeCandidate]) -> tuple[MergeCandidate, MergeCandidate]:
    first, second = candidates
    if first.is_named and second.is_named:
        raise PersonMergeValidationError("不支持合并两个已命名人物。", code="both_named")
    if first.is_named and not second.is_named:
        return first, second
    if second.is_named and not first.is_named:
        return second, first
    if first.sample_count > second.sample_count:
        return first, second
    if second.sample_count > first.sample_count:
        return second, first
    if first.person_id <= second.person_id:
        return first, second
    return second, first


def _load_active_assignment_ids_for_update(connection: sqlite3.Connection, person_id: str) -> list[int]:
    return [
        int(row[0])
        for row in connection.execute(
            """
            SELECT id
            FROM person_face_assignments
            WHERE person_id = ?
              AND active = 1
            ORDER BY id ASC
            """,
            (person_id,),
        ).fetchall()
    ]


def _maybe_inject_merge_failure(stage: str) -> None:
    # 仅用于自动化验证事务回滚；未设置环境变量时不会影响正常逻辑。
    if os.environ.get("HIKBOX_TEST_MERGE_FAIL_STAGE") == stage:
        raise RuntimeError(f"merge fault injected at stage={stage}")


def _maybe_inject_exclusion_failure(stage: str, *, row_index: int) -> None:
    # 仅用于自动化验证事务回滚；未设置环境变量时不会影响正常逻辑。
    if row_index != 0:
        return
    if os.environ.get("HIKBOX_TEST_EXCLUSION_FAIL_STAGE") == stage:
        raise RuntimeError(f"exclusion fault injected at stage={stage}")


def _maybe_hold_undo_transaction() -> None:
    hold_seconds = os.environ.get("HIKBOX_TEST_UNDO_HOLD_SECONDS")
    if hold_seconds is None:
        return
    time.sleep(float(hold_seconds))


def _maybe_inject_undo_failure(stage: str) -> None:
    # 仅用于自动化验证事务回滚；未设置环境变量时不会影响正常逻辑。
    if os.environ.get("HIKBOX_TEST_UNDO_FAIL_STAGE") == stage:
        raise RuntimeError(f"undo fault injected at stage={stage}")


def _maybe_break_latest_merge_snapshot_for_testing(
    connection: sqlite3.Connection,
    *,
    merge_operation_id: int,
) -> None:
    if os.environ.get("HIKBOX_TEST_BREAK_LATEST_MERGE_SNAPSHOT") != "1":
        return
    connection.execute(
        """
        DELETE FROM person_merge_operation_assignments
        WHERE id = (
          SELECT id
          FROM person_merge_operation_assignments
          WHERE merge_operation_id = ?
            AND person_role = 'loser'
          ORDER BY id DESC
          LIMIT 1
        )
        """,
        (merge_operation_id,),
    )


def _record_undo_trace(event: str, *, request_id: str) -> None:
    trace_path_text = os.environ.get("HIKBOX_TEST_UNDO_TRACE_FILE")
    if trace_path_text is None:
        return
    trace_path = Path(trace_path_text)
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    with trace_path.open("a", encoding="utf-8") as trace_file:
        trace_file.write(f"{time.time_ns()} {request_id} {event}\n")


def _find_missing_tables(*, db_path: Path, required_tables: tuple[str, ...]) -> list[str]:
    connection = sqlite3.connect(db_path)
    try:
        existing_tables = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                """
            ).fetchall()
        }
    except sqlite3.Error as exc:
        raise PeopleGalleryError(f"工作区 schema 检查失败：{db_path}") from exc
    finally:
        connection.close()
    return [table_name for table_name in required_tables if table_name not in existing_tables]


def _find_missing_columns(
    *,
    db_path: Path,
    required_columns: dict[str, set[str]],
) -> list[str]:
    connection = sqlite3.connect(db_path)
    try:
        missing: list[str] = []
        for table_name, columns in required_columns.items():
            try:
                existing_columns = {
                    str(row[1])
                    for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
                }
            except sqlite3.Error as exc:
                raise PeopleGalleryError(f"工作区 schema 列检查失败：{db_path}:{table_name}") from exc
            for column_name in sorted(columns):
                if column_name not in existing_columns:
                    missing.append(f"{table_name}.{column_name}")
        return missing
    finally:
        connection.close()
