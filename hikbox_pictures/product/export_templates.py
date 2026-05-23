from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import math
import os
import logging
import shutil
import sqlite3
import subprocess
import threading
import uuid

from PIL import ExifTags
from PIL import Image
from PIL import ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:
    pass

from hikbox_pictures.product.scan_shared import utc_now_text
from hikbox_pictures.product.sources import WorkspaceContext


class ExportTemplateError(RuntimeError):
    """导出模板数据访问失败。"""


class ExportTemplateValidationError(ExportTemplateError):
    """导出模板校验失败。"""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


# Test-only hook called after each file copy during export.
_per_file_copy_hook: callable | None = None
EXPORT_LIVE_PHOTO_SWIFT_PATH = Path(__file__).resolve().with_name("export_live_photo.swift")


def set_per_file_copy_hook(hook: callable | None) -> None:
    global _per_file_copy_hook
    _per_file_copy_hook = hook


def _ensure_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"找不到依赖工具：{name}")


def run_live_photo_export_helper(
    *,
    still_src: Path,
    mov_src: Path,
    still_dst: Path,
    mov_dst: Path,
) -> None:
    _ensure_tool("swift")
    if not EXPORT_LIVE_PHOTO_SWIFT_PATH.is_file():
        raise FileNotFoundError(f"找不到 Live Photo 导出 helper：{EXPORT_LIVE_PHOTO_SWIFT_PATH}")
    subprocess.run(
        [
            "swift",
            "-suppress-warnings",
            str(EXPORT_LIVE_PHOTO_SWIFT_PATH),
            str(still_src),
            str(mov_src),
            str(still_dst),
            str(mov_dst),
        ],
        check=True,
    )


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


@dataclass(frozen=True)
class ExportTemplateDetail:
    template_id: str
    name: str
    output_root: str
    status: str
    created_at: str
    person_ids: list[str]


@dataclass(frozen=True)
class PreviewAsset:
    asset_id: int
    file_name: str
    capture_month: str
    context_url: str
    representative_person_id: str
    is_live: bool


@dataclass(frozen=True)
class PreviewMonthBucket:
    month: str
    total_count: int
    only_assets: list[PreviewAsset]
    group_assets: list[PreviewAsset]


@dataclass(frozen=True)
class PreviewResult:
    total_count: int
    only_count: int
    group_count: int
    month_buckets: list[PreviewMonthBucket]


@dataclass(frozen=True)
class BurstPickAsset:
    asset_id: int
    file_name: str
    bucket: str
    month: str
    context_url: str
    is_live: bool


@dataclass(frozen=True)
class BurstPickEdge:
    asset_ids: tuple[int, int]
    threshold: str
    metadata_assisted: bool
    dhash_hamming: int
    luminance_cosine: float
    color_histogram_intersection: float
    capture_time_delta_seconds: float | None
    normalized_device_match: bool | None


@dataclass(frozen=True)
class BurstPickGroup:
    group_key: str
    assets: list[BurstPickAsset]
    edges: list[BurstPickEdge]


@dataclass(frozen=True)
class BurstPickResult:
    template_id: str
    groups: list[BurstPickGroup]
    skipped_missing_or_unreadable_count: int


@dataclass(frozen=True)
class BurstPickSubmitResult:
    abandoned_asset_ids: list[int]
    kept_asset_ids: list[int]
    created_count: int
    already_abandoned_count: int


@dataclass(frozen=True)
class _BurstCandidateAsset:
    asset_id: int
    file_name: str
    absolute_path: str
    bucket: str
    month: str
    context_url: str
    is_live: bool


@dataclass(frozen=True)
class _VisualFingerprint:
    dhash_bits: tuple[int, ...]
    luminance_vector: tuple[float, ...]
    color_histogram: tuple[float, ...]
    capture_time: datetime | None
    normalized_device: tuple[str, str] | None


@dataclass(frozen=True)
class PreviewAssetFace:
    face_observation_id: int
    assignment_id: int
    person_id: str
    person_display_name: str | None
    crop_url: str
    context_url: str


@dataclass(frozen=True)
class PersonFaceGroup:
    person_id: str
    display_name: str
    faces: list[PreviewAssetFace]


@dataclass(frozen=True)
class PreviewAssetDetail:
    asset_id: int
    file_name: str
    template_id: str
    template_name: str
    person_groups: list[PersonFaceGroup]


@dataclass(frozen=True)
class ExportRunListItem:
    run_id: int
    template_id: str
    status: str
    started_at: str
    completed_at: str | None
    copied_count: int
    skipped_count: int


@dataclass(frozen=True)
class ExportDeliveryItem:
    delivery_id: int
    asset_id: int
    target_path: str
    result: str
    mov_result: str


@dataclass(frozen=True)
class ExportRunDetail:
    run_id: int
    template_id: str
    template_name: str
    status: str
    started_at: str
    completed_at: str | None
    copied_count: int
    skipped_count: int
    deliveries: list[ExportDeliveryItem]


def is_export_running(
    workspace_context: WorkspaceContext,
    connection: sqlite3.Connection | None = None,
) -> bool:
    if connection is not None:
        row = connection.execute(
            "SELECT 1 FROM export_run WHERE status = 'running' LIMIT 1"
        ).fetchone()
        return row is not None
    conn = sqlite3.connect(workspace_context.library_db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT 1 FROM export_run WHERE status = 'running' LIMIT 1"
        ).fetchone()
        return row is not None
    except sqlite3.Error as exc:
        raise ExportTemplateError("导出状态读取失败。") from exc
    finally:
        conn.close()


def assert_no_running_export(
    workspace_context: WorkspaceContext,
    connection: sqlite3.Connection | None = None,
) -> None:
    if is_export_running(workspace_context, connection=connection):
        raise ExportTemplateValidationError(
            "导出进行中，暂不可修改人物库。", code="export_in_progress"
        )


def cleanup_stale_export_runs(
    workspace_context: WorkspaceContext,
) -> None:
    connection = sqlite3.connect(workspace_context.library_db_path)
    try:
        connection.execute(
            """
            UPDATE export_run
            SET status = 'failed'
            WHERE status = 'running'
            """
        )
        connection.commit()
    except sqlite3.Error as exc:
        connection.rollback()
        raise ExportTemplateError("残留导出记录清理失败。") from exc
    finally:
        connection.close()


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


def delete_export_template(
    workspace_context: WorkspaceContext,
    *,
    template_id: str,
) -> None:
    connection = sqlite3.connect(workspace_context.library_db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT 1 FROM export_template WHERE template_id = ?",
            (template_id,),
        ).fetchone()
        if row is None:
            raise ExportTemplateValidationError("模板不存在。", code="template_not_found")

        connection.execute(
            "DELETE FROM export_template_person WHERE template_id = ?",
            (template_id,),
        )
        connection.execute(
            "DELETE FROM export_template WHERE template_id = ?",
            (template_id,),
        )
        connection.commit()
    except ExportTemplateValidationError:
        connection.rollback()
        raise
    except sqlite3.Error as exc:
        connection.rollback()
        raise ExportTemplateError("导出模板删除失败。") from exc
    finally:
        connection.close()


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


def load_export_template_detail(
    workspace_context: WorkspaceContext,
    *,
    template_id: str,
) -> ExportTemplateDetail:
    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        template_row = connection.execute(
            """
            SELECT template_id, name, output_root, status, created_at
            FROM export_template
            WHERE template_id = ?
            """,
            (template_id,),
        ).fetchone()
        if template_row is None:
            raise ExportTemplateValidationError("模板不存在。", code="template_not_found")

        person_rows = connection.execute(
            "SELECT person_id FROM export_template_person WHERE template_id = ?",
            (template_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ExportTemplateError("导出模板读取失败。") from exc
    finally:
        connection.close()

    return ExportTemplateDetail(
        template_id=str(template_row["template_id"]),
        name=str(template_row["name"]),
        output_root=str(template_row["output_root"]),
        status=str(template_row["status"]),
        created_at=str(template_row["created_at"]),
        person_ids=[str(r["person_id"]) for r in person_rows],
    )


def compute_export_preview(
    workspace_context: WorkspaceContext,
    *,
    template_id: str,
) -> PreviewResult:
    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        template_row = connection.execute(
            """
            SELECT template_id, status
            FROM export_template
            WHERE template_id = ?
            """,
            (template_id,),
        ).fetchone()
        if template_row is None:
            raise ExportTemplateValidationError("模板不存在。", code="template_not_found")
        if str(template_row["status"]) != "active":
            raise ExportTemplateValidationError(
                "模板已失效，无法预览。", code="template_invalid"
            )

        person_rows = connection.execute(
            "SELECT person_id FROM export_template_person WHERE template_id = ?",
            (template_id,),
        ).fetchall()
        selected_person_ids = [str(r["person_id"]) for r in person_rows]
        selected_person_set = set(selected_person_ids)

        if not selected_person_ids:
            raise ExportTemplateValidationError(
                "模板未关联任何人物。", code="template_empty"
            )

        rows = connection.execute(
            """
            WITH selected_persons AS (
              SELECT person_id FROM export_template_person WHERE template_id = ?
            ),
            asset_has_all AS (
              SELECT fo.asset_id
              FROM face_observations fo
              INNER JOIN person_face_assignments pfa
                ON pfa.face_observation_id = fo.id AND pfa.active = 1
              INNER JOIN selected_persons sp ON sp.person_id = pfa.person_id
              GROUP BY fo.asset_id
              HAVING COUNT(DISTINCT pfa.person_id) = (SELECT COUNT(*) FROM selected_persons)
            )
            SELECT
              a.id AS asset_id,
              a.file_name,
              a.capture_month,
              a.absolute_path,
              a.file_extension,
              a.live_photo_mov_path,
              a.source_id,
              fo.id AS face_id,
              fo.bbox_x1,
              fo.bbox_y1,
              fo.bbox_x2,
              fo.bbox_y2,
              fo.context_path,
              pfa.person_id,
              pfa.id AS assignment_id
            FROM asset_has_all aha
            INNER JOIN assets a ON a.id = aha.asset_id
            LEFT JOIN export_abandoned_asset eaa ON eaa.asset_id = a.id
            INNER JOIN face_observations fo ON fo.asset_id = a.id
            LEFT JOIN person_face_assignments pfa
              ON pfa.face_observation_id = fo.id AND pfa.active = 1
            WHERE eaa.asset_id IS NULL
            ORDER BY a.id, fo.id
            """,
            (template_id,),
        ).fetchall()

        # 查询 source_label 映射
        source_labels: dict[int, str] = {}
        source_rows = connection.execute(
            "SELECT id, label FROM library_sources"
        ).fetchall()
        for sr in source_rows:
            source_labels[int(sr["id"])] = str(sr["label"])

    except ExportTemplateValidationError:
        raise
    except sqlite3.Error as exc:
        raise ExportTemplateError("预览计算失败。") from exc
    finally:
        connection.close()

    # Group faces by asset
    assets_data: dict[int, dict[str, object]] = {}
    for row in rows:
        asset_id = int(row["asset_id"])
        if asset_id not in assets_data:
            assets_data[asset_id] = {
                "asset_id": asset_id,
                "file_name": str(row["file_name"]),
                "capture_month": str(row["capture_month"]) if row["capture_month"] else "",
                "live_photo_mov_path": row["live_photo_mov_path"],
                "source_id": int(row["source_id"]),
                "faces": [],
            }
        area = float(row["bbox_x2"] - row["bbox_x1"]) * float(row["bbox_y2"] - row["bbox_y1"])
        assets_data[asset_id]["faces"].append({
            "area": area,
            "person_id": str(row["person_id"]) if row["person_id"] is not None else None,
            "assignment_id": int(row["assignment_id"]) if row["assignment_id"] is not None else None,
        })

    months: dict[str, dict[str, list[PreviewAsset]]] = defaultdict(
        lambda: {"only": [], "group": []}
    )
    # 按 (bucket, month) 收集命中的 asset，用于冲突消解
    bucket_month_assets: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    total_count = 0
    only_count = 0
    group_count = 0

    for asset in assets_data.values():
        faces = asset["faces"]
        selected_max_areas = {}
        for person_id in selected_person_ids:
            areas = [f["area"] for f in faces if f["person_id"] == person_id]
            if areas:
                selected_max_areas[person_id] = max(areas)

        if len(selected_max_areas) != len(selected_person_ids):
            continue

        selected_min_area = min(selected_max_areas.values())
        threshold = selected_min_area / 4.0

        bucket = "only"
        for face in faces:
            if face["area"] >= threshold:
                if face["person_id"] not in selected_person_set:
                    bucket = "group"
                    break

        rep_person_id = min(selected_person_ids)
        rep_assignment_id = None
        for face in faces:
            if face["person_id"] == rep_person_id and face["assignment_id"] is not None:
                rep_assignment_id = face["assignment_id"]
                break

        asset_preview = PreviewAsset(
            asset_id=asset["asset_id"],
            file_name=asset["file_name"],
            capture_month=asset["capture_month"],
            context_url=f"/images/assignments/{rep_assignment_id}/context" if rep_assignment_id else "",
            representative_person_id=rep_person_id,
            is_live=bool(asset.get("live_photo_mov_path")),
        )

        month = asset["capture_month"] if asset["capture_month"] else "unknown-date"
        months[month][bucket].append(asset_preview)
        bucket_month_assets[(bucket, month)].append(asset)
        total_count += 1
        if bucket == "only":
            only_count += 1
        else:
            group_count += 1

    sorted_months = []
    for month in sorted(months.keys()):
        month_data = months[month]
        only_sorted = sorted(month_data["only"], key=lambda a: a.file_name)
        group_sorted = sorted(month_data["group"], key=lambda a: a.file_name)
        sorted_months.append(
            PreviewMonthBucket(
                month=month,
                total_count=len(only_sorted) + len(group_sorted),
                only_assets=only_sorted,
                group_assets=group_sorted,
            )
        )

    # 写入 export_plan（幂等 upsert + 冲突消解）
    _persist_export_plan(
        workspace_context,
        template_id=template_id,
        bucket_month_assets=bucket_month_assets,
        source_labels=source_labels,
    )

    return PreviewResult(
        total_count=total_count,
        only_count=only_count,
        group_count=group_count,
        month_buckets=sorted_months,
    )


def _persist_export_plan(
    workspace_context: WorkspaceContext,
    *,
    template_id: str,
    bucket_month_assets: dict[tuple[str, str], list[dict[str, object]]],
    source_labels: dict[int, str],
) -> None:
    """将 preview 计算结果持久化到 export_plan 表。

    幂等语义：已有记录（按 UNIQUE(template_id, asset_id)）不动，新命中 insert。
    不再命中的旧记录（如因排除导致 asset 不再匹配）会被删除。
    同名冲突消解：同模板、同 bucket、同 month、同 file_name 的不同 asset_id →
    后续文件在 stem 后追加 __<source_label> 后缀。
    """
    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")

        # 读取已有的 plan 记录（用于冲突检测）
        existing_rows = connection.execute(
            "SELECT id, asset_id, bucket, month, file_name, mov_file_name FROM export_plan WHERE template_id = ?",
            (template_id,),
        ).fetchall()

        existing_asset_ids: set[int] = set()
        # 已存在的 (bucket, month, file_name) 集合，用于冲突检测
        existing_names_by_bucket_month: dict[tuple[str, str], set[str]] = defaultdict(set)
        for row in existing_rows:
            existing_asset_ids.add(int(row["asset_id"]))
            key = (str(row["bucket"]), str(row["month"]))
            existing_names_by_bucket_month[key].add(str(row["file_name"]))

        # 按 asset_id 升序遍历，逐条写入
        for (bucket, month), assets in bucket_month_assets.items():
            # 当前批次已写入的文件名集合（合并已有记录）
            batch_key = (bucket, month)
            known_names = set(existing_names_by_bucket_month.get(batch_key, set()))

            # 每个 original_name 已见过的 source_label 集合
            # 包含首个保持原名的 asset 的 source_label
            seen_labels_by_original: dict[str, set[str]] = defaultdict(set)

            # 按 asset_id 升序排序
            sorted_assets = sorted(assets, key=lambda a: a["asset_id"])

            for asset in sorted_assets:
                asset_id = asset["asset_id"]
                if asset_id in existing_asset_ids:
                    # 已有记录，仍需记录其 source_label 以影响后续冲突消解
                    source_label = source_labels.get(asset["source_id"], "unknown")
                    original_file_name = str(asset["file_name"])
                    seen_labels_by_original[original_file_name].add(source_label)
                    continue

                source_label = source_labels.get(asset["source_id"], "unknown")
                original_file_name = str(asset["file_name"])

                if original_file_name not in known_names:
                    plan_file_name = original_file_name
                    seen_labels_by_original[original_file_name].add(source_label)
                    known_names.add(plan_file_name)
                else:
                    # 冲突：同模板、同 bucket、同 month、同 file_name
                    seen_labels = seen_labels_by_original[original_file_name]
                    suffix = Path(original_file_name).suffix
                    stem = Path(original_file_name).stem

                    if source_label not in seen_labels:
                        # source_label 不同，用 __<source_label> 后缀
                        candidate = f"{stem}__{source_label}{suffix}"
                        if candidate not in known_names:
                            plan_file_name = candidate
                        else:
                            # 不应发生（不同 label 但同名后缀），追加序号兜底
                            seq = 2
                            while True:
                                candidate = f"{stem}__{source_label}-{seq}{suffix}"
                                if candidate not in known_names:
                                    break
                                seq += 1
                            plan_file_name = candidate
                    else:
                        # source_label 相同（两个源目录恰好同名），追加序号
                        seq = 2
                        while True:
                            candidate = f"{stem}__{source_label}-{seq}{suffix}"
                            if candidate not in known_names:
                                break
                            seq += 1
                        plan_file_name = candidate

                    seen_labels_by_original[original_file_name].add(source_label)
                    known_names.add(plan_file_name)

                # MOV 文件名同步重命名
                mov_file_name = None
                if asset.get("live_photo_mov_path"):
                    mov_path = Path(str(asset["live_photo_mov_path"]))
                    if plan_file_name != original_file_name:
                        # 重命名 MOV 与静态图一致
                        mov_stem = mov_path.name
                        # MOV 文件名通常以 . 前缀开头，如 .IMG_0001.MOV
                        if mov_stem.startswith("."):
                            # 处理 dot-prefixed MOV：保留 . 前缀
                            inner_name = mov_stem[1:]
                            inner_stem = Path(inner_name).stem
                            inner_suffix = Path(inner_name).suffix
                            new_inner_name = f"{Path(plan_file_name).stem}{inner_suffix}"
                            mov_file_name = f".{new_inner_name}"
                        else:
                            mov_suffix = mov_path.suffix
                            mov_file_name = f"{Path(plan_file_name).stem}{mov_suffix}"
                    else:
                        mov_file_name = mov_path.name

                connection.execute(
                    """
                    INSERT OR IGNORE INTO export_plan
                    (template_id, asset_id, bucket, month, file_name, mov_file_name, source_label)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (template_id, asset_id, bucket, month, plan_file_name, mov_file_name, source_label),
                )

        # 删除不再命中的旧记录（如因排除导致 asset 不再匹配）
        current_asset_ids: set[int] = set()
        for assets in bucket_month_assets.values():
            for asset in assets:
                current_asset_ids.add(asset["asset_id"])

        if current_asset_ids:
            placeholders = ", ".join("?" for _ in current_asset_ids)
            connection.execute(
                f"DELETE FROM export_plan WHERE template_id = ? AND asset_id NOT IN ({placeholders})",
                (template_id, *current_asset_ids),
            )
        else:
            connection.execute(
                "DELETE FROM export_plan WHERE template_id = ?",
                (template_id,),
            )

        connection.commit()
    except sqlite3.Error as exc:
        connection.rollback()
        raise ExportTemplateError("导出计划写入失败。") from exc
    finally:
        connection.close()


_EXIF_NAME_BY_ID = {value: key for key, value in ExifTags.TAGS.items()}


def load_export_template_burst_pick(
    workspace_context: WorkspaceContext,
    *,
    template_id: str,
) -> BurstPickResult:
    candidates = _load_burst_pick_candidates(workspace_context, template_id=template_id)
    fingerprints: dict[int, _VisualFingerprint] = {}
    display_assets: dict[int, BurstPickAsset] = {}
    skipped_count = 0

    for candidate in candidates:
        try:
            fingerprint = _compute_visual_fingerprint(Path(candidate.absolute_path))
        except (OSError, ValueError):
            skipped_count += 1
            continue
        fingerprints[candidate.asset_id] = fingerprint
        display_assets[candidate.asset_id] = BurstPickAsset(
            asset_id=candidate.asset_id,
            file_name=candidate.file_name,
            bucket=candidate.bucket,
            month=candidate.month,
            context_url=candidate.context_url,
            is_live=candidate.is_live,
        )

    edges: list[BurstPickEdge] = []
    asset_ids = sorted(fingerprints)
    for index, first_id in enumerate(asset_ids):
        for second_id in asset_ids[index + 1:]:
            edge = _build_visual_edge(
                first_id,
                fingerprints[first_id],
                second_id,
                fingerprints[second_id],
            )
            if edge is not None:
                edges.append(edge)

    groups = _connected_burst_groups(
        display_assets=display_assets,
        edges=edges,
    )
    return BurstPickResult(
        template_id=template_id,
        groups=groups,
        skipped_missing_or_unreadable_count=skipped_count,
    )


def submit_export_template_burst_pick(
    workspace_context: WorkspaceContext,
    *,
    template_id: str,
    submitted_groups: list[dict[str, object]],
) -> BurstPickSubmitResult:
    current = load_export_template_burst_pick(workspace_context, template_id=template_id)
    current_by_key = {group.group_key: group for group in current.groups}
    submitted_by_key: dict[str, set[int]] = {}

    for submitted_group in submitted_groups:
        if not isinstance(submitted_group, dict):
            raise ExportTemplateValidationError("连拍组提交格式无效。", code="burst_group_invalid")
        group_key = str(submitted_group.get("group_key", ""))
        if not group_key:
            raise ExportTemplateValidationError("提交包含空的连拍组标识。", code="burst_group_key_blank")
        if group_key in submitted_by_key:
            raise ExportTemplateValidationError("提交包含重复的连拍组。", code="burst_group_duplicate")
        raw_keep_ids = submitted_group.get("keep_asset_ids", [])
        if not isinstance(raw_keep_ids, list):
            raise ExportTemplateValidationError("保留照片格式无效。", code="burst_keep_invalid")
        try:
            keep_ids = {int(asset_id) for asset_id in raw_keep_ids}
        except (TypeError, ValueError) as exc:
            raise ExportTemplateValidationError("保留照片包含无效 asset。", code="burst_keep_invalid") from exc
        submitted_by_key[group_key] = keep_ids

    if set(submitted_by_key) != set(current_by_key):
        raise ExportTemplateValidationError("连拍组已变化，请刷新后重试。", code="burst_groups_stale")

    kept_asset_ids: set[int] = set()
    abandoned_by_asset: dict[int, str] = {}
    for group_key, group in current_by_key.items():
        keep_ids = submitted_by_key[group_key]
        group_asset_ids = {asset.asset_id for asset in group.assets}
        if not keep_ids:
            raise ExportTemplateValidationError(
                "每个相似组至少保留 1 张照片。", code="burst_keep_empty"
            )
        if not keep_ids.issubset(group_asset_ids):
            raise ExportTemplateValidationError("保留照片不属于当前连拍组。", code="burst_keep_outside_group")
        kept_asset_ids.update(keep_ids)
        for asset_id in sorted(group_asset_ids - keep_ids):
            abandoned_by_asset[asset_id] = group_key

    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        assert_no_running_export(workspace_context, connection=connection)
        abandoned_asset_ids = sorted(abandoned_by_asset)
        already_abandoned_count = 0
        if abandoned_asset_ids:
            placeholders = ", ".join("?" for _ in abandoned_asset_ids)
            already_abandoned_count = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM export_abandoned_asset WHERE asset_id IN ({placeholders})",
                    tuple(abandoned_asset_ids),
                ).fetchone()[0]
            )

        created_count = 0
        now = utc_now_text()
        for asset_id in abandoned_asset_ids:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO export_abandoned_asset
                  (asset_id, triggered_template_id, group_key, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (asset_id, template_id, abandoned_by_asset[asset_id], now),
            )
            created_count += int(cursor.rowcount)

        if abandoned_asset_ids:
            placeholders = ", ".join("?" for _ in abandoned_asset_ids)
            connection.execute(
                f"DELETE FROM export_plan WHERE asset_id IN ({placeholders})",
                tuple(abandoned_asset_ids),
            )

        connection.commit()
    except ExportTemplateValidationError:
        connection.rollback()
        raise
    except sqlite3.Error as exc:
        connection.rollback()
        raise ExportTemplateError("连拍挑选保存失败。") from exc
    finally:
        connection.close()

    return BurstPickSubmitResult(
        abandoned_asset_ids=sorted(abandoned_by_asset),
        kept_asset_ids=sorted(kept_asset_ids),
        created_count=created_count,
        already_abandoned_count=already_abandoned_count,
    )


def _load_burst_pick_candidates(
    workspace_context: WorkspaceContext,
    *,
    template_id: str,
) -> list[_BurstCandidateAsset]:
    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        template_row = connection.execute(
            """
            SELECT template_id, status
            FROM export_template
            WHERE template_id = ?
            """,
            (template_id,),
        ).fetchone()
        if template_row is None:
            raise ExportTemplateValidationError("模板不存在。", code="template_not_found")
        if str(template_row["status"]) != "active":
            raise ExportTemplateValidationError("模板已失效，无法连拍挑选。", code="template_invalid")

        person_rows = connection.execute(
            "SELECT person_id FROM export_template_person WHERE template_id = ?",
            (template_id,),
        ).fetchall()
        selected_person_ids = [str(r["person_id"]) for r in person_rows]
        selected_person_set = set(selected_person_ids)
        if not selected_person_ids:
            raise ExportTemplateValidationError("模板未关联任何人物。", code="template_empty")

        rows = connection.execute(
            """
            WITH selected_persons AS (
              SELECT person_id FROM export_template_person WHERE template_id = ?
            ),
            asset_has_all AS (
              SELECT fo.asset_id
              FROM face_observations fo
              INNER JOIN person_face_assignments pfa
                ON pfa.face_observation_id = fo.id AND pfa.active = 1
              INNER JOIN selected_persons sp ON sp.person_id = pfa.person_id
              GROUP BY fo.asset_id
              HAVING COUNT(DISTINCT pfa.person_id) = (SELECT COUNT(*) FROM selected_persons)
            )
            SELECT
              a.id AS asset_id,
              a.file_name,
              a.capture_month,
              a.absolute_path,
              a.live_photo_mov_path,
              fo.id AS face_id,
              fo.bbox_x1,
              fo.bbox_y1,
              fo.bbox_x2,
              fo.bbox_y2,
              pfa.person_id,
              pfa.id AS assignment_id
            FROM asset_has_all aha
            INNER JOIN assets a ON a.id = aha.asset_id
            LEFT JOIN export_abandoned_asset eaa ON eaa.asset_id = a.id
            INNER JOIN face_observations fo ON fo.asset_id = a.id
            LEFT JOIN person_face_assignments pfa
              ON pfa.face_observation_id = fo.id AND pfa.active = 1
            WHERE eaa.asset_id IS NULL
            ORDER BY a.id, fo.id
            """,
            (template_id,),
        ).fetchall()
    except ExportTemplateValidationError:
        raise
    except sqlite3.Error as exc:
        raise ExportTemplateError("连拍候选读取失败。") from exc
    finally:
        connection.close()

    assets_data: dict[int, dict[str, object]] = {}
    for row in rows:
        asset_id = int(row["asset_id"])
        if asset_id not in assets_data:
            assets_data[asset_id] = {
                "asset_id": asset_id,
                "file_name": str(row["file_name"]),
                "capture_month": str(row["capture_month"]) if row["capture_month"] else "",
                "absolute_path": str(row["absolute_path"]),
                "live_photo_mov_path": row["live_photo_mov_path"],
                "faces": [],
            }
        area = float(row["bbox_x2"] - row["bbox_x1"]) * float(row["bbox_y2"] - row["bbox_y1"])
        assets_data[asset_id]["faces"].append({
            "area": area,
            "person_id": str(row["person_id"]) if row["person_id"] is not None else None,
            "assignment_id": int(row["assignment_id"]) if row["assignment_id"] is not None else None,
        })

    candidates: list[_BurstCandidateAsset] = []
    for asset in assets_data.values():
        faces = asset["faces"]
        selected_max_areas = {}
        for person_id in selected_person_ids:
            areas = [f["area"] for f in faces if f["person_id"] == person_id]
            if areas:
                selected_max_areas[person_id] = max(areas)
        if len(selected_max_areas) != len(selected_person_ids):
            continue

        selected_min_area = min(selected_max_areas.values())
        threshold = selected_min_area / 4.0
        bucket = "only"
        for face in faces:
            if face["area"] >= threshold and face["person_id"] not in selected_person_set:
                bucket = "group"
                break

        rep_person_id = min(selected_person_ids)
        rep_assignment_id = None
        for face in faces:
            if face["person_id"] == rep_person_id and face["assignment_id"] is not None:
                rep_assignment_id = face["assignment_id"]
                break

        month = str(asset["capture_month"]) if asset["capture_month"] else "unknown-date"
        candidates.append(
            _BurstCandidateAsset(
                asset_id=int(asset["asset_id"]),
                file_name=str(asset["file_name"]),
                absolute_path=str(asset["absolute_path"]),
                bucket=bucket,
                month=month,
                context_url=f"/images/assignments/{rep_assignment_id}/context" if rep_assignment_id else "",
                is_live=bool(asset.get("live_photo_mov_path")),
            )
        )
    return sorted(candidates, key=lambda candidate: candidate.asset_id)


def _compute_visual_fingerprint(path: Path) -> _VisualFingerprint:
    if os.environ.get("HIKBOX_TEST_BURST_PICK_FAIL_FEATURES") == "1":
        raise ExportTemplateError("视觉特征准备失败。")
    if not path.is_file():
        raise FileNotFoundError(str(path))

    try:
        raw_image = Image.open(path)
        exif = raw_image.getexif()
        image = ImageOps.exif_transpose(raw_image).convert("RGB")
    except Exception as exc:
        raise ValueError(f"图片无法解码：{path}") from exc

    luminance = image.convert("L")
    horizontal = luminance.resize((9, 8), Image.Resampling.BILINEAR)
    horizontal_pixels = list(horizontal.getdata())
    dhash_bits: list[int] = []
    for y in range(8):
        row = y * 9
        for x in range(8):
            dhash_bits.append(1 if horizontal_pixels[row + x] > horizontal_pixels[row + x + 1] else 0)

    vertical = luminance.resize((8, 9), Image.Resampling.BILINEAR)
    vertical_pixels = list(vertical.getdata())
    for y in range(8):
        for x in range(8):
            dhash_bits.append(1 if vertical_pixels[y * 8 + x] > vertical_pixels[(y + 1) * 8 + x] else 0)

    thumbnail_pixels = list(luminance.resize((16, 16), Image.Resampling.BILINEAR).getdata())
    mean_value = sum(thumbnail_pixels) / len(thumbnail_pixels)
    centered = [float(value) - mean_value for value in thumbnail_pixels]
    norm = math.sqrt(sum(value * value for value in centered))
    luminance_vector = tuple(0.0 for _ in centered) if norm == 0 else tuple(value / norm for value in centered)

    histogram = [0 for _ in range(64)]
    for red, green, blue in image.getdata():
        histogram[(red // 64) * 16 + (green // 64) * 4 + (blue // 64)] += 1
    total_pixels = sum(histogram)
    color_histogram = tuple(value / total_pixels for value in histogram)

    return _VisualFingerprint(
        dhash_bits=tuple(dhash_bits),
        luminance_vector=luminance_vector,
        color_histogram=color_histogram,
        capture_time=_extract_capture_time(exif),
        normalized_device=_extract_normalized_device(exif),
    )


def _extract_capture_time(exif: object) -> datetime | None:
    for tag_name in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
        tag_id = _EXIF_NAME_BY_ID.get(tag_name)
        if tag_id is None:
            continue
        value = exif.get(tag_id)
        if not value:
            continue
        text = str(value).strip()
        for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                pass
    return None


def _extract_normalized_device(exif: object) -> tuple[str, str] | None:
    make_tag = _EXIF_NAME_BY_ID.get("Make")
    model_tag = _EXIF_NAME_BY_ID.get("Model")
    if make_tag is None or model_tag is None:
        return None
    make = _normalize_device_part(exif.get(make_tag))
    model = _normalize_device_part(exif.get(model_tag))
    if not make or not model:
        return None
    return (make, model)


def _normalize_device_part(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _build_visual_edge(
    first_id: int,
    first: _VisualFingerprint,
    second_id: int,
    second: _VisualFingerprint,
) -> BurstPickEdge | None:
    dhash_hamming = sum(a != b for a, b in zip(first.dhash_bits, second.dhash_bits))
    luminance_cosine = _luminance_cosine(first.luminance_vector, second.luminance_vector)
    color_intersection = sum(min(a, b) for a, b in zip(first.color_histogram, second.color_histogram))

    capture_delta: float | None = None
    if first.capture_time is not None and second.capture_time is not None:
        capture_delta = abs((first.capture_time - second.capture_time).total_seconds())

    normalized_device_match: bool | None = None
    if first.normalized_device is not None and second.normalized_device is not None:
        normalized_device_match = first.normalized_device == second.normalized_device

    threshold_name: str | None = None
    metadata_assisted = False
    if dhash_hamming <= 10 and color_intersection >= 0.88:
        threshold_name = "strict"
    elif dhash_hamming <= 18 and luminance_cosine >= 0.96 and color_intersection >= 0.80:
        threshold_name = "resave_or_light_edit"
    elif (
        normalized_device_match is True
        and capture_delta is not None
        and capture_delta <= 10
        and dhash_hamming <= 24
        and luminance_cosine >= 0.94
        and color_intersection >= 0.72
    ):
        threshold_name = "metadata_assisted"
        metadata_assisted = True

    if threshold_name is None:
        return None
    asset_ids = tuple(sorted((first_id, second_id)))
    return BurstPickEdge(
        asset_ids=(int(asset_ids[0]), int(asset_ids[1])),
        threshold=threshold_name,
        metadata_assisted=metadata_assisted,
        dhash_hamming=int(dhash_hamming),
        luminance_cosine=float(luminance_cosine),
        color_histogram_intersection=float(color_intersection),
        capture_time_delta_seconds=capture_delta,
        normalized_device_match=normalized_device_match,
    )


def _luminance_cosine(first: tuple[float, ...], second: tuple[float, ...]) -> float:
    first_zero = all(value == 0 for value in first)
    second_zero = all(value == 0 for value in second)
    if first_zero and second_zero:
        return 1.0
    if first_zero or second_zero:
        return 0.0
    return sum(a * b for a, b in zip(first, second))


def _connected_burst_groups(
    *,
    display_assets: dict[int, BurstPickAsset],
    edges: list[BurstPickEdge],
) -> list[BurstPickGroup]:
    parent = {asset_id: asset_id for asset_id in display_assets}

    def find(asset_id: int) -> int:
        while parent[asset_id] != asset_id:
            parent[asset_id] = parent[parent[asset_id]]
            asset_id = parent[asset_id]
        return asset_id

    def union(first_id: int, second_id: int) -> None:
        first_root = find(first_id)
        second_root = find(second_id)
        if first_root != second_root:
            parent[max(first_root, second_root)] = min(first_root, second_root)

    for edge in edges:
        union(edge.asset_ids[0], edge.asset_ids[1])

    component_assets: dict[int, list[int]] = defaultdict(list)
    for asset_id in sorted(display_assets):
        component_assets[find(asset_id)].append(asset_id)

    edges_by_component: dict[int, list[BurstPickEdge]] = defaultdict(list)
    for edge in edges:
        edges_by_component[find(edge.asset_ids[0])].append(edge)

    groups: list[BurstPickGroup] = []
    for asset_ids in component_assets.values():
        if len(asset_ids) < 2:
            continue
        sorted_asset_ids = sorted(asset_ids)
        group_edges = sorted(
            edges_by_component[find(sorted_asset_ids[0])],
            key=lambda edge: (edge.asset_ids[0], edge.asset_ids[1]),
        )
        groups.append(
            BurstPickGroup(
                group_key="g_" + "_".join(str(asset_id) for asset_id in sorted_asset_ids),
                assets=[display_assets[asset_id] for asset_id in sorted_asset_ids],
                edges=group_edges,
            )
        )
    return sorted(groups, key=lambda group: group.assets[0].asset_id)


_export_log = logging.getLogger("hikbox_pictures.export")


def _create_export_run(
    workspace_context: WorkspaceContext,
    *,
    template_id: str,
) -> tuple[int, Path]:
    """验证模板、创建 export_run 记录，返回 (run_id, output_root)。"""
    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        # 设置 busy_timeout 以允许并发请求在事务锁上等待，避免立即失败返回 500
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("BEGIN IMMEDIATE")

        template_row = connection.execute(
            """
            SELECT template_id, status, output_root
            FROM export_template
            WHERE template_id = ?
            """,
            (template_id,),
        ).fetchone()
        if template_row is None:
            connection.rollback()
            raise ExportTemplateValidationError("模板不存在。", code="template_not_found")
        if str(template_row["status"]) != "active":
            connection.rollback()
            raise ExportTemplateValidationError(
                "模板已失效，无法执行导出。", code="template_invalid"
            )

        output_root = Path(str(template_row["output_root"]))
        now = utc_now_text()

        cursor = connection.execute(
            """
            INSERT INTO export_run (template_id, status, started_at)
            SELECT ?, 'running', ?
            WHERE NOT EXISTS (SELECT 1 FROM export_run WHERE status = 'running')
            """,
            (template_id, now),
        )
        if cursor.rowcount == 0:
            connection.rollback()
            raise ExportTemplateValidationError(
                "已有导出正在进行中。", code="export_in_progress"
            )
        run_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.commit()
        return run_id, output_root
    except ExportTemplateValidationError:
        raise
    except sqlite3.Error as exc:
        raise ExportTemplateError("导出启动失败。") from exc
    finally:
        connection.close()


def _run_export(
    workspace_context: WorkspaceContext,
    *,
    template_id: str,
    run_id: int,
    output_root: Path,
) -> None:
    """从 export_plan 读取记录，执行文件复制，更新 run 状态。"""
    copied_count = 0
    skipped_count = 0
    status = "completed"

    try:
        conn = sqlite3.connect(workspace_context.library_db_path)
        conn.row_factory = sqlite3.Row
        try:
            plan_rows = conn.execute(
                """
                SELECT
                  ep.id AS plan_id,
                  ep.asset_id,
                  ep.bucket,
                  ep.month,
                  ep.file_name AS plan_file_name,
                  ep.mov_file_name AS plan_mov_file_name,
                  a.absolute_path,
                  a.file_extension,
                  a.live_photo_mov_path
                FROM export_plan ep
                INNER JOIN assets a ON a.id = ep.asset_id
                LEFT JOIN export_abandoned_asset eaa ON eaa.asset_id = ep.asset_id
                WHERE ep.template_id = ?
                  AND eaa.asset_id IS NULL
                ORDER BY ep.asset_id
                """,
                (template_id,),
            ).fetchall()

            for plan_row in plan_rows:
                plan_id = int(plan_row["plan_id"])
                asset_id = int(plan_row["asset_id"])
                bucket = str(plan_row["bucket"])
                month = str(plan_row["month"])
                plan_file_name = str(plan_row["plan_file_name"])
                plan_mov_file_name = str(plan_row["plan_mov_file_name"]) if plan_row["plan_mov_file_name"] else None
                absolute_path = str(plan_row["absolute_path"])
                file_extension = str(plan_row["file_extension"]).lower()
                live_photo_mov_path = plan_row["live_photo_mov_path"]

                bucket_dir = output_root / bucket / month
                src_path = Path(absolute_path)
                dst_path = bucket_dir / plan_file_name
                live_photo_pair = (
                    file_extension in ("heic", "heif", "jpg", "jpeg")
                    and live_photo_mov_path
                    and plan_mov_file_name
                )

                if dst_path.exists():
                    result = "skipped_exists"
                    mov_result = "not_applicable"
                else:
                    bucket_dir.mkdir(parents=True, exist_ok=True)
                    result = "copied"

                    mov_result = "not_applicable"
                    if live_photo_pair:
                        mov_src = Path(str(live_photo_mov_path))
                        mov_dst = bucket_dir / plan_mov_file_name
                        if mov_src.exists():
                            run_live_photo_export_helper(
                                still_src=src_path,
                                mov_src=mov_src,
                                still_dst=dst_path,
                                mov_dst=mov_dst,
                            )
                            mov_result = "copied"
                        else:
                            shutil.copy2(src_path, dst_path)
                            mov_result = "skipped_missing"
                    else:
                        shutil.copy2(src_path, dst_path)

                target_path = str(output_root / bucket / month / plan_file_name)

                conn.execute(
                    """
                    INSERT INTO export_delivery
                    (run_id, asset_id, target_path, result, mov_result, plan_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (run_id, asset_id, target_path, result, mov_result, plan_id),
                )
                conn.commit()

                if result == "copied":
                    copied_count += 1
                else:
                    skipped_count += 1

                if _per_file_copy_hook is not None:
                    _per_file_copy_hook()
        finally:
            conn.close()
    except Exception:
        status = "failed"
        raise
    finally:
        conn = sqlite3.connect(workspace_context.library_db_path)
        try:
            conn.execute(
                """
                UPDATE export_run
                SET status = ?, completed_at = ?, copied_count = ?, skipped_count = ?
                WHERE run_id = ?
                """,
                (status, utc_now_text(), copied_count, skipped_count, run_id),
            )
            conn.commit()
        finally:
            conn.close()


def execute_export(
    workspace_context: WorkspaceContext,
    *,
    template_id: str,
) -> int:
    """同步执行导出（创建 run + 复制文件），返回 run_id。"""
    run_id, output_root = _create_export_run(workspace_context, template_id=template_id)
    _run_export(workspace_context, template_id=template_id, run_id=run_id, output_root=output_root)
    return run_id


def execute_export_async(
    workspace_context: WorkspaceContext,
    *,
    template_id: str,
) -> int:
    """创建 export_run 记录后立即返回 run_id，文件复制在后台线程中执行。"""
    run_id, output_root = _create_export_run(workspace_context, template_id=template_id)

    def _background() -> None:
        try:
            _run_export(
                workspace_context,
                template_id=template_id,
                run_id=run_id,
                output_root=output_root,
            )
        except Exception:
            _export_log.exception("后台导出 run_id=%d 失败", run_id)

    threading.Thread(target=_background, daemon=True).start()
    return run_id


def load_export_runs_for_template(
    workspace_context: WorkspaceContext,
    *,
    template_id: str,
) -> list[ExportRunListItem]:
    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT run_id, template_id, status, started_at, completed_at,
                   copied_count, skipped_count
            FROM export_run
            WHERE template_id = ?
            ORDER BY run_id DESC
            """,
            (template_id,),
        ).fetchall()

        result = []
        for row in rows:
            status = str(row["status"])
            copied_count = int(row["copied_count"])
            skipped_count = int(row["skipped_count"])

            # running 状态下 copied_count/skipped_count 尚未写入，从 delivery 实时计算
            if status == "running":
                counts = connection.execute(
                    """
                    SELECT
                      SUM(CASE WHEN result = 'copied' THEN 1 ELSE 0 END) AS copied,
                      SUM(CASE WHEN result = 'skipped_exists' THEN 1 ELSE 0 END) AS skipped
                    FROM export_delivery
                    WHERE run_id = ?
                    """,
                    (int(row["run_id"]),),
                ).fetchone()
                if counts is not None:
                    copied_count = int(counts["copied"] or 0)
                    skipped_count = int(counts["skipped"] or 0)

            result.append(
                ExportRunListItem(
                    run_id=int(row["run_id"]),
                    template_id=str(row["template_id"]),
                    status=status,
                    started_at=str(row["started_at"]),
                    completed_at=str(row["completed_at"]) if row["completed_at"] else None,
                    copied_count=copied_count,
                    skipped_count=skipped_count,
                )
            )
        return result
    except sqlite3.Error as exc:
        raise ExportTemplateError("导出历史读取失败。") from exc
    finally:
        connection.close()


def load_export_run_detail(
    workspace_context: WorkspaceContext,
    *,
    run_id: int,
) -> ExportRunDetail:
    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        run_row = connection.execute(
            """
            SELECT run_id, template_id, status, started_at, completed_at,
                   copied_count, skipped_count
            FROM export_run
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if run_row is None:
            raise ExportTemplateValidationError("导出记录不存在。", code="run_not_found")

        template_name = connection.execute(
            "SELECT name FROM export_template WHERE template_id = ?",
            (str(run_row["template_id"]),),
        ).fetchone()[0]

        delivery_rows = connection.execute(
            """
            SELECT delivery_id, asset_id, target_path, result, mov_result
            FROM export_delivery
            WHERE run_id = ?
            ORDER BY delivery_id ASC
            """,
            (run_id,),
        ).fetchall()
    except ExportTemplateValidationError:
        raise
    except sqlite3.Error as exc:
        raise ExportTemplateError("导出详情读取失败。") from exc
    finally:
        connection.close()

    return ExportRunDetail(
        run_id=int(run_row["run_id"]),
        template_id=str(run_row["template_id"]),
        template_name=str(template_name),
        status=str(run_row["status"]),
        started_at=str(run_row["started_at"]),
        completed_at=str(run_row["completed_at"]) if run_row["completed_at"] else None,
        copied_count=int(run_row["copied_count"]),
        skipped_count=int(run_row["skipped_count"]),
        deliveries=[
            ExportDeliveryItem(
                delivery_id=int(row["delivery_id"]),
                asset_id=int(row["asset_id"]),
                target_path=str(row["target_path"]),
                result=str(row["result"]),
                mov_result=str(row["mov_result"]),
            )
            for row in delivery_rows
        ],
    )


def load_export_preview_asset_detail(
    workspace_context: WorkspaceContext,
    *,
    template_id: str,
    asset_id: int,
) -> PreviewAssetDetail | None:
    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        template_row = connection.execute(
            "SELECT template_id, name, status FROM export_template WHERE template_id = ?",
            (template_id,),
        ).fetchone()
        if template_row is None:
            raise ExportTemplateValidationError("模板不存在。", code="template_not_found")
        if str(template_row["status"]) != "active":
            raise ExportTemplateValidationError(
                "模板已失效，无法查看。", code="template_invalid"
            )
        template_name = str(template_row["name"])

        person_rows = connection.execute(
            "SELECT person_id FROM export_template_person WHERE template_id = ?",
            (template_id,),
        ).fetchall()
        selected_person_ids = [str(r["person_id"]) for r in person_rows]

        asset_row = connection.execute(
            "SELECT file_name FROM assets WHERE id = ?",
            (asset_id,),
        ).fetchone()
        if asset_row is None:
            return None
        file_name = str(asset_row["file_name"])

        person_placeholders = ", ".join("?" for _ in selected_person_ids)
        face_rows = connection.execute(
            f"""
            SELECT
              fo.id AS face_observation_id,
              pfa.id AS assignment_id,
              pfa.person_id,
              p.display_name,
              fo.crop_path,
              fo.context_path
            FROM face_observations fo
            INNER JOIN person_face_assignments pfa
              ON pfa.face_observation_id = fo.id AND pfa.active = 1
            INNER JOIN person p
              ON p.id = pfa.person_id AND p.status = 'active'
            WHERE fo.asset_id = ?
              AND pfa.person_id IN ({person_placeholders})
            ORDER BY pfa.person_id ASC, pfa.id ASC
            """,
            (asset_id, *selected_person_ids),
        ).fetchall()

        person_name_rows = connection.execute(
            f"""
            SELECT id, display_name
            FROM person
            WHERE id IN ({person_placeholders})
            """,
            tuple(selected_person_ids),
        ).fetchall()
        person_names: dict[str, str | None] = {
            str(r["id"]): str(r["display_name"]) if r["display_name"] else None
            for r in person_name_rows
        }

    except ExportTemplateValidationError:
        raise
    except sqlite3.Error as exc:
        raise ExportTemplateError("照片-人物详情读取失败。") from exc
    finally:
        connection.close()

    faces_by_person: dict[str, list[PreviewAssetFace]] = defaultdict(list)
    for row in face_rows:
        pid = str(row["person_id"])
        faces_by_person[pid].append(
            PreviewAssetFace(
                face_observation_id=int(row["face_observation_id"]),
                assignment_id=int(row["assignment_id"]),
                person_id=pid,
                person_display_name=str(row["display_name"]) if row["display_name"] else None,
                crop_url=f"/images/faces/{int(row['face_observation_id'])}/crop",
                context_url=f"/images/assignments/{int(row['assignment_id'])}/context",
            )
        )

    person_groups: list[PersonFaceGroup] = []
    for pid in selected_person_ids:
        display_name = person_names.get(pid)
        if display_name is None:
            display_name = f"匿名人物 #{pid.replace('-', '')[:8]}"
        person_groups.append(
            PersonFaceGroup(
                person_id=pid,
                display_name=display_name,
                faces=faces_by_person.get(pid, []),
            )
        )

    return PreviewAssetDetail(
        asset_id=asset_id,
        file_name=file_name,
        template_id=template_id,
        template_name=template_name,
        person_groups=person_groups,
    )
