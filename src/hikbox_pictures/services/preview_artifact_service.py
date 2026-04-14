from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, UnidentifiedImageError

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.image_io import load_oriented_image
from hikbox_pictures.repositories import AssetRepo
from hikbox_pictures.services.asset_pipeline import PREVIEW_CONTEXT_REBUILD_FAILED_ERROR
from hikbox_pictures.services.observability_service import ObservabilityService
from hikbox_pictures.services.path_guard import ensure_safe_asset_path

LEGACY_CONTEXT_MAX_SIDE = 48
CONTEXT_PREVIEW_MIN_SIDE = 160
CONTEXT_PREVIEW_MAX_SIDE = 320
CONTEXT_PREVIEW_MARGIN_FACTOR = 1.0
CONTEXT_PREVIEW_MIN_AREA_RATIO = 2.5


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
                    [str(path) for path in self._allowed_roots_for_row(row)],
                )
                if safe_existing.exists() and safe_existing.is_file() and self._is_crop_artifact_usable(row, safe_existing):
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
            if context_path.exists() and context_path.is_file() and self._is_context_artifact_usable(row, context_path):
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

    def _is_crop_artifact_usable(self, row: dict[str, object], crop_path: Path) -> bool:
        try:
            expected_width, expected_height = self._expected_crop_size(row)
            with Image.open(crop_path) as image:
                actual_size = image.size
            return self._sizes_close(actual_size, (expected_width, expected_height), tolerance=1)
        except (LookupError, UnidentifiedImageError, OSError, ValueError):
            return False

    def _is_context_artifact_usable(self, row: dict[str, object], context_path: Path) -> bool:
        try:
            expected_context_size, expected_bbox_size = self._expected_context_geometry(row)
            with Image.open(context_path) as image:
                rgb = image.convert("RGB")
                if max(rgb.size) <= LEGACY_CONTEXT_MAX_SIDE:
                    return False
                if not self._context_has_meaningful_scene(rgb):
                    return False
                if not self._sizes_close(rgb.size, expected_context_size, tolerance=2):
                    return False
                bounds = self._find_bbox_highlight_bounds(rgb)
                if bounds is None:
                    return False
                actual_bbox_size = (
                    bounds[2] - bounds[0] + 1,
                    bounds[3] - bounds[1] + 1,
                )
                tolerance = max(4, min(rgb.size) // 32)
                return self._sizes_close(actual_bbox_size, expected_bbox_size, tolerance=tolerance)
        except (LookupError, UnidentifiedImageError, OSError, ValueError):
            return False

    def _context_has_meaningful_scene(self, image: Image.Image) -> bool:
        bounds = self._find_bbox_highlight_bounds(image)
        if bounds is None:
            return False
        min_x, min_y, max_x, max_y = bounds
        bbox_width = max_x - min_x + 1
        bbox_height = max_y - min_y + 1
        if bbox_width <= 0 or bbox_height <= 0:
            return False
        area_ratio = float(image.width * image.height) / float(bbox_width * bbox_height)
        return area_ratio >= CONTEXT_PREVIEW_MIN_AREA_RATIO

    def _find_bbox_highlight_bounds(self, image: Image.Image) -> tuple[int, int, int, int] | None:
        pixels = image.load()
        min_x = image.width
        min_y = image.height
        max_x = -1
        max_y = -1
        for y in range(image.height):
            for x in range(image.width):
                r, g, b = pixels[x, y]
                if r >= 180 and g <= 130 and b <= 130:
                    min_x = min(min_x, x)
                    min_y = min(min_y, y)
                    max_x = max(max_x, x)
                    max_y = max(max_y, y)
        if max_x < 0 or max_y < 0:
            return None
        return (min_x, min_y, max_x, max_y)

    def _rebuild_crop(self, row: dict[str, object]) -> Path:
        output_dir = self.workspace / ".hikbox" / "artifacts" / "face-crops" / "rebuilt"
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / f"obs-{int(row['id'])}.jpg"

        image = self._load_source_image(row)
        left, top, right, bottom = self._resolve_bbox_pixels(
            row,
            width=image.width,
            height=image.height,
        )
        crop = image.crop((left, top, right, bottom)).convert("RGB")
        crop.save(out_path, format="JPEG")
        return out_path

    def _rebuild_context(self, row: dict[str, object], out_path: Path) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        image = self._load_source_image(row)
        left, top, right, bottom = self._resolve_bbox_pixels(
            row,
            width=image.width,
            height=image.height,
        )

        face_w = max(1, right - left)
        face_h = max(1, bottom - top)
        # context 必须明显大于 crop，才能在审核页形成可判断的中间层证据。
        margin_x = max(2, int(face_w * CONTEXT_PREVIEW_MARGIN_FACTOR))
        margin_y = max(2, int(face_h * CONTEXT_PREVIEW_MARGIN_FACTOR))
        ctx_left = max(0, left - margin_x)
        ctx_top = max(0, top - margin_y)
        ctx_right = min(image.width, right + margin_x)
        ctx_bottom = min(image.height, bottom + margin_y)

        context = image.crop((ctx_left, ctx_top, ctx_right, ctx_bottom)).convert("RGB")
        box_left = left - ctx_left
        box_top = top - ctx_top
        box_right = right - ctx_left
        box_bottom = bottom - ctx_top

        max_context_side = CONTEXT_PREVIEW_MAX_SIDE
        current_max_side = max(context.size)
        target_max_side = current_max_side
        if current_max_side > max_context_side:
            target_max_side = max_context_side
        elif current_max_side < CONTEXT_PREVIEW_MIN_SIDE:
            target_max_side = CONTEXT_PREVIEW_MIN_SIDE

        if target_max_side != current_max_side:
            scale = float(target_max_side) / float(current_max_side)
            resized_size = (
                max(1, int(context.width * scale)),
                max(1, int(context.height * scale)),
            )
            context = context.resize(resized_size, resample=Image.Resampling.LANCZOS)
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

    def _resolve_source_path(self, row: dict[str, object]) -> Path:
        source_path = ensure_safe_asset_path(
            str(row["primary_path"]),
            [str(path) for path in self._allowed_roots_for_row(row)],
        )
        if not source_path.exists() or not source_path.is_file():
            raise LookupError(f"媒体文件不存在: {source_path}")
        return source_path

    def _allowed_roots_for_row(self, row: dict[str, object]) -> list[Path]:
        roots: list[Path] = [self._artifacts_root()]
        source_root = row.get("source_root_path")
        if source_root not in (None, ""):
            roots.append(Path(str(source_root)).expanduser().resolve())
        return self._dedupe_paths(roots)

    def _artifacts_root(self) -> Path:
        return (self.workspace / ".hikbox" / "artifacts").resolve()

    @staticmethod
    def _dedupe_paths(paths: list[Path]) -> list[Path]:
        deduped: list[Path] = []
        seen: set[str] = set()
        for path in paths:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(path)
        return deduped

    def _load_source_image(self, row: dict[str, object]) -> Image.Image:
        return load_oriented_image(self._resolve_source_path(row))

    @staticmethod
    def _resolve_bbox_pixels(
        row: dict[str, object],
        *,
        width: int,
        height: int,
    ) -> tuple[int, int, int, int]:
        left = max(0, min(width - 1, int(float(row["bbox_left"]) * width)))
        top = max(0, min(height - 1, int(float(row["bbox_top"]) * height)))
        right = max(left + 1, min(width, int(float(row["bbox_right"]) * width)))
        bottom = max(top + 1, min(height, int(float(row["bbox_bottom"]) * height)))
        return left, top, right, bottom

    def _expected_crop_size(self, row: dict[str, object]) -> tuple[int, int]:
        image = self._load_source_image(row)
        left, top, right, bottom = self._resolve_bbox_pixels(
            row,
            width=image.width,
            height=image.height,
        )
        return right - left, bottom - top

    def _expected_context_geometry(self, row: dict[str, object]) -> tuple[tuple[int, int], tuple[int, int]]:
        image = self._load_source_image(row)
        left, top, right, bottom = self._resolve_bbox_pixels(
            row,
            width=image.width,
            height=image.height,
        )

        face_w = max(1, right - left)
        face_h = max(1, bottom - top)
        margin_x = max(2, int(face_w * CONTEXT_PREVIEW_MARGIN_FACTOR))
        margin_y = max(2, int(face_h * CONTEXT_PREVIEW_MARGIN_FACTOR))
        ctx_left = max(0, left - margin_x)
        ctx_top = max(0, top - margin_y)
        ctx_right = min(image.width, right + margin_x)
        ctx_bottom = min(image.height, bottom + margin_y)

        context_width = max(1, ctx_right - ctx_left)
        context_height = max(1, ctx_bottom - ctx_top)
        box_left = left - ctx_left
        box_top = top - ctx_top
        box_right = right - ctx_left
        box_bottom = bottom - ctx_top

        current_max_side = max(context_width, context_height)
        target_max_side = current_max_side
        if current_max_side > CONTEXT_PREVIEW_MAX_SIDE:
            target_max_side = CONTEXT_PREVIEW_MAX_SIDE
        elif current_max_side < CONTEXT_PREVIEW_MIN_SIDE:
            target_max_side = CONTEXT_PREVIEW_MIN_SIDE

        if target_max_side != current_max_side:
            scale = float(target_max_side) / float(current_max_side)
            context_width = max(1, int(context_width * scale))
            context_height = max(1, int(context_height * scale))
            box_left = int(box_left * scale)
            box_top = int(box_top * scale)
            box_right = int(box_right * scale)
            box_bottom = int(box_bottom * scale)

        return (
            (context_width, context_height),
            (max(1, box_right - box_left), max(1, box_bottom - box_top)),
        )

    @staticmethod
    def _sizes_close(actual: tuple[int, int], expected: tuple[int, int], *, tolerance: int) -> bool:
        return (
            abs(int(actual[0]) - int(expected[0])) <= int(tolerance)
            and abs(int(actual[1]) - int(expected[1])) <= int(tolerance)
        )
