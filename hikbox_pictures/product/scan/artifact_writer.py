from __future__ import annotations

import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


def atomic_write_json(target_path: Path, payload: dict[str, Any]) -> None:
    """先写临时文件，再原子替换到目标文件。"""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=target_path.parent, delete=False, suffix=".tmp") as tmp_file:
        tmp_path = Path(tmp_file.name)
        json.dump(payload, tmp_file, ensure_ascii=False)
        tmp_file.flush()
    tmp_path.replace(target_path)
