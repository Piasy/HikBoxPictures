"""工作区配置与初始化。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from hikbox_pictures.product.db.schema_bootstrap import bootstrap_embedding_db, bootstrap_library_db


@dataclass(frozen=True)
class WorkspaceLayout:
    workspace_root: Path
    hikbox_root: Path
    library_db: Path
    embedding_db: Path
    config_json: Path


def _normalize_root(path: Path, *, name: str) -> Path:
    root = path.expanduser().resolve()
    if root.exists() and not root.is_dir():
        raise ValueError(f"{name} 必须是目录路径: {root}")
    return root


def _write_config(config_path: Path, *, external_root: Path) -> None:
    payload = {
        "external_root": str(external_root),
    }
    config_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def initialize_workspace(workspace_root: Path, external_root: Path) -> WorkspaceLayout:
    workspace = _normalize_root(workspace_root, name="workspace_root")
    external = _normalize_root(external_root, name="external_root")

    workspace.mkdir(parents=True, exist_ok=True)
    external.mkdir(parents=True, exist_ok=True)

    hikbox_root = workspace / ".hikbox"
    hikbox_root.mkdir(parents=True, exist_ok=True)

    layout = WorkspaceLayout(
        workspace_root=workspace,
        hikbox_root=hikbox_root,
        library_db=hikbox_root / "library.db",
        embedding_db=hikbox_root / "embedding.db",
        config_json=hikbox_root / "config.json",
    )

    bootstrap_library_db(layout.library_db)
    bootstrap_embedding_db(layout.embedding_db)
    _write_config(layout.config_json, external_root=external)
    return layout
