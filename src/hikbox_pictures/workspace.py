from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkspacePaths:
    root: Path
    db_path: Path
    artifacts_dir: Path
    logs_dir: Path
    exports_dir: Path


def ensure_workspace_layout(root: Path) -> WorkspacePaths:
    resolved_root = root.expanduser().resolve()
    hikbox_dir = resolved_root / ".hikbox"
    db_path = hikbox_dir / "library.db"
    artifacts_dir = hikbox_dir / "artifacts"
    logs_dir = hikbox_dir / "logs"
    exports_dir = hikbox_dir / "exports"

    required_dirs = (
        hikbox_dir,
        artifacts_dir / "ann",
        artifacts_dir / "thumbs",
        artifacts_dir / "face-crops",
        logs_dir / "runs",
        exports_dir,
    )
    for path in required_dirs:
        path.mkdir(parents=True, exist_ok=True)

    db_path.touch(exist_ok=True)

    return WorkspacePaths(
        root=resolved_root,
        db_path=db_path,
        artifacts_dir=artifacts_dir,
        logs_dir=logs_dir,
        exports_dir=exports_dir,
    )
