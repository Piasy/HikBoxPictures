from __future__ import annotations

from pathlib import Path

import pytest

from hikbox_pictures.product.db.schema_bootstrap import bootstrap_library_schema
from hikbox_pictures.product.source.repository import SQLiteSourceRepository
from hikbox_pictures.product.source.service import (
    SourceDeletedError,
    SourceNotFoundError,
    SourceService,
)


def test_source_service_full_lifecycle(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)

    source_root = (tmp_path / "photos").resolve()
    source_root.mkdir(parents=True)

    repo = SQLiteSourceRepository(db_path)
    service = SourceService(repo)

    created = service.add_source(root_path=source_root, label="家庭相册")
    assert created.root_path == str(source_root)
    assert created.enabled is True

    disabled = service.disable_source(created.id)
    assert disabled.enabled is False

    enabled = service.enable_source(created.id)
    assert enabled.enabled is True

    relabeled = service.relabel_source(created.id, "全家福")
    assert relabeled.label == "全家福"

    removed = service.remove_source(created.id)
    assert removed.status == "deleted"
    assert removed.enabled is False

    active_sources = service.list_sources()
    assert active_sources == []

    all_sources = service.list_sources(include_deleted=True)
    assert [item.id for item in all_sources] == [created.id]


def test_add_source_requires_absolute_path(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)

    repo = SQLiteSourceRepository(db_path)
    service = SourceService(repo)

    with pytest.raises(ValueError, match="绝对路径"):
        service.add_source(root_path=Path("relative/photos"), label="相对路径")


def test_add_source_rejects_duplicate_root_path(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)

    source_root = (tmp_path / "photos").resolve()
    source_root.mkdir(parents=True)

    repo = SQLiteSourceRepository(db_path)
    service = SourceService(repo)

    service.add_source(root_path=source_root, label="目录1")

    with pytest.raises(ValueError, match="root_path"):
        service.add_source(root_path=source_root, label="目录2")


def test_add_source_rejects_recreate_after_soft_delete(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)

    source_root = (tmp_path / "photos").resolve()
    source_root.mkdir(parents=True)

    repo = SQLiteSourceRepository(db_path)
    service = SourceService(repo)

    created = service.add_source(root_path=source_root, label="目录1")
    service.remove_source(created.id)

    with pytest.raises(ValueError, match="root_path"):
        service.add_source(root_path=source_root, label="目录2")


def test_source_operation_raises_not_found_error(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)
    repo = SQLiteSourceRepository(db_path)
    service = SourceService(repo)

    with pytest.raises(SourceNotFoundError):
        service.disable_source(1234)


def test_source_operation_raises_deleted_error(tmp_path: Path) -> None:
    db_path = tmp_path / "library.db"
    bootstrap_library_schema(db_path)

    source_root = (tmp_path / "photos").resolve()
    source_root.mkdir(parents=True)
    repo = SQLiteSourceRepository(db_path)
    service = SourceService(repo)
    created = service.add_source(root_path=source_root, label="目录1")
    service.remove_source(created.id)

    with pytest.raises(SourceDeletedError):
        service.enable_source(created.id)
