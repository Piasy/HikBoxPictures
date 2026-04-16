from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from hikbox_pictures.services.identity_threshold_evaluation_service import IdentityThresholdEvaluationService


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="评估 identity 阈值候选（只读）")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    service: IdentityThresholdEvaluationService | None = None
    try:
        service = IdentityThresholdEvaluationService(Path(args.workspace))
        report = service.evaluate()

        output_dir = Path(args.output_dir).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        summary_path = output_dir / "summary.json"
        candidate_path = output_dir / "candidate-thresholds.json"

        summary_path.write_text(
            json.dumps(report["summary"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        candidate_path.write_text(
            json.dumps(report["candidate_profile"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"identity 阈值评估失败: {exc}", file=sys.stderr)
        return 1
    finally:
        if service is not None:
            service.close()

    print(
        "identity 阈值评估完成: "
        + json.dumps(
            {
                "summary_path": str(summary_path),
                "candidate_thresholds_path": str(candidate_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
