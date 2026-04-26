from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from pathlib import Path
import sqlite3

from hikbox_pictures.product.sources import WorkspaceContext


class PeopleGalleryError(RuntimeError):
    """人物库浏览数据访问失败。"""


REQUIRED_WEBUI_TABLES = (
    "assets",
    "scan_sessions",
    "face_observations",
    "person",
    "person_face_assignments",
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
        "created_at",
    },
    "person_face_assignments": {
        "id",
        "person_id",
        "face_observation_id",
        "active",
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

    @property
    def has_people(self) -> bool:
        return bool(self.named_people or self.anonymous_people)


@dataclass(frozen=True)
class PersonSample:
    assignment_id: int
    asset_id: int
    context_path: Path
    is_live: bool


@dataclass(frozen=True)
class PersonDetailPage:
    person_id: str
    display_label: str
    is_named: bool
    sample_count: int
    current_page: int
    total_pages: int
    page_size: int
    samples: list[PersonSample]

    @property
    def page_numbers(self) -> list[int]:
        return list(range(1, self.total_pages + 1))


def ensure_webui_schema_ready(workspace_context: WorkspaceContext) -> None:
    missing_tables = _find_missing_tables(
        db_path=workspace_context.library_db_path,
        required_tables=REQUIRED_WEBUI_TABLES,
    )
    if missing_tables:
        raise PeopleGalleryError(
            "当前工作区缺少 WebUI 依赖的 schema："
            f"{', '.join(missing_tables)}。"
            "该工作区不支持自动升级，请使用当前版本重新执行 hikbox init。"
        )
    missing_columns = _find_missing_columns(
        db_path=workspace_context.library_db_path,
        required_columns=REQUIRED_WEBUI_COLUMNS,
    )
    if missing_columns:
        raise PeopleGalleryError(
            "当前工作区缺少 WebUI 依赖的 schema 列："
            f"{', '.join(missing_columns)}。"
            "该工作区不支持自动升级，请使用当前版本重新执行 hikbox init。"
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
              CASE
                WHEN person.display_name IS NULL THEN ''
                ELSE person.display_name
              END COLLATE NOCASE ASC,
              person.created_at ASC,
              person.id ASC
            """
        ).fetchall()
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
    return PeopleHomePage(named_people=named_people, anonymous_people=anonymous_people)


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
        is_named=bool(header_row["is_named"]),
        sample_count=sample_count,
        current_page=page,
        total_pages=total_pages,
        page_size=page_size,
        samples=[
            PersonSample(
                assignment_id=int(row["id"]),
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


def build_anonymous_label(person_id: str) -> str:
    normalized = person_id.replace("-", "")
    return f"匿名人物 #{normalized[:8]}"


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
