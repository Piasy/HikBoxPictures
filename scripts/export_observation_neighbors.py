from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from hikbox_pictures.services.observation_neighbor_export_service import (
    ObservationNeighborExportService,
)


def _parse_observation_ids(raw: str) -> list[int]:
    values: list[int] = []
    for token in str(raw).split(","):
        stripped = token.strip()
        if not stripped:
            continue
        values.append(int(stripped))
    if not values:
        raise ValueError("observation id 列表不能为空")
    return values


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="导出 observation 最近邻的 crop/preview 预览")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--observation-ids", type=str, required=True)
    parser.add_argument("--neighbor-count", type=int, default=8)
    parser.add_argument("--output-root", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    output_root = (
        Path(args.output_root).expanduser().resolve()
        if args.output_root is not None
        else Path(__file__).resolve().parents[1] / ".tmp" / "observation-nearest-neighbors"
    )

    try:
        observation_ids = _parse_observation_ids(str(args.observation_ids))
        service = ObservationNeighborExportService(Path(args.workspace))
        result = service.export(
            observation_ids=observation_ids,
            output_root=output_root,
            neighbor_count=int(args.neighbor_count),
        )
    except Exception as exc:
        print(f"observation 最近邻导出失败: {exc}", file=sys.stderr)
        return 1

    print(
        "observation 最近邻导出完成: "
        + json.dumps(
            {
                "output_dir": str(result["output_dir"]),
                "index_path": str(result["index_path"]),
                "manifest_path": str(result["manifest_path"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
