"""图库 source 业务服务。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hikbox_pictures.product.source.repository import SourceRecord, SourceRepository


class SourceError(Exception):
    """source 域基础异常。"""


class SourceNotFoundError(SourceError):
    """source 不存在或已被删除。"""


class SourceRootPathConflictError(SourceError):
    """source 根目录冲突。"""


class SourceService:
    """source 增删改查服务。"""

    def __init__(self, repo: SourceRepository):
        self._repo = repo

    def add_source(self, root_path: str, *, label: str | None = None) -> SourceRecord:
        normalized_path = _normalize_root_path(root_path)
        normalized_label = (label or Path(normalized_path).name).strip()
        if not normalized_label:
            raise ValueError("label 不能为空")
        try:
            return self._repo.insert_source(root_path=normalized_path, label=normalized_label)
        except sqlite3.IntegrityError as exc:
            raise SourceRootPathConflictError(f"source 根目录已存在: {normalized_path}") from exc

    def list_sources(self) -> list[SourceRecord]:
        return self._repo.list_sources(include_removed=False)

    def disable_source(self, source_id: int) -> SourceRecord:
        result = self._repo.set_enabled(source_id, False)
        if result is None:
            raise SourceNotFoundError(f"source 不存在，id={source_id}")
        return result

    def enable_source(self, source_id: int) -> SourceRecord:
        result = self._repo.set_enabled(source_id, True)
        if result is None:
            raise SourceNotFoundError(f"source 不存在，id={source_id}")
        return result

    def relabel_source(self, source_id: int, label: str) -> SourceRecord:
        normalized_label = label.strip()
        if not normalized_label:
            raise ValueError("label 不能为空")
        result = self._repo.set_label(source_id, normalized_label)
        if result is None:
            raise SourceNotFoundError(f"source 不存在，id={source_id}")
        return result

    def remove_source(self, source_id: int) -> SourceRecord:
        result = self._repo.soft_remove(source_id)
        if result is None:
            raise SourceNotFoundError(f"source 不存在，id={source_id}")
        return result


def _normalize_root_path(root_path: str) -> str:
    raw_path = Path(root_path).expanduser()
    if not raw_path.is_absolute():
        raise ValueError(f"root_path 必须是绝对路径: {root_path}")

    normalized = raw_path.resolve()
    if not normalized.exists():
        raise ValueError(f"root_path 不存在: {normalized}")
    if not normalized.is_dir():
        raise ValueError(f"root_path 必须是目录: {normalized}")
    return str(normalized)
