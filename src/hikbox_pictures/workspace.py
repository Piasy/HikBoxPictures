from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

_CONFIG_VERSION = 1


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path
    config_path: Path
    db_path: Path
    external_root: Path
    artifacts_dir: Path
    logs_dir: Path
    exports_dir: Path


def init_workspace_layout(root: Path, external_root: Path) -> WorkspacePaths:
    resolved_root = root.expanduser().resolve()
    resolved_external_root = external_root.expanduser().resolve()
    hikbox_dir = resolved_root / ".hikbox"
    config_path = hikbox_dir / "config.json"
    db_path = hikbox_dir / "library.db"

    hikbox_dir.mkdir(parents=True, exist_ok=True)
    _ensure_external_dirs(resolved_external_root)
    db_path.touch(exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "version": _CONFIG_VERSION,
                "external_root": str(resolved_external_root),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return load_workspace_paths(resolved_root)


def load_workspace_paths(root: Path) -> WorkspacePaths:
    resolved_root = root.expanduser().resolve()
    hikbox_dir = resolved_root / ".hikbox"
    config_path = hikbox_dir / "config.json"
    db_path = hikbox_dir / "library.db"

    if not config_path.exists():
        raise FileNotFoundError(f"workspace 配置不存在: {config_path}")

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    version = int(payload.get("version", 0))
    if version != _CONFIG_VERSION:
        raise ValueError(f"workspace 配置版本不受支持: {version}")

    external_root_raw = str(payload.get("external_root", "")).strip()
    if not external_root_raw:
        raise ValueError("workspace 配置缺少 external_root")
    external_root = Path(external_root_raw).expanduser().resolve()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.touch(exist_ok=True)
    _ensure_external_dirs(external_root)

    return WorkspacePaths(
        root=resolved_root,
        config_path=config_path,
        db_path=db_path,
        external_root=external_root,
        artifacts_dir=external_root / "artifacts",
        logs_dir=external_root / "logs",
        exports_dir=external_root / "exports",
    )


def load_workspace_paths_from_db_path(db_path: Path) -> WorkspacePaths:
    resolved_db_path = db_path.expanduser().resolve()
    return load_workspace_paths(resolved_db_path.parent.parent)


def ensure_workspace_layout(root: Path) -> WorkspacePaths:
    return load_workspace_paths(root)


def _ensure_external_dirs(external_root: Path) -> None:
    required_dirs = (
        external_root / "artifacts" / "ann",
        external_root / "artifacts" / "thumbs",
        external_root / "artifacts" / "face-crops",
        external_root / "artifacts" / "context",
        external_root / "logs" / "runs",
        external_root / "exports",
    )
    for path in required_dirs:
        path.mkdir(parents=True, exist_ok=True)
