from __future__ import annotations

import builtins
import json
import shutil
import sys
from datetime import datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from .fixtures_identity_v3_1_export import build_identity_v3_1_export_workspace

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "export_identity_v3_1_report.py"
_SCRIPT_SPEC = spec_from_file_location("task5_export_identity_v3_1_report_script", _SCRIPT_PATH)
if _SCRIPT_SPEC is None or _SCRIPT_SPEC.loader is None:
    raise RuntimeError(f"无法加载导出脚本: {_SCRIPT_PATH}")
_SCRIPT_MODULE = module_from_spec(_SCRIPT_SPEC)
sys.modules[_SCRIPT_SPEC.name] = _SCRIPT_MODULE
_SCRIPT_SPEC.loader.exec_module(_SCRIPT_MODULE)
export_main = _SCRIPT_MODULE.main


def _load_summary_json(stdout: str) -> dict[str, object]:
    marker = "identity v3.1 prototype 导出完成: "
    lines = [line for line in stdout.splitlines() if line.startswith(marker)]
    assert lines, "stdout 缺少导出成功摘要"
    return json.loads(lines[-1][len(marker) :])


def test_export_identity_v3_1_report_script_passes_all_arguments_to_service(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: dict[str, object] = {}

    class _StubService:
        def __init__(self, workspace: Path) -> None:
            calls["workspace"] = Path(workspace)

        def export(self, **kwargs: object) -> dict[str, Path]:
            calls["export_kwargs"] = dict(kwargs)
            output_dir = tmp_path / "script-pass-args" / "bundle"
            output_dir.mkdir(parents=True, exist_ok=True)
            return {
                "output_dir": output_dir,
                "index_path": output_dir / "index.html",
                "manifest_path": output_dir / "manifest.json",
            }

    monkeypatch.setattr(_SCRIPT_MODULE, "_resolve_export_service_class", lambda: _StubService)

    rc = export_main(
        [
            "--workspace",
            str(tmp_path / "workspace"),
            "--base-run-id",
            "321",
            "--assign-source",
            "attachment",
            "--top-k",
            "7",
            "--auto-max-distance",
            "0.22",
            "--review-max-distance",
            "0.33",
            "--min-margin",
            "0.11",
            "--promote-cluster-ids",
            "11,22",
            "--disable-seed-cluster-ids",
            "33,44",
            "--output-root",
            str(tmp_path / "script-pass-args"),
        ]
    )

    assert rc == 0
    assert calls["workspace"] == (tmp_path / "workspace").resolve()
    export_kwargs = calls["export_kwargs"]
    assert isinstance(export_kwargs, dict)
    assert int(export_kwargs["base_run_id"]) == 321
    assert export_kwargs["promote_cluster_ids"] == {11, 22}
    assert export_kwargs["disable_seed_cluster_ids"] == {33, 44}

    assign_parameters = export_kwargs["assign_parameters"]
    assert assign_parameters.assign_source == "attachment"
    assert int(assign_parameters.top_k) == 7
    assert float(assign_parameters.auto_max_distance) == pytest.approx(0.22)
    assert float(assign_parameters.review_max_distance) == pytest.approx(0.33)
    assert float(assign_parameters.min_margin) == pytest.approx(0.11)


def test_export_identity_v3_1_report_script_uses_repo_default_workspace_and_output_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: dict[str, object] = {}

    class _StubService:
        def __init__(self, workspace: Path) -> None:
            calls["workspace"] = Path(workspace)

        def export(self, **kwargs: object) -> dict[str, Path]:
            calls["output_root"] = Path(kwargs["output_root"])
            output_dir = tmp_path / "script-default-paths" / "bundle"
            output_dir.mkdir(parents=True, exist_ok=True)
            return {
                "output_dir": output_dir,
                "index_path": output_dir / "index.html",
                "manifest_path": output_dir / "manifest.json",
            }

    monkeypatch.setattr(_SCRIPT_MODULE, "_resolve_export_service_class", lambda: _StubService)

    rc = export_main([])

    assert rc == 0
    assert calls["workspace"] == (_REPO_ROOT / ".tmp" / ".hikbox").resolve()
    assert calls["output_root"] == (_REPO_ROOT / ".tmp" / "v3_1-identity-prototype").resolve()


def test_export_identity_v3_1_report_script_calls_assign_parameters_validate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state = {"called": False}

    original_validate = _SCRIPT_MODULE.AssignParameters.validate

    def _wrapped_validate(self):  # type: ignore[no-untyped-def]
        state["called"] = True
        return original_validate(self)

    class _StubService:
        def __init__(self, workspace: Path) -> None:
            self.workspace = Path(workspace)

        def export(self, **kwargs: object) -> dict[str, Path]:
            output_dir = tmp_path / "script-validate" / "bundle"
            output_dir.mkdir(parents=True, exist_ok=True)
            return {
                "output_dir": output_dir,
                "index_path": output_dir / "index.html",
                "manifest_path": output_dir / "manifest.json",
            }

    monkeypatch.setattr(_SCRIPT_MODULE.AssignParameters, "validate", _wrapped_validate)
    monkeypatch.setattr(_SCRIPT_MODULE, "_resolve_export_service_class", lambda: _StubService)

    rc = export_main([
        "--workspace",
        str(tmp_path / "workspace"),
        "--output-root",
        str(tmp_path / "script-validate"),
    ])

    assert rc == 0
    assert bool(state["called"]) is True


def test_export_identity_v3_1_report_script_rejects_invalid_assign_source() -> None:
    with pytest.raises(SystemExit):
        export_main(["--assign-source", "bogus"])


def test_export_identity_v3_1_report_script_resolver_returns_formal_export_service_class() -> None:
    from hikbox_experiments.identity_v3_1.export_service import IdentityV31ReportExportService

    resolved = _SCRIPT_MODULE._resolve_export_service_class()
    assert resolved is IdentityV31ReportExportService


def test_export_identity_v3_1_report_script_returns_1_when_resolver_import_fails(
    monkeypatch,
    capsys,
) -> None:
    def _raise_import_error():  # type: ignore[no-untyped-def]
        raise ImportError("missing export service")

    monkeypatch.setattr(_SCRIPT_MODULE, "_resolve_export_service_class", _raise_import_error)

    rc = export_main([])
    assert rc == 1
    captured = capsys.readouterr()
    assert "missing export service" in captured.err


def test_export_identity_v3_1_report_script_returns_1_when_internal_import_path_fails(
    monkeypatch,
    capsys,
) -> None:
    original_import = builtins.__import__

    def _patched_import(name, globals=None, locals=None, fromlist=(), level=0):  # type: ignore[no-untyped-def]
        if name == "hikbox_experiments.identity_v3_1.export_service":
            raise ImportError("missing export service")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _patched_import)

    rc = export_main([])
    assert rc == 1
    captured = capsys.readouterr()
    assert "missing export service" in captured.err


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--top-k", "0"], "top_k 必须大于 0"),
        (
            ["--auto-max-distance", "0.40", "--review-max-distance", "0.30"],
            "auto_max_distance 不能大于 review_max_distance",
        ),
        (["--min-margin", "-0.1"], "min_margin 不能小于 0"),
    ],
)
def test_export_identity_v3_1_report_script_returns_1_when_validate_fails(
    argv: list[str],
    message: str,
    capsys,
) -> None:
    rc = export_main(argv)

    assert rc == 1
    captured = capsys.readouterr()
    assert message in captured.err


def test_export_identity_v3_1_report_script_returns_1_when_service_raises(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    class _BrokenService:
        def __init__(self, workspace: Path) -> None:
            self.workspace = Path(workspace)

        def export(self, **kwargs: object) -> dict[str, Path]:
            raise RuntimeError("boom")

    monkeypatch.setattr(_SCRIPT_MODULE, "_resolve_export_service_class", lambda: _BrokenService)

    rc = export_main([
        "--workspace",
        str(tmp_path / "workspace"),
        "--output-root",
        str(tmp_path / "script-service-fail"),
    ])

    assert rc == 1
    captured = capsys.readouterr()
    assert "identity v3.1 prototype 导出失败: boom" in captured.err


def test_export_identity_v3_1_report_script_returns_1_when_all_seed_disabled_real_fixture(
    tmp_path: Path,
    capsys,
) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "script-disable-all-seed")
    output_root = tmp_path / "script-disable-all-seed-output"
    try:
        cluster_ids = ws.cluster_ids
        disable_ids = ",".join(
            str(cluster_ids[key])
            for key in ("seed_primary", "seed_fallback", "seed_invalid")
        )

        rc = export_main(
            [
                "--workspace",
                str(ws.root),
                "--base-run-id",
                str(ws.base_run_id),
                "--disable-seed-cluster-ids",
                disable_ids,
                "--output-root",
                str(output_root),
            ]
        )

        assert rc == 1
        captured = capsys.readouterr()
        assert "没有任何可用 seed identity" in captured.err
    finally:
        ws.close()


def test_export_identity_v3_1_report_script_happy_path_with_real_fixture_and_default_output_root(
    tmp_path: Path,
    capsys,
) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "script-happy-path")
    default_output_root = (_REPO_ROOT / ".tmp" / "v3_1-identity-prototype").resolve()
    before_dirs = {path for path in default_output_root.iterdir() if path.is_dir()} if default_output_root.exists() else set()
    created_dir: Path | None = None
    try:
        rc = export_main([
            "--workspace",
            str(ws.root),
            "--base-run-id",
            str(ws.base_run_id),
        ])

        assert rc == 0
        captured = capsys.readouterr()
        summary = _load_summary_json(captured.out)

        output_dir = Path(str(summary["output_dir"]))
        index_path = Path(str(summary["index_path"]))
        manifest_path = Path(str(summary["manifest_path"]))
        created_dir = output_dir

        assert output_dir.parent == default_output_root
        try:
            datetime.strptime(output_dir.name, "%Y%m%d-%H%M%S")
        except ValueError:
            datetime.strptime(output_dir.name, "%Y%m%d-%H%M%S-%f")

        assert index_path == output_dir / "index.html"
        assert manifest_path == output_dir / "manifest.json"
        assert index_path.is_file()
        assert manifest_path.is_file()

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert "assignment_summary" in manifest

        after_dirs = {path for path in default_output_root.iterdir() if path.is_dir()}
        assert output_dir in (after_dirs - before_dirs)
    finally:
        ws.close()
        if created_dir is not None and created_dir.exists():
            shutil.rmtree(created_dir)
