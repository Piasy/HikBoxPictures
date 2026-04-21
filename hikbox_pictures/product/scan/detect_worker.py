from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifact_writer import atomic_write_json


@dataclass(frozen=True)
class DetectWorkerRequest:
    batch_id: int
    claim_token: str
    items: list[dict[str, int]]


def run_detect_worker(*, request: DetectWorkerRequest, output_path: Path) -> None:
    """子进程合同：只消费输入 payload，并输出结果文件，不直接写业务表。"""
    results: list[dict[str, Any]] = []
    for item in request.items:
        results.append(
            {
                "scan_batch_item_id": int(item["scan_batch_item_id"]),
                "photo_asset_id": int(item["photo_asset_id"]),
                "status": "done",
                "error_message": None,
            }
        )

    payload = {
        "batch_id": request.batch_id,
        "claim_token": request.claim_token,
        "results": results,
    }
    atomic_write_json(output_path, payload)
