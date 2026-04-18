from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

if __package__ in (None, ""):
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    src_dir = repo_root / "src"
    if src_dir.is_dir():
        src_dir_str = str(src_dir)
        while src_dir_str in sys.path:
            sys.path.remove(src_dir_str)
        sys.path.insert(0, src_dir_str)
        package_src = src_dir / "hikbox_pictures"
        package = sys.modules.get("hikbox_pictures")
        if package is not None and hasattr(package, "__path__"):
            package_src_str = str(package_src)
            package_paths = [str(path) for path in package.__path__]
            package.__path__ = [package_src_str, *[path for path in package_paths if path != package_src_str]]
        services = sys.modules.get("hikbox_pictures.services")
        if services is not None and hasattr(services, "__path__"):
            services_src_str = str(package_src / "services")
            services_paths = [str(path) for path in services.__path__]
            services.__path__ = [services_src_str, *[path for path in services_paths if path != services_src_str]]
        orchestrator_module_name = "hikbox_pictures.services.identity_bootstrap_orchestrator"
        cached_orchestrator = sys.modules.get(orchestrator_module_name)
        if cached_orchestrator is not None:
            cached_file = Path(getattr(cached_orchestrator, "__file__", "")).resolve()
            cached_from_current_src = str(cached_file).startswith(str(src_dir.resolve()))
            if not cached_from_current_src:
                # 清理外部 worktree 缓存，避免导入到错误模块。
                sys.modules.pop(orchestrator_module_name, None)
            # 为避免复用解释器中的旧状态，即使来自当前 src 也强制重新导入。
            sys.modules.pop(orchestrator_module_name, None)

from hikbox_pictures.services.identity_bootstrap_orchestrator import IdentityBootstrapOrchestrator


_PROGRESS_LOG_INTERVAL_SECONDS = 10.0


class _ThrottledProgressPrinter:
    def __init__(self) -> None:
        self._last_emit_at: float | None = None

    def __call__(self, payload: dict[str, object]) -> None:
        now = time.monotonic()
        if self._last_emit_at is None or (now - self._last_emit_at) >= _PROGRESS_LOG_INTERVAL_SECONDS:
            self._emit(payload)
            self._last_emit_at = now

    def _emit(self, payload: dict[str, object]) -> None:
        phase = str(payload.get("phase") or "unknown")
        subphase = str(payload.get("subphase") or "unknown")
        total = max(0, int(payload.get("total_count") or 0))
        completed = min(max(0, int(payload.get("completed_count") or 0)), total)
        percent = 100.0 if total <= 0 else float(payload.get("percent") or 0.0)
        print(
            "identity cluster rerun 进度: "
            f"phase={phase} "
            f"subphase={subphase} "
            f"total={total} "
            f"completed={completed} "
            f"percent={percent:.1f}%",
            flush=True,
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="基于 snapshot 重跑 identity cluster run（含 prepare）")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--snapshot-id", type=int, required=True)
    parser.add_argument("--cluster-profile-id", type=int, default=None)
    parser.add_argument("--supersedes-run-id", type=int, default=None)
    parser.add_argument("--no-select-review-target", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    orchestrator: IdentityBootstrapOrchestrator | None = None
    progress_printer = _ThrottledProgressPrinter()
    try:
        orchestrator = IdentityBootstrapOrchestrator(Path(args.workspace))
        summary = orchestrator.rerun_cluster_run(
            snapshot_id=int(args.snapshot_id),
            cluster_profile_id=args.cluster_profile_id,
            supersedes_run_id=args.supersedes_run_id,
            select_as_review_target=not bool(args.no_select_review_target),
            progress_reporter=progress_printer,
        )
    except Exception as exc:
        print(f"identity cluster rerun 失败: {exc}", file=sys.stderr)
        return 1
    finally:
        if orchestrator is not None:
            orchestrator.close()

    print("identity cluster rerun 完成: " + json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
