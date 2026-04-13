from __future__ import annotations

from pathlib import Path


def ensure_safe_asset_path(candidate: str | Path, allowed_roots: list[str | Path]) -> Path:
    resolved = Path(candidate).expanduser().resolve()
    resolved_roots = [Path(root).expanduser().resolve() for root in allowed_roots]
    if not resolved_roots:
        raise PermissionError(f"asset path out of allowed roots: {resolved}")

    for root in resolved_roots:
        if resolved == root or root in resolved.parents:
            return resolved
    raise PermissionError(f"asset path out of allowed roots: {resolved}")
