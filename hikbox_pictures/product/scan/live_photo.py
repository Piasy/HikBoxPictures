"""Live Photo 配对能力。"""

from __future__ import annotations

from dataclasses import dataclass
import re
from collections.abc import Iterable
from pathlib import Path

LIVE_STILL_SUFFIXES = {".heic", ".heif"}
LIVE_MOV_EXT = ".mov"


@dataclass(frozen=True)
class LiveMovCandidate:
    path: Path
    token: int
    mtime_ns: int


def match_live_mov(still_path: Path) -> Path | None:
    """按 iPhone 隐藏命名规则匹配 Live Photo 对应 MOV。"""
    return pick_best_live_mov(still_path, still_path.parent.iterdir())


def pick_best_live_mov(still_path: Path, candidates: Iterable[Path]) -> Path | None:
    """从候选 MOV 中挑选最优配对。"""
    suffix = still_path.suffix.lower()
    if suffix not in LIVE_STILL_SUFFIXES:
        return None

    parsed: list[LiveMovCandidate] = []
    for entry in candidates:
        if entry.suffix.lower() != LIVE_MOV_EXT:
            continue
        token = _extract_token_for_still(entry.name, still_path)
        if token is None:
            continue
        try:
            mtime_ns = int(entry.stat().st_mtime_ns)
        except OSError:
            continue
        parsed.append(LiveMovCandidate(path=entry, token=token, mtime_ns=mtime_ns))

    if not parsed:
        return None
    best = sorted(parsed, key=lambda item: (item.token, item.mtime_ns, item.path.name), reverse=True)[0]
    return best.path


def _extract_token_for_still(entry_name: str, still_path: Path) -> int | None:
    still_name = re.escape(still_path.name)
    still_stem = re.escape(still_path.stem)
    with_ext = re.match(rf"^\.{still_name}_(\d+)\.mov$", entry_name, flags=re.IGNORECASE)
    if with_ext is not None:
        return int(with_ext.group(1))
    without_ext = re.match(rf"^\.{still_stem}_(\d+)\.mov$", entry_name, flags=re.IGNORECASE)
    if without_ext is not None:
        return int(without_ext.group(1))
    return None
