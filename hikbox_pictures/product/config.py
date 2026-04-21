from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .db.schema_bootstrap import bootstrap_databases


@dataclass(frozen=True)
class WorkspaceLayout:
    workspace_root: Path
    hikbox_root: Path
    config_path: Path
    library_db_path: Path
    embedding_db_path: Path
    external_root: Path
    artifacts_root: Path
    crops_root: Path
    aligned_root: Path
    context_root: Path
    logs_root: Path


def initialize_workspace(workspace_root: Path, external_root: Path) -> WorkspaceLayout:
    workspace_root = workspace_root.resolve()
    external_root = external_root.resolve()
    hikbox_root = workspace_root / ".hikbox"

    layout = WorkspaceLayout(
        workspace_root=workspace_root,
        hikbox_root=hikbox_root,
        config_path=hikbox_root / "config.json",
        library_db_path=hikbox_root / "library.db",
        embedding_db_path=hikbox_root / "embedding.db",
        external_root=external_root,
        artifacts_root=external_root / "artifacts",
        crops_root=external_root / "artifacts" / "crops",
        aligned_root=external_root / "artifacts" / "aligned",
        context_root=external_root / "artifacts" / "context",
        logs_root=external_root / "logs",
    )

    _ensure_directories(layout)
    _ensure_config(layout)
    bootstrap_databases(
        library_db_path=layout.library_db_path,
        embedding_db_path=layout.embedding_db_path,
    )
    return layout


def _ensure_directories(layout: WorkspaceLayout) -> None:
    layout.workspace_root.mkdir(parents=True, exist_ok=True)
    layout.hikbox_root.mkdir(parents=True, exist_ok=True)
    layout.crops_root.mkdir(parents=True, exist_ok=True)
    layout.aligned_root.mkdir(parents=True, exist_ok=True)
    layout.context_root.mkdir(parents=True, exist_ok=True)
    layout.logs_root.mkdir(parents=True, exist_ok=True)


def _ensure_config(layout: WorkspaceLayout) -> None:
    expected = {
        "version": 1,
        "external_root": str(layout.external_root),
    }
    if not layout.config_path.exists():
        layout.config_path.write_text(json.dumps(expected, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    raw = json.loads(layout.config_path.read_text(encoding="utf-8"))
    if raw != expected:
        raise ValueError(f"工作区配置不匹配: {layout.config_path}")
