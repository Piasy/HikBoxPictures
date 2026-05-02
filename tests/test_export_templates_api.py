from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import time

import httpx
import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "people_gallery_scan"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"


def _run_hikbox(
    *args: str,
    cwd: Path | None = None,
    env_updates: dict[str, str] | None = None,
    pythonpath_prepend: list[Path] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    pythonpath_parts = [str(path) for path in (pythonpath_prepend or [])]
    pythonpath_parts.append(str(REPO_ROOT))
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    if env_updates:
        env.update(env_updates)
    return subprocess.run(
        [sys.executable, "-m", "hikbox_pictures", *args],
        cwd=cwd or REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _spawn_hikbox(
    *args: str,
    cwd: Path | None = None,
    env_updates: dict[str, str] | None = None,
    pythonpath_prepend: list[Path] | None = None,
) -> subprocess.Popen[str]:
    env = os.environ.copy()
    pythonpath_parts = [str(path) for path in (pythonpath_prepend or [])]
    pythonpath_parts.append(str(REPO_ROOT))
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    if env_updates:
        env.update(env_updates)
    return subprocess.Popen(
        [sys.executable, "-m", "hikbox_pictures", *args],
        cwd=cwd or REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _init_workspace(workspace: Path, external_root: Path) -> subprocess.CompletedProcess[str]:
    return _run_hikbox(
        "init",
        "--workspace",
        str(workspace),
        "--external-root",
        str(external_root),
    )


def _add_source(workspace: Path, source_dir: Path) -> subprocess.CompletedProcess[str]:
    return _run_hikbox(
        "source",
        "add",
        "--workspace",
        str(workspace),
        str(source_dir),
    )


def _prepare_workspace_models(workspace: Path) -> None:
    source_root = _find_model_root()
    target_root = workspace / ".hikbox" / "models" / "insightface"
    if target_root.exists():
        shutil.rmtree(target_root)
    shutil.copytree(source_root, target_root)


def _find_model_root() -> Path:
    candidates = [REPO_ROOT / ".insightface", Path.home() / ".insightface"]
    candidates.extend(parent / ".insightface" for parent in REPO_ROOT.parents)
    for candidate in candidates:
        if (candidate / "models" / "buffalo_l" / "det_10g.onnx").exists():
            return candidate
    raise AssertionError("缺少 InsightFace buffalo_l 模型目录，无法执行集成测试")


def _load_manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(sock.getsockname()[1])


def _wait_for_http_ready(base_url: str) -> None:
    deadline = time.time() + 30
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            response = httpx.get(base_url, follow_redirects=True, timeout=1.0)
            if response.status_code < 500:
                return
        except Exception as exc:
            last_error = exc
        time.sleep(0.2)
    raise AssertionError(f"等待服务可用超时: {base_url}; last_error={last_error!r}")


def _terminate_process(process: subprocess.Popen[str]) -> tuple[str, str]:
    import signal
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    try:
        stdout_text, stderr_text = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout_text, stderr_text = process.communicate(timeout=30)
    return stdout_text, stderr_text


def _fetch_all(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(db_path)
    try:
        return [tuple(row) for row in connection.execute(sql, params).fetchall()]
    finally:
        connection.close()


def _expected_target_mapping(library_db: Path, manifest: dict[str, object]) -> dict[str, str]:
    rows = _fetch_all(
        library_db,
        """
        SELECT
          assets.file_name,
          person_face_assignments.person_id
        FROM person_face_assignments
        INNER JOIN face_observations
          ON face_observations.id = person_face_assignments.face_observation_id
        INNER JOIN assets
          ON assets.id = face_observations.asset_id
        WHERE person_face_assignments.active = 1
        ORDER BY assets.file_name ASC
        """,
    )
    assignment_rows: dict[str, list[str]] = {}
    for file_name, person_id in rows:
        assignment_rows.setdefault(str(file_name), []).append(str(person_id))

    mapping: dict[str, str] = {}
    for label in manifest["expected_person_groups"]:
        observed_person_ids: set[str] = set()
        for asset in manifest["assets"]:
            if asset["expected_target_people"] != [label]:
                continue
            file_name = str(asset["file"])
            assigned = assignment_rows.get(file_name, [])
            if not assigned:
                continue
            observed_person_ids.update(assigned)
        assert observed_person_ids, f"{label} 缺少 target assignment"
        assert len(observed_person_ids) == 1, observed_person_ids
        mapping[str(label)] = next(iter(observed_person_ids))
    return mapping




def _name_person_via_api(base_url: str, person_id: str, display_name: str) -> None:
    response = httpx.post(
        f"{base_url}/people/{person_id}/name",
        data={"display_name": display_name},
        follow_redirects=False,
        timeout=5.0,
    )
    assert response.status_code in (302, 303)


def _merge_people_via_api(base_url: str, person_ids: list[str]) -> None:
    response = httpx.post(
        f"{base_url}/people/merge",
        data={"person_id": person_ids},
        follow_redirects=False,
        timeout=5.0,
    )
    assert response.status_code == 303


def _create_template_via_api(
    base_url: str,
    *,
    name: str,
    person_ids: list[str],
    output_root: str,
) -> dict[str, object]:
    response = httpx.post(
        f"{base_url}/api/export-templates",
        data={
            "name": name,
            "output_root": output_root,
            "person_id": person_ids,
        },
        timeout=5.0,
    )
    response.raise_for_status()
    return response.json()


def _list_templates_via_api(base_url: str) -> list[dict[str, object]]:
    response = httpx.get(f"{base_url}/api/export-templates", timeout=5.0)
    response.raise_for_status()
    return response.json()["templates"]


class TestExportTemplateCreation:
    def test_create_template_success(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = _find_free_port()
        process = _spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            _wait_for_http_ready(f"{base_url}/")

            _name_person_via_api(base_url, alex_id, "Alex Chen")
            _name_person_via_api(base_url, blair_id, "Blair Lin")

            response = httpx.post(
                f"{base_url}/api/export-templates",
                data={
                    "name": "Alex & Blair",
                    "output_root": output_root,
                    "person_id": [alex_id, blair_id],
                },
                timeout=5.0,
            )
            assert response.status_code == 200
            result = response.json()
            assert "template_id" in result

            template_rows = _fetch_all(library_db, "SELECT template_id, name, output_root, status FROM export_template")
            assert len(template_rows) == 1
            assert template_rows[0][1] == "Alex & Blair"
            assert template_rows[0][2] == output_root
            assert template_rows[0][3] == "active"

            person_rows = _fetch_all(library_db, "SELECT template_id, person_id FROM export_template_person")
            assert len(person_rows) == 2
            person_ids_in_db = {str(row[1]) for row in person_rows}
            assert person_ids_in_db == {alex_id, blair_id}
        finally:
            _terminate_process(process)

    def test_create_template_rejects_zero_or_one_person(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]

        port = _find_free_port()
        process = _spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            _wait_for_http_ready(f"{base_url}/")
            _name_person_via_api(base_url, alex_id, "Alex Chen")

            for person_ids in [[], [alex_id]]:
                snapshot_before = _fetch_all(library_db, "SELECT COUNT(*) FROM export_template")
                response = httpx.post(
                    f"{base_url}/api/export-templates",
                    data={
                        "name": "Test",
                        "output_root": output_root,
                        "person_id": person_ids,
                    },
                    timeout=5.0,
                )
                assert response.status_code == 400
                snapshot_after = _fetch_all(library_db, "SELECT COUNT(*) FROM export_template")
                assert snapshot_before == snapshot_after
        finally:
            _terminate_process(process)

    def test_create_template_rejects_missing_or_relative_or_uncreatable_output_root(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = _find_free_port()
        process = _spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        try:
            _wait_for_http_ready(f"{base_url}/")
            _name_person_via_api(base_url, alex_id, "Alex Chen")
            _name_person_via_api(base_url, blair_id, "Blair Lin")

            # Create a file to block directory creation
            blocked_path = tmp_path / "blocked-file"
            blocked_path.write_text("block")

            invalid_cases = [
                ("", "output_root missing"),
                ("relative/path", "relative path"),
                (str(blocked_path / "subdir"), "uncreatable output_root"),
            ]
            for output_root, _desc in invalid_cases:
                snapshot_before = _fetch_all(library_db, "SELECT COUNT(*) FROM export_template")
                response = httpx.post(
                    f"{base_url}/api/export-templates",
                    data={
                        "name": "Test",
                        "output_root": output_root,
                        "person_id": [alex_id, blair_id],
                    },
                    timeout=5.0,
                )
                assert response.status_code == 400, f"{_desc}: {response.status_code}"
                snapshot_after = _fetch_all(library_db, "SELECT COUNT(*) FROM export_template")
                assert snapshot_before == snapshot_after
        finally:
            _terminate_process(process)

    def test_create_template_rejects_inactive_or_anonymous_person(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        casey_id = target_person_ids["target_casey"]

        port = _find_free_port()
        process = _spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            _wait_for_http_ready(f"{base_url}/")
            _name_person_via_api(base_url, alex_id, "Alex Chen")
            # casey remains unnamed; merge makes casey inactive and anonymous
            _merge_people_via_api(base_url, [alex_id, casey_id])

            casey_is_active = _fetch_all(library_db, "SELECT status FROM person WHERE id = ?", (casey_id,))[0][0]
            assert casey_is_active == "inactive"

            response = httpx.post(
                f"{base_url}/api/export-templates",
                data={
                    "name": "Test",
                    "output_root": output_root,
                    "person_id": [alex_id, casey_id],
                },
                timeout=5.0,
            )
            assert response.status_code == 400
            assert _fetch_all(library_db, "SELECT COUNT(*) FROM export_template")[0][0] == 0
        finally:
            _terminate_process(process)

    def test_create_template_rejects_active_anonymous_person(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        casey_id = target_person_ids["target_casey"]

        port = _find_free_port()
        process = _spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            _wait_for_http_ready(f"{base_url}/")
            _name_person_via_api(base_url, alex_id, "Alex Chen")
            # casey remains unnamed (anonymous) but still active

            casey_is_active = _fetch_all(library_db, "SELECT status FROM person WHERE id = ?", (casey_id,))[0][0]
            assert casey_is_active == "active"
            casey_display_name = _fetch_all(library_db, "SELECT display_name FROM person WHERE id = ?", (casey_id,))[0][0]
            assert casey_display_name is None

            snapshot_before = _fetch_all(library_db, "SELECT COUNT(*) FROM export_template")
            response = httpx.post(
                f"{base_url}/api/export-templates",
                data={
                    "name": "Test",
                    "output_root": output_root,
                    "person_id": [alex_id, casey_id],
                },
                timeout=5.0,
            )
            assert response.status_code == 400
            snapshot_after = _fetch_all(library_db, "SELECT COUNT(*) FROM export_template")
            assert snapshot_before == snapshot_after
        finally:
            _terminate_process(process)

    def test_create_template_dedup_by_person_ids_and_output_root(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = _find_free_port()
        process = _spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            _wait_for_http_ready(f"{base_url}/")
            _name_person_via_api(base_url, alex_id, "Alex Chen")
            _name_person_via_api(base_url, blair_id, "Blair Lin")

            _create_template_via_api(base_url, name="First", person_ids=[alex_id, blair_id], output_root=output_root)
            response = httpx.post(
                f"{base_url}/api/export-templates",
                data={
                    "name": "Second",
                    "output_root": output_root,
                    "person_id": [blair_id, alex_id],
                },
                timeout=5.0,
            )
            assert response.status_code == 400
            assert _fetch_all(library_db, "SELECT COUNT(*) FROM export_template")[0][0] == 1
        finally:
            _terminate_process(process)

    def test_create_template_rejects_blank_name(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = _find_free_port()
        process = _spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            _wait_for_http_ready(f"{base_url}/")
            _name_person_via_api(base_url, alex_id, "Alex Chen")
            _name_person_via_api(base_url, blair_id, "Blair Lin")

            for name in ["", "   ", "\t"]:
                response = httpx.post(
                    f"{base_url}/api/export-templates",
                    data={
                        "name": name,
                        "output_root": output_root,
                        "person_id": [alex_id, blair_id],
                    },
                    timeout=5.0,
                )
                assert response.status_code == 400
            assert _fetch_all(library_db, "SELECT COUNT(*) FROM export_template")[0][0] == 0
        finally:
            _terminate_process(process)

    def test_template_stores_person_ids_not_display_names(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = _find_free_port()
        process = _spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            _wait_for_http_ready(f"{base_url}/")
            _name_person_via_api(base_url, alex_id, "Alex Chen")
            _name_person_via_api(base_url, blair_id, "Blair Lin")

            _create_template_via_api(base_url, name="Test", person_ids=[alex_id, blair_id], output_root=output_root)

            person_rows = _fetch_all(library_db, "SELECT person_id FROM export_template_person")
            assert {str(row[0]) for row in person_rows} == {alex_id, blair_id}

            _name_person_via_api(base_url, alex_id, "Alex Renamed")

            person_rows_after = _fetch_all(library_db, "SELECT person_id FROM export_template_person")
            assert {str(row[0]) for row in person_rows_after} == {alex_id, blair_id}
        finally:
            _terminate_process(process)

    def test_api_list_returns_status(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = _find_free_port()
        process = _spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            _wait_for_http_ready(f"{base_url}/")
            _name_person_via_api(base_url, alex_id, "Alex Chen")
            _name_person_via_api(base_url, blair_id, "Blair Lin")

            _create_template_via_api(base_url, name="Test", person_ids=[alex_id, blair_id], output_root=output_root)

            templates = _list_templates_via_api(base_url)
            assert len(templates) == 1
            assert templates[0]["status"] == "active"
            assert templates[0]["name"] == "Test"
            assert templates[0]["output_root"] == output_root
            assert templates[0]["person_count"] == 2
        finally:
            _terminate_process(process)


class TestExportTemplateCascadeInvalidation:
    def test_merge_winner_absorption_keeps_template_active(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]
        casey_id = target_person_ids["target_casey"]

        port = _find_free_port()
        process = _spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            _wait_for_http_ready(f"{base_url}/")
            _name_person_via_api(base_url, alex_id, "Alex Chen")
            _name_person_via_api(base_url, blair_id, "Blair Lin")
            # casey remains unnamed (anonymous)

            _create_template_via_api(base_url, name="Alex & Blair", person_ids=[alex_id, blair_id], output_root=output_root)

            # Merge named winner (alex) with anonymous loser (casey); template stays active.
            _merge_people_via_api(base_url, [alex_id, casey_id])

            templates = _list_templates_via_api(base_url)
            assert len(templates) == 1
            assert templates[0]["status"] == "active"
        finally:
            _terminate_process(process)

    def test_exclusion_emptying_person_invalidates_template(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = _find_free_port()
        process = _spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            _wait_for_http_ready(f"{base_url}/")
            _name_person_via_api(base_url, alex_id, "Alex Chen")
            _name_person_via_api(base_url, blair_id, "Blair Lin")

            _create_template_via_api(base_url, name="Alex & Blair", person_ids=[alex_id, blair_id], output_root=output_root)

            assignment_ids = [
                int(row[0])
                for row in _fetch_all(
                    library_db,
                    "SELECT id FROM person_face_assignments WHERE person_id = ? AND active = 1",
                    (alex_id,),
                )
            ]
            assert assignment_ids

            response = httpx.post(
                f"{base_url}/people/{alex_id}/exclude",
                data={"assignment_id": assignment_ids},
                follow_redirects=False,
                timeout=5.0,
            )
            assert response.status_code == 303

            templates = _list_templates_via_api(base_url)
            assert len(templates) == 1
            assert templates[0]["status"] == "invalid"
        finally:
            _terminate_process(process)
