from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, UnidentifiedImageError

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.repositories import AssetRepo
from hikbox_pictures.services.asset_pipeline import PREVIEW_CONTEXT_REBUILD_FAILED_ERROR
from hikbox_pictures.services.observability_service import ObservabilityService
from hikbox_pictures.services.path_guard import ensure_safe_asset_path
from hikbox_pictures.services.runtime import resolve_media_allowed_roots


class PreviewArtifactError(Exception):
    def __init__(self, *, error_code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.status_code = int(status_code)


class PreviewArtifactService:
    def __init__(self, *, db_path: Path, workspace: Path) -> None:
        self.db_path = Path(db_path)
        self.workspace = Path(workspace)

    def ensure_crop(self, observation_id: int) -> str:
        conn = connect_db(self.db_path)
        try:
            repo = AssetRepo(conn)
            row = repo.get_observation_with_source(int(observation_id))
            if row is None:
                raise LookupError(f"observation {observation_id} 不存在")

            crop_path_raw = row.get("crop_path")
            if crop_path_raw:
                safe_existing = ensure_safe_asset_path(
                    str(crop_path_raw),
                    [str(p) for p in resolve_media_allowed_roots(self.workspace)],
                )
                if safe_existing.exists() and safe_existing.is_file():
                    return str(safe_existing)

            try:
                rebuilt_path = self._rebuild_crop(row)
            except PermissionError:
                raise
            except (UnidentifiedImageError, OSError, ValueError) as exc:
                ObservabilityService(conn, workspace=self.workspace).emit_event(
                    level="warning",
                    component="api",
                    event_type="preview.context.rebuild_failed",
                    message=f"rebuild crop failed for observation={observation_id}",
                    detail={
                        "observation_id": int(observation_id),
                        "error_type": exc.__class__.__name__,
                        "error_message": str(exc),
                    },
                    run_kind=None,
                    run_id=None,
                )
                raise PreviewArtifactError(
                    error_code=PREVIEW_CONTEXT_REBUILD_FAILED_ERROR,
                    message="裁剪图重建失败",
                    status_code=422,
                ) from exc

            updated = repo.update_observation_crop_path(int(observation_id), str(rebuilt_path))
            if updated <= 0:
                raise LookupError(f"observation {observation_id} 不存在")
            conn.commit()

            ObservabilityService(conn, workspace=self.workspace).emit_event(
                level="info",
                component="api",
                event_type="preview.context.rebuild_requested",
                message=f"rebuild crop for observation={observation_id}",
                detail={
                    "observation_id": int(observation_id),
                    "crop_path": str(rebuilt_path),
                },
                run_kind=None,
                run_id=None,
            )
            return str(rebuilt_path)
        finally:
            conn.close()

    def ensure_context(self, observation_id: int) -> str:
        conn = connect_db(self.db_path)
        try:
            repo = AssetRepo(conn)
            row = repo.get_observation_with_source(int(observation_id))
            if row is None:
                raise LookupError(f"observation {observation_id} 不存在")

            context_path = self.workspace / ".hikbox" / "artifacts" / "context" / f"obs-{int(observation_id)}.jpg"
            if context_path.exists() and context_path.is_file():
                return str(context_path)

            try:
                rebuilt_path = self._rebuild_context(row, context_path)
            except PermissionError:
                raise
            except (UnidentifiedImageError, OSError, ValueError) as exc:
                ObservabilityService(conn, workspace=self.workspace).emit_event(
                    level="warning",
                    component="api",
                    event_type="preview.context.rebuild_failed",
                    message=f"rebuild context failed for observation={observation_id}",
                    detail={
                        "observation_id": int(observation_id),
                        "error_type": exc.__class__.__name__,
                        "error_message": str(exc),
                    },
                    run_kind=None,
                    run_id=None,
                )
                raise PreviewArtifactError(
                    error_code=PREVIEW_CONTEXT_REBUILD_FAILED_ERROR,
                    message="上下文预览重建失败",
                    status_code=422,
                ) from exc

            ObservabilityService(conn, workspace=self.workspace).emit_event(
                level="info",
                component="api",
                event_type="preview.context.rebuild_requested",
                message=f"rebuild context for observation={observation_id}",
                detail={
                    "observation_id": int(observation_id),
                    "context_path": str(rebuilt_path),
                },
                run_kind=None,
                run_id=None,
            )
            return str(rebuilt_path)
        finally:
            conn.close()

    def _rebuild_crop(self, row: dict[str, object]) -> Path:
        source_path = ensure_safe_asset_path(
            str(row["primary_path"]),
            [str(p) for p in resolve_media_allowed_roots(self.workspace)],
        )
        if not source_path.exists() or not source_path.is_file():
            raise LookupError(f"媒体文件不存在: {source_path}")

        output_dir = self.workspace / ".hikbox" / "artifacts" / "face-crops" / "rebuilt"
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / f"obs-{int(row['id'])}.jpg"

        with Image.open(source_path) as image:
            width, height = image.size
            left = max(0, min(width - 1, int(float(row["bbox_left"]) * width)))
            top = max(0, min(height - 1, int(float(row["bbox_top"]) * height)))
            right = max(left + 1, min(width, int(float(row["bbox_right"]) * width)))
            bottom = max(top + 1, min(height, int(float(row["bbox_bottom"]) * height)))
            crop = image.crop((left, top, right, bottom)).convert("RGB")
            crop.save(out_path, format="JPEG")
        return out_path

    def _rebuild_context(self, row: dict[str, object], out_path: Path) -> Path:
        source_path = ensure_safe_asset_path(
            str(row["primary_path"]),
            [str(p) for p in resolve_media_allowed_roots(self.workspace)],
        )
        if not source_path.exists() or not source_path.is_file():
            raise LookupError(f"媒体文件不存在: {source_path}")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(source_path) as image:
            width, height = image.size
            left = max(0, min(width - 1, int(float(row["bbox_left"]) * width)))
            top = max(0, min(height - 1, int(float(row["bbox_top"]) * height)))
            right = max(left + 1, min(width, int(float(row["bbox_right"]) * width)))
            bottom = max(top + 1, min(height, int(float(row["bbox_bottom"]) * height)))

            face_w = max(1, right - left)
            face_h = max(1, bottom - top)
            margin_x = max(2, int(face_w * 0.08))
            margin_y = max(2, int(face_h * 0.08))
            ctx_left = max(0, left - margin_x)
            ctx_top = max(0, top - margin_y)
            ctx_right = min(width, right + margin_x)
            ctx_bottom = min(height, bottom + margin_y)

            context = image.crop((ctx_left, ctx_top, ctx_right, ctx_bottom)).convert("RGB")
            box_left = left - ctx_left
            box_top = top - ctx_top
            box_right = right - ctx_left
            box_bottom = bottom - ctx_top

            max_context_side = 32
            current_max_side = max(context.size)
            if current_max_side > max_context_side:
                scale = float(max_context_side) / float(current_max_side)
                resized_size = (
                    max(1, int(context.width * scale)),
                    max(1, int(context.height * scale)),
                )
                context = context.resize(resized_size)
                box_left = int(box_left * scale)
                box_top = int(box_top * scale)
                box_right = int(box_right * scale)
                box_bottom = int(box_bottom * scale)

            draw = ImageDraw.Draw(context)
            line_width = max(2, min(context.size) // 64)
            draw.rectangle((box_left, box_top, box_right - 1, box_bottom - 1), outline=(255, 64, 64), width=line_width)
            context.save(
                out_path,
                format="JPEG",
                quality=68,
                optimize=True,
                progressive=True,
            )
        return out_path
