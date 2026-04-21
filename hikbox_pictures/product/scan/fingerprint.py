from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_for_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """计算文件 sha256 指纹。"""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while True:
            chunk = file.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
