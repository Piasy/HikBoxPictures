from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path


TERMINAL_SCAN_STATUS = {"completed", "failed", "interrupted", "abandoned"}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _cli_python() -> Path:
    repo_root = _project_root()
    direct_candidate = repo_root / ".venv" / "bin" / "python"
    if direct_candidate.exists():
        return direct_candidate

    git_common = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--git-common-dir"],
        text=True,
        capture_output=True,
        check=False,
    )
    if git_common.returncode == 0:
        common_dir_raw = git_common.stdout.strip()
        if common_dir_raw:
            common_dir = Path(common_dir_raw)
            if not common_dir.is_absolute():
                common_dir = (repo_root / common_dir).resolve()
            repo_root_candidate = common_dir.parent / ".venv" / "bin" / "python"
            if repo_root_candidate.exists():
                return repo_root_candidate

    raise AssertionError("未找到可用 .venv/bin/python")


def _run_cli(
    workspace: Path,
    *args: str,
    json_output: bool = True,
    timeout_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [str(_cli_python()), "-m", "hikbox_pictures.cli"]
    if json_output:
        cmd.append("--json")
    cmd.extend(args)
    cmd.extend(["--workspace", str(workspace)])
    return subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout_seconds,
    )


def _ok_data(proc: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert proc.returncode == 0, f"命令失败: rc={proc.returncode}, stdout={proc.stdout}, stderr={proc.stderr}"
    payload = json.loads(proc.stdout)
    assert payload.get("ok") is True, f"命令未返回 ok=true: {payload}"
    data = payload.get("data")
    assert isinstance(data, dict), f"data 不是对象: {payload}"
    return data


def _err_payload(proc: subprocess.CompletedProcess[str]) -> dict[str, object]:
    text = proc.stderr.strip() or proc.stdout.strip()
    assert text, f"预期错误输出，但 stdout/stderr 都为空: rc={proc.returncode}"
    return json.loads(text)


def _wait_scan_status(workspace: Path, session_id: int, timeout_seconds: int) -> dict[str, object]:
    deadline = time.time() + timeout_seconds
    latest: dict[str, object] = {}
    while time.time() < deadline:
        latest = _ok_data(_run_cli(workspace, "scan", "status", "--session-id", str(session_id)))
        if str(latest["status"]) in TERMINAL_SCAN_STATUS:
            return latest
        time.sleep(1)
    raise AssertionError(f"扫描在 {timeout_seconds}s 内未进入终态，最后状态: {latest}")


def _named_person_ids(items: list[dict[str, object]]) -> list[int]:
    ids: list[int] = []
    for item in items:
        if bool(item.get("is_named")):
            ids.append(int(item["person_id"]))
    return ids


def test_readme_sections_2_to_6_e2e_flow_with_real_dataset(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external = (tmp_path / "external").resolve()
    exports = (tmp_path / "exports").resolve()
    exports.mkdir(parents=True, exist_ok=True)

    dataset_root = _project_root() / "tests" / "data" / "e2e-face-input"
    raw_source = (dataset_root / "raw").resolve()
    group_source = (dataset_root / "groups").resolve()
    assert raw_source.exists(), f"缺少测试数据目录: {raw_source}"
    assert group_source.exists(), f"缺少测试数据目录: {group_source}"

    # README §2 工作区初始化
    _ok_data(_run_cli(workspace, "init"))
    _ok_data(_run_cli(workspace, "config", "set-external-root", str(external)))
    show_data = _ok_data(_run_cli(workspace, "config", "show"))
    assert show_data["external_root"] == str(external)

    assert (workspace / ".hikbox" / "library.db").exists()
    assert (workspace / ".hikbox" / "embedding.db").exists()
    assert (workspace / ".hikbox" / "config.json").exists()
    assert (external / "artifacts" / "crops").exists()
    assert (external / "artifacts" / "aligned").exists()
    assert (external / "artifacts" / "context").exists()
    assert (external / "logs").exists()

    # README §3 扫描与服务
    _ok_data(_run_cli(workspace, "source", "add", str(raw_source), "--label", "e2e-raw"))
    _ok_data(_run_cli(workspace, "source", "add", str(group_source), "--label", "e2e-groups"))
    source_list = _ok_data(_run_cli(workspace, "source", "list"))
    source_items = source_list.get("items")
    assert isinstance(source_items, list)
    assert len(source_items) >= 2

    start_data = _ok_data(_run_cli(workspace, "scan", "start-or-resume"))
    session_id = int(start_data["session_id"])
    assert str(start_data["status"]) == "completed"
    assert bool(start_data["resumed"]) is False

    latest_status = _ok_data(_run_cli(workspace, "scan", "status", "--latest"))
    assert int(latest_status["session_id"]) == session_id
    assert str(latest_status["status"]) == "completed"

    scan_list = _ok_data(_run_cli(workspace, "scan", "list", "--limit", "20"))
    scan_items = scan_list.get("items")
    assert isinstance(scan_items, list)
    assert any(int(item["session_id"]) == session_id for item in scan_items)

    # README §4 人物维护
    people = _ok_data(_run_cli(workspace, "people", "list"))
    people_items = people.get("items")
    assert isinstance(people_items, list)
    assert len(people_items) >= 2, f"预期至少 2 个人物用于 merge，实际: {people_items}"

    first_person_id = int(people_items[0]["person_id"])
    renamed = f"README-E2E-{first_person_id}"
    rename_data = _ok_data(_run_cli(workspace, "people", "rename", str(first_person_id), renamed))
    assert int(rename_data["person_id"]) == first_person_id
    assert str(rename_data["display_name"]) == renamed
    assert bool(rename_data["is_named"]) is True

    first_person_detail = _ok_data(_run_cli(workspace, "people", "show", str(first_person_id)))
    assignment_ids = list(first_person_detail.get("assignment_face_ids") or [])
    assert assignment_ids, f"人物无可排除样本: person_id={first_person_id}"

    exclude_data = _ok_data(
        _run_cli(workspace, "people", "exclude", str(first_person_id), "--face-observation-id", str(int(assignment_ids[0])))
    )
    assert int(exclude_data["person_id"]) == first_person_id
    assert int(exclude_data["face_observation_id"]) == int(assignment_ids[0])
    assert int(exclude_data["pending_reassign"]) == 1

    if len(assignment_ids) >= 2:
        exclude_batch_data = _ok_data(
            _run_cli(
                workspace,
                "people",
                "exclude-batch",
                str(first_person_id),
                "--face-observation-ids",
                str(int(assignment_ids[1])),
            )
        )
        assert int(exclude_batch_data["person_id"]) == first_person_id
        assert int(exclude_batch_data["excluded_count"]) >= 1

    merge_ids = [int(people_items[0]["person_id"]), int(people_items[1]["person_id"])]
    merge_data = _ok_data(
        _run_cli(
            workspace,
            "people",
            "merge",
            "--selected-person-ids",
            ",".join(str(person_id) for person_id in merge_ids),
        )
    )
    assert int(merge_data["winner_person_id"]) in set(merge_ids)
    assert int(merge_data["merge_operation_id"]) > 0

    undo_data = _ok_data(_run_cli(workspace, "people", "undo-last-merge"))
    assert int(undo_data["merge_operation_id"]) == int(merge_data["merge_operation_id"])
    assert str(undo_data["status"]) == "undone"

    # README §5 导出
    template_list_before = _ok_data(_run_cli(workspace, "export", "template", "list"))
    assert isinstance(template_list_before.get("items"), list)

    named_ids = _named_person_ids(people_items)
    assert named_ids, f"预期存在可导出命名人物，实际: {people_items}"
    selected_person_ids = named_ids[:2] if len(named_ids) >= 2 else named_ids[:1]

    create_template = _ok_data(
        _run_cli(
            workspace,
            "export",
            "template",
            "create",
            "--name",
            "README-E2E-模板",
            "--output-root",
            str(exports),
            "--person-ids",
            ",".join(str(person_id) for person_id in selected_person_ids),
        )
    )
    template_id = int(create_template["template_id"])
    assert template_id > 0

    update_template = _ok_data(
        _run_cli(
            workspace,
            "export",
            "template",
            "update",
            str(template_id),
            "--name",
            "README-E2E-模板-更新",
            "--person-ids",
            ",".join(str(person_id) for person_id in selected_person_ids),
        )
    )
    assert int(update_template["template_id"]) == template_id
    assert bool(update_template["updated"]) is True

    run_data = _ok_data(_run_cli(workspace, "export", "run", str(template_id)))
    export_run_id = int(run_data["export_run_id"])
    assert str(run_data["status"]) == "completed"

    run_list = _ok_data(_run_cli(workspace, "export", "run-list", "--template-id", str(template_id), "--limit", "20"))
    run_items = run_list.get("items")
    assert isinstance(run_items, list)
    assert any(int(item["export_run_id"]) == export_run_id for item in run_items)

    # README §6 审计与维护命令
    audit_data = _ok_data(_run_cli(workspace, "audit", "list", "--scan-session-id", str(session_id)))
    assert isinstance(audit_data.get("items"), list)

    logs_data = _ok_data(_run_cli(workspace, "logs", "list", "--scan-session-id", str(session_id)))
    assert isinstance(logs_data.get("items"), list)

    vacuum_data = _ok_data(_run_cli(workspace, "db", "vacuum"))
    vacuumed = vacuum_data.get("vacuumed")
    assert isinstance(vacuumed, list)
    assert set(vacuumed) == {"library", "embedding"}
