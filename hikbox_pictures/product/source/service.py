from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from .repository import LibrarySource, SQLiteSourceRepository


class SourceServiceError(RuntimeError):
    """source 服务基础异常。"""


class SourceNotFoundError(SourceServiceError):
    """source 不存在。"""

    def __init__(self, source_id: int) -> None:
        self.source_id = source_id
        super().__init__(f"source 不存在: id={source_id}")


class SourceDeletedError(SourceServiceError):
    """source 已删除，不允许修改。"""

    def __init__(self, source_id: int) -> None:
        self.source_id = source_id
        super().__init__(f"source 已删除，不允许修改: id={source_id}")


class SourceService:
    def __init__(self, repo: SQLiteSourceRepository) -> None:
        self._repo = repo

    def add_source(self, *, root_path: Path | str, label: str) -> LibrarySource:
        path = Path(root_path)
        if not path.is_absolute():
            raise ValueError(f"source root_path 必须是绝对路径: {root_path}")

        normalized = str(path.resolve())
        clean_label = label.strip()
        if not clean_label:
            raise ValueError("source label 不能为空")

        return self._repo.create_source(
            root_path=normalized,
            label=clean_label,
            now=_utc_now(),
        )

    def disable_source(self, source_id: int) -> LibrarySource:
        source = self._require_active_source(source_id)
        if not source.enabled:
            return source
        return self._repo.update_source(source_id, enabled=False, now=_utc_now())

    def enable_source(self, source_id: int) -> LibrarySource:
        source = self._require_active_source(source_id)
        if source.enabled:
            return source
        return self._repo.update_source(source_id, enabled=True, now=_utc_now())

    def relabel_source(self, source_id: int, label: str) -> LibrarySource:
        self._require_active_source(source_id)
        clean_label = label.strip()
        if not clean_label:
            raise ValueError("source label 不能为空")
        return self._repo.update_source(source_id, label=clean_label, now=_utc_now())

    def remove_source(self, source_id: int) -> LibrarySource:
        self._require_active_source(source_id)
        return self._repo.update_source(
            source_id,
            status="deleted",
            enabled=False,
            now=_utc_now(),
        )

    def list_sources(self, *, include_deleted: bool = False) -> list[LibrarySource]:
        return self._repo.list_sources(include_deleted=include_deleted)

    def _require_active_source(self, source_id: int) -> LibrarySource:
        source = self._repo.get_source(source_id, include_deleted=True)
        if source is None:
            raise SourceNotFoundError(source_id)
        if source.status == "deleted":
            raise SourceDeletedError(source_id)
        return source


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
