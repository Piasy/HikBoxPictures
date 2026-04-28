from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import shutil
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


# Test-only hook called after each file copy during export.
_per_file_copy_hook: callable | None = None


def set_per_file_copy_hook(hook: callable | None) -> None:
    global _per_file_copy_hook
    _per_file_copy_hook = hook


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


@dataclass(frozen=True)
class PreviewMonthBucket:
    month: str
    only_assets: list[PreviewAsset]
    group_assets: list[PreviewAsset]


@dataclass(frozen=True)
class PreviewResult:
    total_count: int
    only_count: int
    group_count: int
    month_buckets: list[PreviewMonthBucket]


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
            INNER JOIN face_observations fo ON fo.asset_id = a.id
            LEFT JOIN person_face_assignments pfa
              ON pfa.face_observation_id = fo.id AND pfa.active = 1
            ORDER BY a.id, fo.id
            """,
            (template_id,),
        ).fetchall()
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
        )

        month = asset["capture_month"] if asset["capture_month"] else "unknown-date"
        months[month][bucket].append(asset_preview)
        total_count += 1
        if bucket == "only":
            only_count += 1
        else:
            group_count += 1

    sorted_months = []
    for month in sorted(months.keys()):
        month_data = months[month]
        sorted_months.append(
            PreviewMonthBucket(
                month=month,
                only_assets=sorted(month_data["only"], key=lambda a: a.file_name),
                group_assets=sorted(month_data["group"], key=lambda a: a.file_name),
            )
        )

    return PreviewResult(
        total_count=total_count,
        only_count=only_count,
        group_count=group_count,
        month_buckets=sorted_months,
    )


def _resolve_asset_month(asset: dict[str, object]) -> str:
    capture_month = str(asset.get("capture_month", "")).strip()
    if capture_month:
        return capture_month
    absolute_path = str(asset.get("absolute_path", ""))
    if absolute_path:
        try:
            mtime = Path(absolute_path).stat().st_mtime
            return datetime.fromtimestamp(mtime).strftime("%Y-%m")
        except (OSError, ValueError):
            pass
    return "unknown-date"


def _copy_asset(
    asset: dict[str, object],
    bucket: str,
    output_root: Path,
) -> tuple[str, str]:
    absolute_path = str(asset["absolute_path"])
    file_name = str(asset["file_name"])
    file_extension = str(asset.get("file_extension", "")).lower()
    live_photo_mov_path = asset.get("live_photo_mov_path")

    month = _resolve_asset_month(asset)
    bucket_dir = output_root / bucket / month
    bucket_dir.mkdir(parents=True, exist_ok=True)

    src_path = Path(absolute_path)
    dst_path = bucket_dir / file_name

    if dst_path.exists():
        return "skipped_exists", "not_applicable"

    shutil.copy2(src_path, dst_path)

    mov_result = "not_applicable"
    if file_extension in ("heic", "heif") and live_photo_mov_path:
        mov_src = Path(str(live_photo_mov_path))
        if mov_src.exists():
            mov_dst = bucket_dir / mov_src.name
            if not mov_dst.exists():
                shutil.copy2(mov_src, mov_dst)
            mov_result = "copied"
        else:
            mov_result = "skipped_missing"

    return "copied", mov_result


def execute_export(
    workspace_context: WorkspaceContext,
    *,
    template_id: str,
) -> int:
    connection = sqlite3.connect(workspace_context.library_db_path)
    connection.row_factory = sqlite3.Row
    try:
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

        running = connection.execute(
            "SELECT 1 FROM export_run WHERE status = 'running' LIMIT 1"
        ).fetchone()
        if running is not None:
            connection.rollback()
            raise ExportTemplateValidationError(
                "已有导出正在进行中。", code="export_in_progress"
            )

        output_root = Path(str(template_row["output_root"]))
        now = utc_now_text()

        connection.execute(
            """
            INSERT INTO export_run (template_id, status, started_at)
            VALUES (?, 'running', ?)
            """,
            (template_id, now),
        )
        run_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        connection.commit()
    except ExportTemplateValidationError:
        raise
    except sqlite3.Error as exc:
        raise ExportTemplateError("导出启动失败。") from exc
    finally:
        connection.close()

    copied_count = 0
    skipped_count = 0
    status = "completed"

    try:
        preview = compute_export_preview(workspace_context, template_id=template_id)

        conn = sqlite3.connect(workspace_context.library_db_path)
        conn.row_factory = sqlite3.Row
        try:
            for month_bucket in preview.month_buckets:
                for bucket_name in ("only", "group"):
                    assets = getattr(month_bucket, f"{bucket_name}_assets")
                    for asset in assets:
                        asset_row = conn.execute(
                            """
                            SELECT file_name, absolute_path, file_extension,
                                   capture_month, live_photo_mov_path
                            FROM assets WHERE id = ?
                            """,
                            (asset.asset_id,),
                        ).fetchone()

                        if asset_row is None:
                            continue

                        asset_dict = {
                            "file_name": str(asset_row["file_name"]),
                            "absolute_path": str(asset_row["absolute_path"]),
                            "file_extension": str(asset_row["file_extension"]),
                            "capture_month": str(asset_row["capture_month"]) if asset_row["capture_month"] else "",
                            "live_photo_mov_path": asset_row["live_photo_mov_path"],
                        }

                        result, mov_result = _copy_asset(
                            asset_dict, bucket_name, output_root
                        )

                        target_path = str(
                            output_root
                            / bucket_name
                            / _resolve_asset_month(asset_dict)
                            / asset_dict["file_name"]
                        )

                        conn.execute(
                            """
                            INSERT INTO export_delivery
                            (run_id, asset_id, target_path, result, mov_result)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (run_id, asset.asset_id, target_path, result, mov_result),
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
    except sqlite3.Error as exc:
        raise ExportTemplateError("导出历史读取失败。") from exc
    finally:
        connection.close()

    return [
        ExportRunListItem(
            run_id=int(row["run_id"]),
            template_id=str(row["template_id"]),
            status=str(row["status"]),
            started_at=str(row["started_at"]),
            completed_at=str(row["completed_at"]) if row["completed_at"] else None,
            copied_count=int(row["copied_count"]),
            skipped_count=int(row["skipped_count"]),
        )
        for row in rows
    ]


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
