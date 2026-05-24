from __future__ import annotations

from collections import Counter
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
import math
import os
import logging
import shutil
import sqlite3
import subprocess
import threading
import time
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


BURST_PICK_ALGORITHM_VERSION = "visual_fingerprint_v2_multifeature_recall_merge_v3"


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
    original_url: str
    is_live: bool


@dataclass(frozen=True)
class BurstPickEdge:
    asset_ids: tuple[int, int]
    edge_type: str
    confidence: float
    phash_hamming: int
    dhash_hamming: int
    center_phash_hamming: int
    block_match_ratio: float
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
    status: str
    run_id: int | None
    groups: list[BurstPickGroup]
    skipped_missing_or_unreadable_count: int
    total_candidate_count: int
    processed_candidate_count: int
    error_message: str | None


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
    global_phash_bits: tuple[int, ...]
    center_phash_bits: tuple[int, ...]
    block_phash_bits: tuple[tuple[int, ...], ...]
    event_time: datetime | None
    normalized_device: tuple[str, str] | None
    width: int
    height: int
    file_size: int


@dataclass(frozen=True)
class _PairMetrics:
    dhash_hamming: int
    phash_hamming: int
    center_phash_hamming: int
    block_match_ratio: float
    capture_time_delta_seconds: float | None
    normalized_device_match: bool | None


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
    run_id = _ensure_burst_pick_run(workspace_context, template_id=template_id)
    return _load_burst_pick_run_result(
        workspace_context,
        template_id=template_id,
        run_id=run_id,
    )


def _ensure_burst_pick_run(
    workspace_context: WorkspaceContext,
    *,
    template_id: str,
) -> int:
    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        _validate_burst_pick_template(connection, template_id=template_id)
        existing_row = _latest_burst_pick_run_row(connection, template_id=template_id)
        if existing_row is not None and (
            str(existing_row["status"]) != "failed" or _burst_pick_failure_injection_active()
        ):
            return int(existing_row["id"])
    except ExportTemplateValidationError:
        raise
    except sqlite3.Error as exc:
        raise ExportTemplateError("连拍挑选状态读取失败。") from exc
    finally:
        connection.close()

    candidates = _load_burst_pick_candidates(workspace_context, template_id=template_id)
    now = utc_now_text()

    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("BEGIN IMMEDIATE")
        existing_row = _latest_burst_pick_run_row(connection, template_id=template_id)
        if existing_row is not None and (
            str(existing_row["status"]) != "failed" or _burst_pick_failure_injection_active()
        ):
            connection.commit()
            return int(existing_row["id"])
        cursor = connection.execute(
            """
            INSERT INTO export_burst_pick_run (
              template_id,
              algorithm_version,
              status,
              started_at,
              total_candidate_count,
              processed_candidate_count,
              skipped_missing_or_unreadable_count
            )
            VALUES (?, ?, 'running', ?, ?, 0, 0)
            """,
            (template_id, BURST_PICK_ALGORITHM_VERSION, now, len(candidates)),
        )
        run_id = int(cursor.lastrowid)
        connection.commit()
    except sqlite3.Error as exc:
        connection.rollback()
        raise ExportTemplateError("连拍挑选任务启动失败。") from exc
    finally:
        connection.close()

    thread = threading.Thread(
        target=_run_burst_pick_task,
        kwargs={
            "workspace_context": workspace_context,
            "template_id": template_id,
            "run_id": run_id,
        },
        daemon=True,
    )
    thread.start()
    return run_id


def _burst_pick_failure_injection_active() -> bool:
    return (
        os.environ.get("HIKBOX_TEST_BURST_PICK_FAIL_FEATURES") == "1"
        or os.environ.get("HIKBOX_TEST_BURST_PICK_FAIL_PERSISTENCE") == "1"
    )


def _run_burst_pick_task(
    *,
    workspace_context: WorkspaceContext,
    template_id: str,
    run_id: int,
) -> None:
    try:
        candidates = _load_burst_pick_candidates(workspace_context, template_id=template_id)
        _update_burst_pick_run_totals(
            workspace_context,
            run_id=run_id,
            total_candidate_count=len(candidates),
            processed_candidate_count=0,
        )
        groups, skipped_count = _compute_burst_pick_groups(
            candidates,
            progress_callback=lambda processed: _update_burst_pick_run_totals(
                workspace_context,
                run_id=run_id,
                total_candidate_count=len(candidates),
                processed_candidate_count=processed,
            ),
        )
        _persist_burst_pick_run_success(
            workspace_context,
            run_id=run_id,
            groups=groups,
            skipped_count=skipped_count,
            total_candidate_count=len(candidates),
        )
    except Exception as exc:
        _persist_burst_pick_run_failure(
            workspace_context,
            run_id=run_id,
            error_message=str(exc),
        )
        _export_log.exception("连拍挑选后台处理失败: template_id=%s run_id=%s", template_id, run_id)


def _compute_burst_pick_groups(
    candidates: list[_BurstCandidateAsset],
    *,
    progress_callback: callable | None = None,
) -> tuple[list[BurstPickGroup], int]:
    fingerprints: dict[int, _VisualFingerprint] = {}
    display_assets: dict[int, BurstPickAsset] = {}
    skipped_count = 0

    for processed_count, candidate in enumerate(candidates, start=1):
        try:
            fingerprint = _compute_visual_fingerprint(Path(candidate.absolute_path))
        except (OSError, ValueError):
            skipped_count += 1
            if progress_callback is not None:
                progress_callback(processed_count)
            continue
        fingerprints[candidate.asset_id] = fingerprint
        display_assets[candidate.asset_id] = BurstPickAsset(
            asset_id=candidate.asset_id,
            file_name=candidate.file_name,
            bucket=candidate.bucket,
            month=candidate.month,
            context_url=candidate.context_url,
            original_url=_asset_original_url(candidate.asset_id),
            is_live=candidate.is_live,
        )
        if progress_callback is not None:
            progress_callback(processed_count)

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
        fingerprints=fingerprints,
        edges=edges,
    )
    return groups, skipped_count


def _asset_original_url(asset_id: int) -> str:
    return f"/images/assets/{asset_id}/original"


def load_asset_original_path(
    workspace_context: WorkspaceContext,
    *,
    asset_id: int,
) -> Path | None:
    connection = sqlite3.connect(workspace_context.library_db_path)
    try:
        row = connection.execute(
            "SELECT absolute_path FROM assets WHERE id = ?",
            (asset_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise ExportTemplateError("原图路径读取失败。") from exc
    finally:
        connection.close()
    if row is None:
        return None
    return Path(str(row[0]))


def _latest_burst_pick_run_row(
    connection: sqlite3.Connection,
    *,
    template_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT
          id,
          template_id,
          status,
          started_at,
          finished_at,
          error_message,
          total_candidate_count,
          processed_candidate_count,
          skipped_missing_or_unreadable_count
        FROM export_burst_pick_run
        WHERE template_id = ?
          AND algorithm_version = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (template_id, BURST_PICK_ALGORITHM_VERSION),
    ).fetchone()


def _validate_burst_pick_template(
    connection: sqlite3.Connection,
    *,
    template_id: str,
) -> None:
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

    person_row = connection.execute(
        "SELECT COUNT(*) FROM export_template_person WHERE template_id = ?",
        (template_id,),
    ).fetchone()
    if int(person_row[0]) == 0:
        raise ExportTemplateValidationError("模板未关联任何人物。", code="template_empty")


def _update_burst_pick_run_totals(
    workspace_context: WorkspaceContext,
    *,
    run_id: int,
    total_candidate_count: int,
    processed_candidate_count: int,
) -> None:
    connection = sqlite3.connect(workspace_context.library_db_path)
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        with connection:
            connection.execute(
                """
                UPDATE export_burst_pick_run
                SET total_candidate_count = ?,
                    processed_candidate_count = ?
                WHERE id = ?
                  AND status = 'running'
                """,
                (total_candidate_count, processed_candidate_count, run_id),
            )
    finally:
        connection.close()


def _persist_burst_pick_run_success(
    workspace_context: WorkspaceContext,
    *,
    run_id: int,
    groups: list[BurstPickGroup],
    skipped_count: int,
    total_candidate_count: int,
) -> None:
    connection = sqlite3.connect(workspace_context.library_db_path)
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("BEGIN IMMEDIATE")
        for ordinal, group in enumerate(groups):
            group_cursor = connection.execute(
                """
                INSERT INTO export_burst_pick_group (run_id, group_key, ordinal)
                VALUES (?, ?, ?)
                """,
                (run_id, group.group_key, ordinal),
            )
            group_id = int(group_cursor.lastrowid)
            for position, asset in enumerate(group.assets):
                connection.execute(
                    """
                    INSERT INTO export_burst_pick_group_asset (
                      group_id,
                      asset_id,
                      position,
                      file_name,
                      bucket,
                      month,
                      context_url,
                      is_live
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        group_id,
                        asset.asset_id,
                        position,
                        asset.file_name,
                        asset.bucket,
                        asset.month,
                        asset.context_url,
                        int(asset.is_live),
                    ),
                )
            for edge in group.edges:
                if os.environ.get("HIKBOX_TEST_BURST_PICK_FAIL_PERSISTENCE") == "1":
                    raise sqlite3.OperationalError("受控连拍 evidence 写入失败")
                connection.execute(
                    """
                    INSERT INTO export_burst_pick_group_edge (
                      group_id,
                      asset_id_first,
                      asset_id_second,
                      threshold,
                      metadata_assisted,
                      dhash_hamming,
                      luminance_cosine,
                      color_histogram_intersection,
                      capture_time_delta_seconds,
                      normalized_device_match,
                      edge_type,
                      confidence,
                      phash_hamming,
                      center_phash_hamming,
                      block_match_ratio
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        group_id,
                        edge.asset_ids[0],
                        edge.asset_ids[1],
                        edge.edge_type,
                        0,
                        edge.dhash_hamming,
                        0.0,
                        0.0,
                        edge.capture_time_delta_seconds,
                        None
                        if edge.normalized_device_match is None
                        else int(edge.normalized_device_match),
                        edge.edge_type,
                        edge.confidence,
                        edge.phash_hamming,
                        edge.center_phash_hamming,
                        edge.block_match_ratio,
                    ),
                )
        connection.execute(
            """
            UPDATE export_burst_pick_run
            SET status = 'completed',
                finished_at = ?,
                error_message = NULL,
                total_candidate_count = ?,
                processed_candidate_count = ?,
                skipped_missing_or_unreadable_count = ?
            WHERE id = ?
            """,
            (utc_now_text(), total_candidate_count, total_candidate_count, skipped_count, run_id),
        )
        connection.commit()
    except sqlite3.Error as exc:
        connection.rollback()
        raise ExportTemplateError("连拍挑选结果保存失败。") from exc
    finally:
        connection.close()


def _persist_burst_pick_run_failure(
    workspace_context: WorkspaceContext,
    *,
    run_id: int,
    error_message: str,
) -> None:
    connection = sqlite3.connect(workspace_context.library_db_path)
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        with connection:
            connection.execute(
                """
                UPDATE export_burst_pick_run
                SET status = 'failed',
                    finished_at = ?,
                    error_message = ?
                WHERE id = ?
                """,
                (utc_now_text(), error_message, run_id),
            )
    finally:
        connection.close()


def _load_burst_pick_run_result(
    workspace_context: WorkspaceContext,
    *,
    template_id: str,
    run_id: int,
) -> BurstPickResult:
    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        run_row = connection.execute(
            """
            SELECT
              id,
              status,
              error_message,
              total_candidate_count,
              processed_candidate_count,
              skipped_missing_or_unreadable_count
            FROM export_burst_pick_run
            WHERE id = ?
              AND template_id = ?
            """,
            (run_id, template_id),
        ).fetchone()
        if run_row is None:
            raise ExportTemplateError("连拍挑选任务不存在。")

        groups: list[BurstPickGroup] = []
        if str(run_row["status"]) == "completed":
            group_rows = connection.execute(
                """
                SELECT g.id, g.group_key
                FROM export_burst_pick_group g
                WHERE g.run_id = ?
                  AND g.submitted_at IS NULL
                  AND NOT EXISTS (
                    SELECT 1
                    FROM export_burst_pick_group_asset ga
                    INNER JOIN export_abandoned_asset eaa ON eaa.asset_id = ga.asset_id
                    WHERE ga.group_id = g.id
                  )
                ORDER BY g.ordinal ASC
                """,
                (run_id,),
            ).fetchall()
            for group_row in group_rows:
                group_id = int(group_row["id"])
                asset_rows = connection.execute(
                    """
                    SELECT
                      asset_id,
                      file_name,
                      bucket,
                      month,
                      context_url,
                      is_live
                    FROM export_burst_pick_group_asset
                    WHERE group_id = ?
                    ORDER BY position ASC
                    """,
                    (group_id,),
                ).fetchall()
                edge_rows = connection.execute(
                    """
                    SELECT
                      asset_id_first,
                      asset_id_second,
                      threshold,
                      metadata_assisted,
                      dhash_hamming,
                      luminance_cosine,
                      color_histogram_intersection,
                      capture_time_delta_seconds,
                      normalized_device_match,
                      edge_type,
                      confidence,
                      phash_hamming,
                      center_phash_hamming,
                      block_match_ratio
                    FROM export_burst_pick_group_edge
                    WHERE group_id = ?
                    ORDER BY asset_id_first ASC, asset_id_second ASC, edge_type ASC
                    """,
                    (group_id,),
                ).fetchall()
                groups.append(
                    BurstPickGroup(
                        group_key=str(group_row["group_key"]),
                        assets=[
                            BurstPickAsset(
                                asset_id=int(asset_row["asset_id"]),
                                file_name=str(asset_row["file_name"]),
                                bucket=str(asset_row["bucket"]),
                                month=str(asset_row["month"]),
                                context_url=str(asset_row["context_url"]),
                                original_url=_asset_original_url(int(asset_row["asset_id"])),
                                is_live=bool(asset_row["is_live"]),
                            )
                            for asset_row in asset_rows
                        ],
                        edges=[
                            BurstPickEdge(
                                asset_ids=(
                                    int(edge_row["asset_id_first"]),
                                    int(edge_row["asset_id_second"]),
                                ),
                                edge_type=str(edge_row["edge_type"]),
                                confidence=float(edge_row["confidence"]),
                                phash_hamming=int(edge_row["phash_hamming"]),
                                dhash_hamming=int(edge_row["dhash_hamming"]),
                                center_phash_hamming=int(edge_row["center_phash_hamming"]),
                                block_match_ratio=float(edge_row["block_match_ratio"]),
                                capture_time_delta_seconds=(
                                    None
                                    if edge_row["capture_time_delta_seconds"] is None
                                    else float(edge_row["capture_time_delta_seconds"])
                                ),
                                normalized_device_match=(
                                    None
                                    if edge_row["normalized_device_match"] is None
                                    else bool(edge_row["normalized_device_match"])
                                ),
                            )
                            for edge_row in edge_rows
                        ],
                    )
                )
    except sqlite3.Error as exc:
        raise ExportTemplateError("连拍挑选结果读取失败。") from exc
    finally:
        connection.close()

    return BurstPickResult(
        template_id=template_id,
        status=str(run_row["status"]),
        run_id=int(run_row["id"]),
        groups=groups,
        skipped_missing_or_unreadable_count=int(run_row["skipped_missing_or_unreadable_count"]),
        total_candidate_count=int(run_row["total_candidate_count"]),
        processed_candidate_count=int(run_row["processed_candidate_count"]),
        error_message=(
            None if run_row["error_message"] is None else str(run_row["error_message"])
        ),
    )


def submit_export_template_burst_pick(
    workspace_context: WorkspaceContext,
    *,
    template_id: str,
    submitted_groups: list[dict[str, object]],
) -> BurstPickSubmitResult:
    if len(submitted_groups) != 1:
        raise ExportTemplateValidationError("每次只能提交一个相似组。", code="burst_single_group_required")
    submitted_group = submitted_groups[0]
    if not isinstance(submitted_group, dict):
        raise ExportTemplateValidationError("连拍组提交格式无效。", code="burst_group_invalid")
    group_key = str(submitted_group.get("group_key", ""))
    if not group_key:
        raise ExportTemplateValidationError("提交包含空的连拍组标识。", code="burst_group_key_blank")
    raw_keep_ids = submitted_group.get("keep_asset_ids", [])
    if not isinstance(raw_keep_ids, list):
        raise ExportTemplateValidationError("保留照片格式无效。", code="burst_keep_invalid")
    try:
        keep_ids = {int(asset_id) for asset_id in raw_keep_ids}
    except (TypeError, ValueError) as exc:
        raise ExportTemplateValidationError("保留照片包含无效 asset。", code="burst_keep_invalid") from exc
    if not keep_ids:
        raise ExportTemplateValidationError(
            "每个相似组至少保留 1 张照片。", code="burst_keep_empty"
        )

    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("BEGIN IMMEDIATE")
        _validate_burst_pick_template(connection, template_id=template_id)
        assert_no_running_export(workspace_context, connection=connection)
        run_row = _latest_burst_pick_run_row(connection, template_id=template_id)
        if run_row is None or str(run_row["status"]) != "completed":
            raise ExportTemplateValidationError("连拍挑选仍在处理，请刷新后重试。", code="burst_not_ready")

        group_row = connection.execute(
            """
            SELECT id
            FROM export_burst_pick_group
            WHERE run_id = ?
              AND group_key = ?
              AND submitted_at IS NULL
            """,
            (int(run_row["id"]), group_key),
        ).fetchone()
        if group_row is None:
            raise ExportTemplateValidationError("连拍组已变化，请刷新后重试。", code="burst_groups_stale")

        asset_rows = connection.execute(
            """
            SELECT ga.asset_id, eaa.asset_id AS abandoned_asset_id
            FROM export_burst_pick_group_asset ga
            LEFT JOIN export_abandoned_asset eaa ON eaa.asset_id = ga.asset_id
            WHERE ga.group_id = ?
            ORDER BY ga.position ASC
            """,
            (int(group_row["id"]),),
        ).fetchall()
        if not asset_rows:
            raise ExportTemplateValidationError("连拍组已变化，请刷新后重试。", code="burst_groups_stale")
        if any(row["abandoned_asset_id"] is not None for row in asset_rows):
            raise ExportTemplateValidationError("连拍组已变化，请刷新后重试。", code="burst_groups_stale")

        group_asset_ids = {int(row["asset_id"]) for row in asset_rows}
        if not keep_ids.issubset(group_asset_ids):
            raise ExportTemplateValidationError("保留照片不属于当前连拍组。", code="burst_keep_outside_group")

        kept_asset_ids = set(keep_ids)
        abandoned_by_asset = {
            asset_id: group_key
            for asset_id in sorted(group_asset_ids - keep_ids)
        }
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

        connection.execute(
            """
            UPDATE export_burst_pick_group
            SET submitted_at = ?
            WHERE id = ?
            """,
            (utc_now_text(), int(group_row["id"])),
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
    delay_text = os.environ.get("HIKBOX_TEST_BURST_PICK_FINGERPRINT_DELAY_SECONDS")
    if delay_text:
        time.sleep(float(delay_text))
    if not path.is_file():
        raise FileNotFoundError(str(path))

    try:
        raw_image = Image.open(path)
        exif = raw_image.getexif()
        image = ImageOps.exif_transpose(raw_image).convert("RGB")
    except Exception as exc:
        raise ValueError(f"图片无法解码：{path}") from exc

    luminance = image.convert("L")
    width, height = image.size

    return _VisualFingerprint(
        dhash_bits=_compute_dhash_bits(luminance),
        global_phash_bits=_compute_phash_bits(luminance, image_size=32, coefficient_size=8),
        center_phash_bits=_compute_phash_bits(
            _center_luminance(image),
            image_size=32,
            coefficient_size=8,
        ),
        block_phash_bits=_compute_block_phash_bits(image),
        event_time=_extract_event_time(path, exif),
        normalized_device=_extract_normalized_device(exif),
        width=width,
        height=height,
        file_size=path.stat().st_size,
    )


def _compute_dhash_bits(luminance: Image.Image) -> tuple[int, ...]:
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
    return tuple(dhash_bits)


def _center_luminance(image: Image.Image) -> Image.Image:
    width, height = image.size
    crop_width = max(1, round(width * 0.75))
    crop_height = max(1, round(height * 0.75))
    left = (width - crop_width) // 2
    top = (height - crop_height) // 2
    return image.crop((left, top, left + crop_width, top + crop_height)).convert("L")


def _compute_block_phash_bits(image: Image.Image) -> tuple[tuple[int, ...], ...]:
    width, height = image.size
    blocks: list[tuple[int, ...]] = []
    for row in range(4):
        for col in range(4):
            left = math.floor(col * width / 4)
            right = max(left + 1, math.floor((col + 1) * width / 4))
            top = math.floor(row * height / 4)
            bottom = max(top + 1, math.floor((row + 1) * height / 4))
            block = image.crop((left, top, min(right, width), min(bottom, height))).convert("L")
            blocks.append(_compute_phash_bits(block, image_size=16, coefficient_size=4))
    return tuple(blocks)


def _compute_phash_bits(
    luminance: Image.Image,
    *,
    image_size: int,
    coefficient_size: int,
) -> tuple[int, ...]:
    resized = luminance.resize((image_size, image_size), Image.Resampling.BILINEAR)
    pixels = tuple(float(value) for value in resized.getdata())
    coefficients: list[float] = []
    for v in range(coefficient_size):
        for u in range(coefficient_size):
            if u == 0 and v == 0:
                continue
            coefficients.append(_dct_coefficient(pixels, image_size, u, v))
    median = sorted(coefficients)[len(coefficients) // 2]
    return tuple(1 if value > median else 0 for value in coefficients)


@lru_cache(maxsize=None)
def _dct_cosines(size: int) -> tuple[tuple[float, ...], ...]:
    return tuple(
        tuple(math.cos(math.pi * (2 * x + 1) * frequency / (2 * size)) for x in range(size))
        for frequency in range(size)
    )


def _dct_coefficient(
    pixels: tuple[float, ...],
    size: int,
    u: int,
    v: int,
) -> float:
    alpha_u = math.sqrt(1.0 / size) if u == 0 else math.sqrt(2.0 / size)
    alpha_v = math.sqrt(1.0 / size) if v == 0 else math.sqrt(2.0 / size)
    cosines = _dct_cosines(size)
    total = 0.0
    for y in range(size):
        row_offset = y * size
        cos_y = cosines[v][y]
        for x in range(size):
            total += pixels[row_offset + x] * cosines[u][x] * cos_y
    return alpha_u * alpha_v * total


def _extract_event_time(path: Path, exif: object) -> datetime:
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
    return datetime.utcfromtimestamp(path.stat().st_mtime)


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
    metrics = _pair_metrics(first, second)
    classification = _classify_pair(metrics)
    if classification is None:
        return None
    asset_ids = tuple(sorted((first_id, second_id)))
    return BurstPickEdge(
        asset_ids=(int(asset_ids[0]), int(asset_ids[1])),
        edge_type=classification[0],
        confidence=classification[1],
        phash_hamming=metrics.phash_hamming,
        dhash_hamming=metrics.dhash_hamming,
        center_phash_hamming=metrics.center_phash_hamming,
        block_match_ratio=metrics.block_match_ratio,
        capture_time_delta_seconds=metrics.capture_time_delta_seconds,
        normalized_device_match=metrics.normalized_device_match,
    )


def _pair_metrics(first: _VisualFingerprint, second: _VisualFingerprint) -> _PairMetrics:
    capture_delta: float | None = None
    if first.event_time is not None and second.event_time is not None:
        capture_delta = abs((first.event_time - second.event_time).total_seconds())

    normalized_device_match: bool | None = None
    if first.normalized_device is not None and second.normalized_device is not None:
        normalized_device_match = first.normalized_device == second.normalized_device

    return _PairMetrics(
        dhash_hamming=_hamming(first.dhash_bits, second.dhash_bits),
        phash_hamming=_hamming(first.global_phash_bits, second.global_phash_bits),
        center_phash_hamming=_hamming(first.center_phash_bits, second.center_phash_bits),
        block_match_ratio=_block_match_ratio(first.block_phash_bits, second.block_phash_bits),
        capture_time_delta_seconds=capture_delta,
        normalized_device_match=normalized_device_match,
    )


def _hamming(first: tuple[int, ...], second: tuple[int, ...]) -> int:
    return sum(a != b for a, b in zip(first, second))


def _block_match_ratio(
    first_blocks: tuple[tuple[int, ...], ...],
    second_blocks: tuple[tuple[int, ...], ...],
) -> float:
    def directional_ratio(
        source: tuple[tuple[int, ...], ...],
        target: tuple[tuple[int, ...], ...],
    ) -> float:
        matched = 0
        for block in source:
            if min(_hamming(block, other) for other in target) <= 4:
                matched += 1
        return matched / 16.0

    return min(directional_ratio(first_blocks, second_blocks), directional_ratio(second_blocks, first_blocks))


def _classify_pair(metrics: _PairMetrics) -> tuple[str, float] | None:
    if _matches_exact_duplicate(metrics):
        return ("exact_duplicate", _confidence_exact(metrics))
    if _matches_edited_duplicate(metrics):
        return ("edited_duplicate", _confidence_edited(metrics))
    if _matches_burst_duplicate(metrics):
        return ("burst_duplicate", _confidence_burst(metrics))
    return None


def _matches_exact_duplicate(metrics: _PairMetrics) -> bool:
    return (
        metrics.phash_hamming <= 4
        and metrics.dhash_hamming <= 8
        and metrics.center_phash_hamming <= 6
    ) or (
        metrics.phash_hamming <= 3
        and metrics.block_match_ratio >= 0.875
    )


def _matches_edited_duplicate(metrics: _PairMetrics) -> bool:
    return (
        metrics.phash_hamming <= 12
        and metrics.center_phash_hamming <= 14
        and metrics.block_match_ratio >= 0.50
    ) or (
        metrics.center_phash_hamming <= 10
        and metrics.block_match_ratio >= 0.50
        and metrics.dhash_hamming <= 32
    ) or (
        metrics.block_match_ratio >= 0.625
        and (
            metrics.phash_hamming <= 18
            or metrics.center_phash_hamming <= 18
        )
    )


def _matches_burst_duplicate(metrics: _PairMetrics) -> bool:
    delta = metrics.capture_time_delta_seconds
    if delta is None:
        return False
    if delta <= 10:
        visual_matches = sum(
            (
                metrics.dhash_hamming <= 30,
                metrics.phash_hamming <= 28,
                metrics.center_phash_hamming <= 26,
                metrics.block_match_ratio >= 0.3125,
            )
        )
        return visual_matches >= 2
    if delta <= 60:
        strong_visual = (
            metrics.phash_hamming <= 16
            or metrics.center_phash_hamming <= 14
            or metrics.block_match_ratio >= 0.50
        )
        auxiliary_visual = metrics.dhash_hamming <= 30 or metrics.block_match_ratio >= 0.50
        return strong_visual and auxiliary_visual
    return False


def _confidence_exact(metrics: _PairMetrics) -> float:
    penalty = (
        metrics.phash_hamming / 63.0 * 0.02
        + metrics.dhash_hamming / 128.0 * 0.02
        + metrics.center_phash_hamming / 63.0 * 0.01
    )
    return max(0.95, min(1.0, 0.99 - penalty + metrics.block_match_ratio * 0.01))


def _confidence_edited(metrics: _PairMetrics) -> float:
    visual_bonus = min(0.08, metrics.block_match_ratio * 0.08)
    hamming_penalty = min(0.04, (metrics.phash_hamming + metrics.center_phash_hamming) / 126.0 * 0.04)
    return max(0.85, min(0.94, 0.86 + visual_bonus - hamming_penalty))


def _confidence_burst(metrics: _PairMetrics) -> float:
    delta = metrics.capture_time_delta_seconds
    if delta is not None and delta > 10:
        base = 0.82
    else:
        base = 0.78
    visual_matches = sum(
        (
            metrics.dhash_hamming <= 30,
            metrics.phash_hamming <= 28,
            metrics.center_phash_hamming <= 26,
            metrics.block_match_ratio >= 0.3125,
        )
    )
    device_bonus = 0.03 if metrics.normalized_device_match is True else 0.0
    return max(base, min(0.94, base + visual_matches * 0.025 + device_bonus))


def _connected_burst_groups(
    *,
    display_assets: dict[int, BurstPickAsset],
    fingerprints: dict[int, _VisualFingerprint],
    edges: list[BurstPickEdge],
) -> list[BurstPickGroup]:
    validated_components = _validated_strong_components(
        asset_ids=sorted(display_assets),
        edges=edges,
        fingerprints=fingerprints,
    )

    groups: list[BurstPickGroup] = []
    for asset_ids, component_edges in validated_components:
        if len(asset_ids) < 2:
            continue
        sorted_asset_ids = sorted(asset_ids)
        group_edges = sorted(
            component_edges,
            key=lambda edge: (edge.asset_ids[0], edge.asset_ids[1], edge.edge_type),
        )
        groups.append(
            BurstPickGroup(
                group_key="g_" + "_".join(str(asset_id) for asset_id in sorted_asset_ids),
                assets=[display_assets[asset_id] for asset_id in sorted_asset_ids],
                edges=group_edges,
            )
        )
    return sorted(groups, key=lambda group: group.assets[0].asset_id)


def _validated_strong_components(
    *,
    asset_ids: list[int],
    edges: list[BurstPickEdge],
    fingerprints: dict[int, _VisualFingerprint],
) -> list[tuple[list[int], list[BurstPickEdge]]]:
    components = _edge_components(asset_ids=asset_ids, edges=edges)
    validated: list[tuple[list[int], list[BurstPickEdge]]] = []
    for component_asset_ids, component_edges in components:
        if len(component_asset_ids) < 2:
            continue
        if _component_is_valid(component_asset_ids, component_edges, fingerprints):
            validated.append((component_asset_ids, component_edges))
            continue
        if len(component_edges) <= 1:
            continue
        pruned_edges = sorted(
            component_edges,
            key=lambda edge: (edge.confidence, edge.asset_ids[0], edge.asset_ids[1], edge.edge_type),
        )[1:]
        validated.extend(
            _validated_strong_components(
                asset_ids=component_asset_ids,
                edges=pruned_edges,
                fingerprints=fingerprints,
            )
        )
    return validated


def _edge_components(
    *,
    asset_ids: list[int],
    edges: list[BurstPickEdge],
) -> list[tuple[list[int], list[BurstPickEdge]]]:
    parent = {asset_id: asset_id for asset_id in asset_ids}

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
    for asset_id in asset_ids:
        component_assets[find(asset_id)].append(asset_id)

    edges_by_component: dict[int, list[BurstPickEdge]] = defaultdict(list)
    for edge in edges:
        edges_by_component[find(edge.asset_ids[0])].append(edge)

    return [
        (sorted(component_ids), edges_by_component[root])
        for root, component_ids in sorted(component_assets.items())
        if len(component_ids) >= 2
    ]


def _component_is_valid(
    asset_ids: list[int],
    edges: list[BurstPickEdge],
    fingerprints: dict[int, _VisualFingerprint],
) -> bool:
    if len(asset_ids) < 2 or not edges:
        return False
    possible_edges = len(asset_ids) * (len(asset_ids) - 1) / 2
    density = len(edges) / possible_edges
    if density < _component_min_strong_edge_density(len(asset_ids)):
        return False

    main_type = _component_main_edge_type(edges)
    medoid = _component_medoid(asset_ids, edges)
    for asset_id in asset_ids:
        if asset_id == medoid:
            continue
        metrics = _pair_metrics(fingerprints[medoid], fingerprints[asset_id])
        if main_type in {"exact_duplicate", "edited_duplicate"}:
            if not (
                metrics.phash_hamming <= 18
                or metrics.center_phash_hamming <= 18
                or metrics.block_match_ratio >= 0.50
            ):
                return False
        else:
            if not (
                metrics.phash_hamming <= 32
                or metrics.center_phash_hamming <= 30
                or metrics.block_match_ratio >= 0.25
            ):
                return False
            if (
                metrics.capture_time_delta_seconds is not None
                and metrics.capture_time_delta_seconds > 300
            ):
                return False

    edge_type_counts = Counter(edge.edge_type for edge in edges)
    if edge_type_counts["burst_duplicate"] >= max(
        edge_type_counts["exact_duplicate"],
        edge_type_counts["edited_duplicate"],
    ):
        event_times = [
            fingerprints[asset_id].event_time
            for asset_id in asset_ids
            if fingerprints[asset_id].event_time is not None
        ]
        if event_times and (max(event_times) - min(event_times)).total_seconds() > 300:
            return False
    return True


def _component_min_strong_edge_density(asset_count: int) -> float:
    if asset_count <= 5:
        return 0.40
    return 0.05


def _component_main_edge_type(edges: list[BurstPickEdge]) -> str:
    counts = Counter(edge.edge_type for edge in edges)
    return sorted(
        counts,
        key=lambda edge_type: (
            -counts[edge_type],
            {"exact_duplicate": 0, "edited_duplicate": 1, "burst_duplicate": 2}.get(edge_type, 99),
        ),
    )[0]


def _component_medoid(asset_ids: list[int], edges: list[BurstPickEdge]) -> int:
    direct_edge_count = Counter()
    confidence_sum = defaultdict(float)
    for edge in edges:
        first_id, second_id = edge.asset_ids
        direct_edge_count[first_id] += 1
        direct_edge_count[second_id] += 1
        confidence_sum[first_id] += edge.confidence
        confidence_sum[second_id] += edge.confidence
    return sorted(
        asset_ids,
        key=lambda asset_id: (
            -direct_edge_count[asset_id],
            -confidence_sum[asset_id],
            asset_id,
        ),
    )[0]


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
