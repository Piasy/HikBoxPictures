from __future__ import annotations

from dataclasses import dataclass
import mimetypes
from pathlib import Path
from typing import Callable, Iterator

from PIL import UnidentifiedImageError

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.repositories import AssetRepo
from hikbox_pictures.services.asset_pipeline import (
    PREVIEW_ASSET_DECODE_FAILED_ERROR,
    PREVIEW_ASSET_MISSING_ERROR,
)
from hikbox_pictures.services.observability_service import ObservabilityService
from hikbox_pictures.services.path_guard import ensure_safe_asset_path
from hikbox_pictures.services.preview_artifact_service import PreviewArtifactError, PreviewArtifactService
from hikbox_pictures.services.runtime import resolve_media_allowed_roots
from hikbox_pictures.workspace import load_workspace_paths


class MediaRangeError(Exception):
    def __init__(self, *, total_size: int, message: str = "无效的 Range 请求") -> None:
        super().__init__(message)
        self.total_size = int(total_size)
        self.message = message


class MediaBusinessError(Exception):
    def __init__(self, *, status_code: int, error_code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class MediaStreamPayload:
    file_path: Path
    status_code: int
    media_type: str
    headers: dict[str, str]
    start: int
    end: int

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        with self.file_path.open("rb") as handle:
            handle.seek(self.start)
            remaining = self.end - self.start + 1
            while remaining > 0:
                to_read = min(chunk_size, remaining)
                block = handle.read(to_read)
                if not block:
                    break
                remaining -= len(block)
                yield block


class MediaPreviewService:
    def __init__(
        self,
        *,
        db_path: Path,
        workspace: Path,
        allowed_roots_resolver: Callable[[Path], list[Path]] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.workspace = Path(workspace)
        self.paths = load_workspace_paths(self.workspace)
        self.allowed_roots_resolver = allowed_roots_resolver or resolve_media_allowed_roots
        self.preview_artifact_service = PreviewArtifactService(db_path=self.db_path, workspace=self.workspace)

    def read_original_stream(self, photo_id: int, range_header: str | None = None) -> MediaStreamPayload:
        conn = connect_db(self.db_path)
        try:
            repo = AssetRepo(conn)
            row = repo.get_photo_media(int(photo_id))
        finally:
            conn.close()

        if row is None:
            self._emit_event(
                level="warning",
                event_type=PREVIEW_ASSET_MISSING_ERROR,
                message=f"photo not found: {photo_id}",
                detail={"photo_id": int(photo_id)},
            )
            raise MediaBusinessError(
                status_code=404,
                error_code=PREVIEW_ASSET_MISSING_ERROR,
                message="原图不存在或不可用",
            )

        source_path = str(row["primary_path"])
        try:
            return self._build_stream_payload(
                source_path=source_path,
                range_header=range_header,
                allowed_roots=self._allowed_roots_for_source_row(row),
            )
        except LookupError:
            self._emit_event(
                level="warning",
                event_type=PREVIEW_ASSET_MISSING_ERROR,
                message=f"photo file missing: {photo_id}",
                detail={"photo_id": int(photo_id), "path": source_path},
            )
            raise MediaBusinessError(
                status_code=404,
                error_code=PREVIEW_ASSET_MISSING_ERROR,
                message="原图不存在或不可用",
            )

    def read_preview_stream(self, photo_id: int) -> MediaStreamPayload:
        payload = self.read_original_stream(photo_id, range_header=None)
        try:
            preview_path = self.preview_artifact_service.ensure_photo_preview(
                photo_id=int(photo_id),
                source_path=payload.file_path,
            )
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            self._emit_event(
                level="warning",
                event_type=PREVIEW_ASSET_DECODE_FAILED_ERROR,
                message=f"preview decode failed: {photo_id}",
                detail={
                    "photo_id": int(photo_id),
                    "path": str(payload.file_path),
                    "error_type": exc.__class__.__name__,
                    "error_message": str(exc),
                },
            )
            raise MediaBusinessError(
                status_code=422,
                error_code=PREVIEW_ASSET_DECODE_FAILED_ERROR,
                message="预览解码失败",
            ) from exc
        return self._build_stream_payload(
            source_path=preview_path,
            range_header=None,
            allowed_roots=self._artifact_allowed_roots(),
        )

    def read_observation_crop(self, observation_id: int) -> MediaStreamPayload:
        try:
            crop_path = self.preview_artifact_service.ensure_crop(int(observation_id))
        except PreviewArtifactError as exc:
            raise MediaBusinessError(
                status_code=exc.status_code,
                error_code=exc.error_code,
                message=exc.message,
            ) from exc
        except LookupError:
            raise
        return self._build_stream_payload(
            source_path=crop_path,
            range_header=None,
            allowed_roots=self._artifact_allowed_roots(),
        )

    def read_observation_context(self, observation_id: int) -> MediaStreamPayload:
        try:
            context_path = self.preview_artifact_service.ensure_context(int(observation_id))
        except PreviewArtifactError as exc:
            raise MediaBusinessError(
                status_code=exc.status_code,
                error_code=exc.error_code,
                message=exc.message,
            ) from exc
        except LookupError:
            raise
        return self._build_stream_payload(
            source_path=context_path,
            range_header=None,
            allowed_roots=self._artifact_allowed_roots(),
        )

    def _build_stream_payload(
        self,
        *,
        source_path: str,
        range_header: str | None,
        allowed_roots: list[Path] | None = None,
    ) -> MediaStreamPayload:
        allowed_roots = [
            str(path)
            for path in (
                allowed_roots
                if allowed_roots is not None
                else self.allowed_roots_resolver(self.workspace)
            )
        ]
        safe_path = ensure_safe_asset_path(source_path, allowed_roots)
        if not safe_path.exists() or not safe_path.is_file():
            raise LookupError(f"媒体文件不存在: {safe_path}")

        total_size = safe_path.stat().st_size
        media_type = mimetypes.guess_type(str(safe_path))[0] or "application/octet-stream"

        if range_header is None:
            return MediaStreamPayload(
                file_path=safe_path,
                status_code=200,
                media_type=media_type,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(total_size),
                },
                start=0,
                end=max(total_size - 1, 0),
            )

        start, end = self._parse_range(range_header=range_header, total_size=total_size)
        length = end - start + 1
        return MediaStreamPayload(
            file_path=safe_path,
            status_code=206,
            media_type=media_type,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Range": f"bytes {start}-{end}/{total_size}",
                "Content-Length": str(length),
            },
            start=start,
            end=end,
        )

    def _allowed_roots_for_source_row(self, row: dict[str, object]) -> list[Path]:
        roots = self._artifact_allowed_roots()
        source_root = row.get("source_root_path")
        if source_root not in (None, ""):
            roots.append(Path(str(source_root)).expanduser().resolve())
        return self._dedupe_paths(roots)

    def _artifact_allowed_roots(self) -> list[Path]:
        return [self.paths.artifacts_dir.resolve()]

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

    def _parse_range(self, *, range_header: str, total_size: int) -> tuple[int, int]:
        header = range_header.strip()
        if total_size <= 0:
            raise MediaRangeError(total_size=0)
        if not header.startswith("bytes="):
            raise MediaRangeError(total_size=total_size)

        spec = header[6:]
        if "," in spec or "-" not in spec:
            raise MediaRangeError(total_size=total_size)

        start_text, end_text = spec.split("-", 1)
        if start_text == "" and end_text == "":
            raise MediaRangeError(total_size=total_size)

        try:
            if start_text == "":
                suffix_len = int(end_text)
                if suffix_len <= 0:
                    raise MediaRangeError(total_size=total_size)
                end = total_size - 1
                start = max(total_size - suffix_len, 0)
            else:
                start = int(start_text)
                end = total_size - 1 if end_text == "" else int(end_text)
        except ValueError as exc:
            raise MediaRangeError(total_size=total_size) from exc

        if start < 0 or end < start or start >= total_size:
            raise MediaRangeError(total_size=total_size)

        end = min(end, total_size - 1)
        return start, end

    def _emit_event(
        self,
        *,
        level: str,
        event_type: str,
        message: str,
        detail: dict[str, object],
    ) -> None:
        conn = connect_db(self.db_path)
        try:
            ObservabilityService(conn, workspace=self.workspace).emit_event(
                level=level,
                component="api",
                event_type=event_type,
                message=message,
                detail=detail,
                run_kind=None,
                run_id=None,
            )
        finally:
            conn.close()
