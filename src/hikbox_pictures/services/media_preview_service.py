from __future__ import annotations

from dataclasses import dataclass
import mimetypes
from pathlib import Path
from typing import Callable, Iterator

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.repositories import AssetRepo
from hikbox_pictures.services.path_guard import ensure_safe_asset_path
from hikbox_pictures.services.runtime import resolve_media_allowed_roots


class MediaRangeError(Exception):
    def __init__(self, *, total_size: int, message: str = "无效的 Range 请求") -> None:
        super().__init__(message)
        self.total_size = int(total_size)
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
        self.allowed_roots_resolver = allowed_roots_resolver or resolve_media_allowed_roots

    def read_original_stream(self, photo_id: int, range_header: str | None = None) -> MediaStreamPayload:
        conn = connect_db(self.db_path)
        try:
            repo = AssetRepo(conn)
            row = repo.get_photo_media(int(photo_id))
        finally:
            conn.close()

        if row is None:
            raise LookupError(f"photo {photo_id} 不存在")
        source_path = str(row["primary_path"])
        return self._build_stream_payload(source_path=source_path, range_header=range_header)

    def read_preview_stream(self, photo_id: int) -> MediaStreamPayload:
        conn = connect_db(self.db_path)
        try:
            repo = AssetRepo(conn)
            row = repo.get_photo_media(int(photo_id))
        finally:
            conn.close()

        if row is None:
            raise LookupError(f"photo {photo_id} 不存在")
        # Task13 先走独立预览入口，后续 Task14 可切换到预览产物路径。
        return self._build_stream_payload(source_path=str(row["primary_path"]), range_header=None)

    def read_observation_crop(self, observation_id: int) -> MediaStreamPayload:
        conn = connect_db(self.db_path)
        try:
            repo = AssetRepo(conn)
            row = repo.get_observation_media(int(observation_id))
        finally:
            conn.close()

        if row is None:
            raise LookupError(f"observation {observation_id} 不存在")
        crop_path = row.get("crop_path")
        if crop_path is None or str(crop_path).strip() == "":
            raise LookupError(f"observation {observation_id} 缺少 crop_path")
        return self._build_stream_payload(source_path=str(crop_path), range_header=None)

    def read_observation_context(self, observation_id: int) -> MediaStreamPayload:
        conn = connect_db(self.db_path)
        try:
            repo = AssetRepo(conn)
            row = repo.get_observation_media(int(observation_id))
        finally:
            conn.close()

        if row is None:
            raise LookupError(f"observation {observation_id} 不存在")
        return self._build_stream_payload(source_path=str(row["primary_path"]), range_header=None)

    def _build_stream_payload(self, *, source_path: str, range_header: str | None) -> MediaStreamPayload:
        allowed_roots = [str(path) for path in self.allowed_roots_resolver(self.workspace)]
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
