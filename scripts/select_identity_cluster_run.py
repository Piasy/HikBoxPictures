from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="切换 identity cluster run review target")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--run-id", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    orchestrator: IdentityBootstrapOrchestrator | None = None
    try:
        orchestrator = IdentityBootstrapOrchestrator(Path(args.workspace))
        summary = orchestrator.select_review_target(run_id=int(args.run_id))
    except Exception as exc:
        print(f"identity cluster run 选择失败: {exc}", file=sys.stderr)
        return 1
    finally:
        if orchestrator is not None:
            orchestrator.close()

    print("identity cluster run 选择完成: " + json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
