from pathlib import Path

import pytest

from hikbox_pictures.product.config import initialize_workspace
from hikbox_pictures.product.source.repository import SourceRepository
from hikbox_pictures.product.source.service import (
    SourceNotFoundError,
    SourceRootPathConflictError,
    SourceService,
)


def test_source_add_disable_enable_relabel_and_remove(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    repo = SourceRepository(layout.library_db)
    service = SourceService(repo)

    root = tmp_path / "family"
    root.mkdir()

    source = service.add_source(str(root), label="family")
    service.disable_source(source.id)
    assert service.list_sources()[0].enabled is False

    service.enable_source(source.id)
    assert service.list_sources()[0].enabled is True

    service.relabel_source(source.id, "family-2026")
    assert service.list_sources()[0].label == "family-2026"

    service.remove_source(source.id)
    assert service.list_sources() == []


def test_source_add_conflicts_when_root_path_duplicated(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    service = SourceService(SourceRepository(layout.library_db))
    root = tmp_path / "family"
    root.mkdir()

    service.add_source(str(root), label="family")

    with pytest.raises(SourceRootPathConflictError):
        service.add_source(str(root), label="family-duplicate")


def test_source_add_rejects_non_absolute_or_missing_path(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    service = SourceService(SourceRepository(layout.library_db))

    with pytest.raises(ValueError, match="绝对路径"):
        service.add_source("relative/path", label="relative")

    missing = tmp_path / "not-exists"
    with pytest.raises(ValueError, match="不存在"):
        service.add_source(str(missing), label="missing")


def test_source_enable_or_relabel_after_remove_raises_not_found(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    service = SourceService(SourceRepository(layout.library_db))
    root = tmp_path / "family"
    root.mkdir()

    source = service.add_source(str(root), label="family")
    service.remove_source(source.id)

    with pytest.raises(SourceNotFoundError):
        service.enable_source(source.id)
    with pytest.raises(SourceNotFoundError):
        service.relabel_source(source.id, "new-label")


def test_source_remove_then_add_same_root_path_still_conflicts(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    external_root = tmp_path / "external"
    layout = initialize_workspace(workspace_root=workspace_root, external_root=external_root)
    service = SourceService(SourceRepository(layout.library_db))
    root = tmp_path / "family"
    root.mkdir()

    source = service.add_source(str(root), label="family")
    service.remove_source(source.id)

    with pytest.raises(SourceRootPathConflictError):
        service.add_source(str(root), label="family-again")
