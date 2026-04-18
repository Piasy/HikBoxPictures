from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from hikbox_experiments.identity_v3_1.models import AssignParameters


def _parse_cluster_ids(raw: str) -> set[int]:
    values: set[int] = set()
    for token in str(raw).split(","):
        stripped = token.strip()
        if not stripped:
            continue
        values.add(int(stripped))
    if not values:
        raise ValueError("cluster id 列表不能为空")
    return values


def _resolve_export_service_class():  # type: ignore[no-untyped-def]
    from hikbox_experiments.identity_v3_1.export_service import (
        IdentityV31ReportExportService,
    )

    return IdentityV31ReportExportService


def _build_parser() -> argparse.ArgumentParser:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="导出 identity v3.1 prototype 报告")
    parser.add_argument("--workspace", type=Path, default=repo_root / ".tmp" / ".hikbox")
    parser.add_argument("--base-run-id", type=int, required=False)
    parser.add_argument("--assign-source", choices=("all", "review_pending", "attachment"), default="all")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--auto-max-distance", type=float, default=0.25)
    parser.add_argument("--review-max-distance", type=float, default=0.35)
    parser.add_argument("--min-margin", type=float, default=0.08)
    parser.add_argument("--promote-cluster-ids", type=str, required=False)
    parser.add_argument("--disable-seed-cluster-ids", type=str, required=False)
    parser.add_argument("--output-root", type=Path, default=repo_root / ".tmp" / "v3_1-identity-prototype")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).expanduser().resolve()
    output_root = Path(args.output_root).expanduser().resolve()

    try:
        promote_cluster_ids = (
            _parse_cluster_ids(str(args.promote_cluster_ids)) if args.promote_cluster_ids is not None else set()
        )
        disable_seed_cluster_ids = (
            _parse_cluster_ids(str(args.disable_seed_cluster_ids))
            if args.disable_seed_cluster_ids is not None
            else set()
        )
        assign_parameters = AssignParameters(
            base_run_id=(None if args.base_run_id is None else int(args.base_run_id)),
            assign_source=str(args.assign_source),
            top_k=int(args.top_k),
            auto_max_distance=float(args.auto_max_distance),
            review_max_distance=float(args.review_max_distance),
            min_margin=float(args.min_margin),
            promote_cluster_ids=tuple(sorted(promote_cluster_ids)),
            disable_seed_cluster_ids=tuple(sorted(disable_seed_cluster_ids)),
        ).validate()

        service_cls = _resolve_export_service_class()
        service = service_cls(workspace)
        result = service.export(
            base_run_id=(None if args.base_run_id is None else int(args.base_run_id)),
            promote_cluster_ids=promote_cluster_ids,
            disable_seed_cluster_ids=disable_seed_cluster_ids,
            assign_parameters=assign_parameters,
            output_root=output_root,
        )
    except Exception as exc:
        print(f"identity v3.1 prototype 导出失败: {exc}", file=sys.stderr)
        return 1

    print(
        "identity v3.1 prototype 导出完成: "
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
