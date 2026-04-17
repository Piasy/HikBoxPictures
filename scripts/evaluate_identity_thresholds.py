from __future__ import annotations

import argparse
from pathlib import Path
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="评估 identity 阈值候选（已弃用）")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    parser.parse_args(argv)
    print(
        "identity 阈值评估脚本已弃用；请改用 build_identity_observation_snapshot.py + "
        "rerun_identity_cluster_run.py + /identity-tuning + export_observation_neighbors.py",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
