from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from hikbox_pictures.services.identity_rebuild_service import IdentityRebuildService


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="执行 identity v3 全量重建")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--backup-db", action="store_true")
    parser.add_argument("--skip-ann-rebuild", action="store_true")
    parser.add_argument("--threshold-profile", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    service: IdentityRebuildService | None = None
    try:
        service = IdentityRebuildService(Path(args.workspace))
        summary = service.run_rebuild(
            dry_run=bool(args.dry_run),
            backup_db=bool(args.backup_db),
            skip_ann_rebuild=bool(args.skip_ann_rebuild),
            threshold_profile_path=Path(args.threshold_profile) if args.threshold_profile is not None else None,
        )
    except Exception as exc:
        print(f"identity v3 重建失败: {exc}", file=sys.stderr)
        return 1
    finally:
        if service is not None:
            service.close()

    mode = "dry-run" if args.dry_run else "execute"
    print(
        "identity v3 重建完成: "
        + json.dumps(
            {
                "mode": mode,
                "threshold_profile_id": summary.get("threshold_profile_id"),
                "profile_mode": summary.get("profile_mode"),
                "materialized_cluster_count": summary.get("materialized_cluster_count"),
                "review_pending_cluster_count": summary.get("review_pending_cluster_count"),
                "discarded_cluster_count": summary.get("discarded_cluster_count"),
                "summary_path": str(
                    Path(args.workspace).expanduser().resolve()
                    / ".tmp"
                    / "rebuild-identities-v3"
                    / "last-summary.json"
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
