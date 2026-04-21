from __future__ import annotations

from pathlib import Path

LIVE_STILL_EXTENSIONS = {".heic", ".heif"}


def match_live_photo_mov(still_path: Path) -> Path | None:
    """为 HEIC/HEIF 查找同目录的隐藏 MOV 配对文件。"""
    suffix = still_path.suffix.lower()
    if suffix not in LIVE_STILL_EXTENSIONS:
        return None

    file_name = still_path.name
    stem_name = still_path.stem

    candidates = _collect_hidden_mov(still_path.parent, prefix=f".{file_name}_")
    candidates.extend(_collect_hidden_mov(still_path.parent, prefix=f".{stem_name}_"))
    if not candidates:
        return None

    # 多候选稳定排序：时间戳降序 -> mtime_ns 降序 -> 文件名升序。
    unique_candidates = {item[2].resolve(): item for item in candidates}
    ordered = sorted(
        unique_candidates.values(),
        key=lambda item: (-item[0], -item[1], item[2].name),
    )
    return ordered[0][2]


def _collect_hidden_mov(parent: Path, *, prefix: str) -> list[tuple[int, int, Path]]:
    result: list[tuple[int, int, Path]] = []
    for entry in parent.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix.lower() != ".mov":
            continue
        if not entry.name.startswith(prefix):
            continue
        suffix = entry.name[len(prefix) : -4]
        if suffix.isdigit() and suffix:
            result.append((int(suffix), entry.stat().st_mtime_ns, entry))
    return result
