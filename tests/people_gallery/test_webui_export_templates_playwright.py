from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time

from playwright.sync_api import Page
from playwright.sync_api import expect
from playwright.sync_api import sync_playwright


REPO_ROOT = Path(__file__).resolve().parents[2]
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
    import httpx
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


def _create_scanned_workspace(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, object], dict[str, str]]:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    manifest = _load_manifest()
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0
    scan_result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert scan_result.returncode == 0, scan_result.stderr
    library_db = workspace / ".hikbox" / "library.db"
    return workspace, external_root, library_db, manifest, _expected_target_mapping(library_db, manifest)


class TestExportTemplateWebUI:
    def test_create_template_and_list_shows_fields(self, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = _create_scanned_workspace(tmp_path)
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = _find_free_port()
        process = _spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            _wait_for_http_ready(f"{base_url}/")

            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 900})

                # Name alex and blair via API so they appear in selector
                import httpx
                httpx.post(f"{base_url}/people/{alex_id}/name", data={"display_name": "Alex Chen"}, follow_redirects=False, timeout=5.0)
                httpx.post(f"{base_url}/people/{blair_id}/name", data={"display_name": "Blair Lin"}, follow_redirects=False, timeout=5.0)

                # AC-1: Navigate from /people -> /exports -> /exports/new via UI links
                page.goto(f"{base_url}/people")
                page.locator("a[href='/exports']").click()
                expect(page).to_have_url(f"{base_url}/exports")
                page.locator("a[href='/exports/new']").click()
                expect(page).to_have_url(f"{base_url}/exports/new")

                page.fill("input#name", "Alex & Blair")
                page.fill("input#output_root", output_root)
                page.locator(f"article[data-person-id='{alex_id}'] input[type=checkbox]").check()
                page.locator(f"article[data-person-id='{blair_id}'] input[type=checkbox]").check()
                page.locator("button[type=submit]").click()
                expect(page).to_have_url(f"{base_url}/exports")

                # AC-5: List page shows all five fields
                row = page.locator("tr[data-template-id]").first
                expect(row.locator("[data-template-name]")).to_contain_text("Alex & Blair")
                expect(row.locator("[data-template-created-at]")).not_to_have_text("")
                expect(row.locator("[data-template-person-names]")).to_contain_text("Alex Chen")
                expect(row.locator("[data-template-person-names]")).to_contain_text("Blair Lin")
                expect(row.locator("[data-template-output-root]")).to_contain_text(output_root)
                expect(row.locator("[data-template-status]")).to_contain_text("active")

                browser.close()

            # DB assertions
            template_rows = _fetch_all(
                library_db,
                "SELECT template_id, name, output_root, status, created_at FROM export_template",
            )
            assert len(template_rows) == 1
            assert template_rows[0][1] == "Alex & Blair"
            assert template_rows[0][2] == output_root
            assert template_rows[0][3] == "active"
            assert template_rows[0][4] is not None

            person_rows = _fetch_all(library_db, "SELECT person_id FROM export_template_person")
            assert len(person_rows) == 2
            assert {str(row[0]) for row in person_rows} == {alex_id, blair_id}
        finally:
            _terminate_process(process)

    def test_person_selector_excludes_anonymous_and_inactive(self, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = _create_scanned_workspace(tmp_path)
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]
        casey_id = target_person_ids["target_casey"]

        port = _find_free_port()
        process = _spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        try:
            _wait_for_http_ready(f"{base_url}/")

            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 900})

                import httpx
                httpx.post(f"{base_url}/people/{alex_id}/name", data={"display_name": "Alex Chen"}, follow_redirects=False, timeout=5.0)
                httpx.post(f"{base_url}/people/{blair_id}/name", data={"display_name": "Blair Lin"}, follow_redirects=False, timeout=5.0)
                # casey remains anonymous
                # Merge alex with casey so casey becomes inactive
                httpx.post(f"{base_url}/people/merge", data={"person_id": [alex_id, casey_id]}, follow_redirects=False, timeout=5.0)
                casey_status = _fetch_all(library_db, "SELECT status FROM person WHERE id = ?", (casey_id,))[0][0]
                assert casey_status == "inactive"

                page.goto(f"{base_url}/exports/new")
                selector = page.locator("[data-person-selector]")
                expect(selector).to_be_visible()

                # AC-2: Selector only shows active named persons
                person_cards = selector.locator("article[data-person-id]")
                ids = []
                for i in range(person_cards.count()):
                    pid = person_cards.nth(i).get_attribute("data-person-id")
                    ids.append(pid)
                assert alex_id in ids
                assert blair_id in ids
                assert casey_id not in ids  # inactive
                # Any remaining anonymous person should also not be present
                for pid in ids:
                    display_name = selector.locator(f"article[data-person-id='{pid}'] [data-person-display-name]").inner_text()
                    assert display_name and display_name.strip() != ""

                browser.close()
        finally:
            _terminate_process(process)

    def test_form_error_feedback_and_value_preserve(self, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = _create_scanned_workspace(tmp_path)
        alex_id = target_person_ids["target_alex"]

        port = _find_free_port()
        process = _spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            _wait_for_http_ready(f"{base_url}/")

            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 900})

                import httpx
                httpx.post(f"{base_url}/people/{alex_id}/name", data={"display_name": "Alex Chen"}, follow_redirects=False, timeout=5.0)

                page.goto(f"{base_url}/exports/new")
                page.fill("input#name", "Test")
                page.fill("input#output_root", output_root)
                # Only select 1 person -> should fail
                page.locator(f"article[data-person-id='{alex_id}'] input[type=checkbox]").check()
                page.locator("button[type=submit]").click()

                # Should redirect back to /exports/new with error and preserved values
                assert page.url.startswith(f"{base_url}/exports/new")
                expect(page.locator("[role=alert]")).to_contain_text("至少选择 2 个人物")
                expect(page.locator("input#name")).to_have_value("Test")
                expect(page.locator("input#output_root")).to_have_value(output_root)
                # Checkbox should remain checked
                assert page.locator(f"article[data-person-id='{alex_id}'] input[type=checkbox]").is_checked()

                browser.close()
        finally:
            _terminate_process(process)

    def test_cascade_invalidation_shows_invalid_status(self, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = _create_scanned_workspace(tmp_path)
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = _find_free_port()
        process = _spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            _wait_for_http_ready(f"{base_url}/")

            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 900})

                import httpx
                httpx.post(f"{base_url}/people/{alex_id}/name", data={"display_name": "Alex Chen"}, follow_redirects=False, timeout=5.0)
                httpx.post(f"{base_url}/people/{blair_id}/name", data={"display_name": "Blair Lin"}, follow_redirects=False, timeout=5.0)

                # Create template
                page.goto(f"{base_url}/exports/new")
                page.fill("input#name", "Alex & Blair")
                page.fill("input#output_root", output_root)
                page.locator(f"article[data-person-id='{alex_id}'] input[type=checkbox]").check()
                page.locator(f"article[data-person-id='{blair_id}'] input[type=checkbox]").check()
                page.locator("button[type=submit]").click()
                expect(page).to_have_url(f"{base_url}/exports")
                expect(page.locator("tr[data-template-id] [data-template-status]")).to_contain_text("active")

                # Merge alex and blair; loser becomes inactive and template becomes invalid
                httpx.post(
                    f"{base_url}/people/merge",
                    data={"person_id": [alex_id, blair_id]},
                    follow_redirects=False,
                    timeout=5.0,
                )

                # Refresh and assert status changed to invalid
                page.goto(f"{base_url}/exports")
                expect(page.locator("tr[data-template-id] [data-template-status]")).to_contain_text("invalid")

                browser.close()

            # DB assertion
            db_status = _fetch_all(library_db, "SELECT status FROM export_template")[0][0]
            assert db_status == "invalid"
        finally:
            _terminate_process(process)

    def test_merge_winner_keeps_template_active(self, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = _create_scanned_workspace(tmp_path)
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]
        casey_id = target_person_ids["target_casey"]

        port = _find_free_port()
        process = _spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            _wait_for_http_ready(f"{base_url}/")

            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 900})

                import httpx
                httpx.post(f"{base_url}/people/{alex_id}/name", data={"display_name": "Alex Chen"}, follow_redirects=False, timeout=5.0)
                httpx.post(f"{base_url}/people/{blair_id}/name", data={"display_name": "Blair Lin"}, follow_redirects=False, timeout=5.0)
                # casey remains unnamed (anonymous)

                # Create template
                page.goto(f"{base_url}/exports/new")
                page.fill("input#name", "Alex & Blair")
                page.fill("input#output_root", output_root)
                page.locator(f"article[data-person-id='{alex_id}'] input[type=checkbox]").check()
                page.locator(f"article[data-person-id='{blair_id}'] input[type=checkbox]").check()
                page.locator("button[type=submit]").click()
                expect(page).to_have_url(f"{base_url}/exports")
                expect(page.locator("tr[data-template-id] [data-template-status]")).to_contain_text("active")

                # Merge named alex with anonymous casey; alex is winner, casey is loser
                httpx.post(f"{base_url}/people/merge", data={"person_id": [alex_id, casey_id]}, follow_redirects=False, timeout=5.0)

                # Refresh and assert template stays active
                page.goto(f"{base_url}/exports")
                expect(page.locator("tr[data-template-id] [data-template-status]")).to_contain_text("active")

                browser.close()

            # DB assertion
            db_status = _fetch_all(library_db, "SELECT status FROM export_template")[0][0]
            assert db_status == "active"
        finally:
            _terminate_process(process)

    def test_preview_page_grid_and_counts(self, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = _create_scanned_workspace(tmp_path)
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = _find_free_port()
        process = _spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            _wait_for_http_ready(f"{base_url}/")

            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 900})

                import httpx
                httpx.post(f"{base_url}/people/{alex_id}/name", data={"display_name": "Alex Chen"}, follow_redirects=False, timeout=5.0)
                httpx.post(f"{base_url}/people/{blair_id}/name", data={"display_name": "Blair Lin"}, follow_redirects=False, timeout=5.0)

                # Create template
                page.goto(f"{base_url}/exports/new")
                page.fill("input#name", "Alex & Blair")
                page.fill("input#output_root", output_root)
                page.locator(f"article[data-person-id='{alex_id}'] input[type=checkbox]").check()
                page.locator(f"article[data-person-id='{blair_id}'] input[type=checkbox]").check()
                page.locator("button[type=submit]").click()
                expect(page).to_have_url(f"{base_url}/exports")

                # Get template id from DOM and visit preview directly
                template_id = page.locator("tr[data-template-id]").first.get_attribute("data-template-id")
                page.goto(f"{base_url}/exports/{template_id}/preview")

                # Assert counts visible
                expect(page.locator("[data-preview-total]")).not_to_have_text("")
                expect(page.locator("[data-preview-only]")).not_to_have_text("")
                expect(page.locator("[data-preview-group]")).not_to_have_text("")

                # Assert grid CSS (6 columns on desktop)
                grid = page.locator("[data-preview-grid]").first
                grid_css = grid.evaluate("el => getComputedStyle(el).gridTemplateColumns")
                assert len(grid_css.split()) == 6

                # Fetch preview API to cross-check DOM structure
                preview = httpx.get(f"{base_url}/api/export-templates/{template_id}/preview", timeout=5.0).json()

                # AC-1 + AC-2: verify month buckets and per-asset data-person-id
                for month_bucket in preview["months"]:
                    month = month_bucket["month"]
                    section = page.locator(f"section[data-month='{month}']")
                    expect(section).to_be_visible()

                    for bucket in ("only", "group"):
                        assets = month_bucket[bucket]
                        if not assets:
                            continue
                        grid_locator = section.locator(f"div[data-preview-grid][data-bucket='{bucket}']")
                        expect(grid_locator).to_be_visible()

                        # Verify each article's data-person-id matches API representative_person_id
                        for asset in assets:
                            article = grid_locator.locator(f"article[data-asset-id='{asset['asset_id']}']")
                            expect(article).to_be_visible()
                            dom_person_id = article.get_attribute("data-person-id")
                            assert dom_person_id == asset["representative_person_id"], (
                                f"asset {asset['file_name']} data-person-id mismatch: "
                                f"dom={dom_person_id}, api={asset['representative_person_id']}"
                            )
                            # Also verify the displayed file name
                            expect(article.locator("[data-asset-file-name]")).to_contain_text(asset["file_name"])

                browser.close()
        finally:
            _terminate_process(process)

    def test_history_page_shows_run_details(self, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = _create_scanned_workspace(tmp_path)
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = _find_free_port()
        process = _spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            _wait_for_http_ready(f"{base_url}/")

            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 900})

                import httpx
                httpx.post(f"{base_url}/people/{alex_id}/name", data={"display_name": "Alex Chen"}, follow_redirects=False, timeout=5.0)
                httpx.post(f"{base_url}/people/{blair_id}/name", data={"display_name": "Blair Lin"}, follow_redirects=False, timeout=5.0)

                # Create template and execute via API
                response = httpx.post(
                    f"{base_url}/api/export-templates",
                    data={"name": "Alex & Blair", "output_root": output_root, "person_id": [alex_id, blair_id]},
                    timeout=5.0,
                )
                template_id = response.json()["template_id"]
                httpx.get(f"{base_url}/api/export-templates/{template_id}/preview", timeout=30.0)
                execute_resp = httpx.post(f"{base_url}/api/export-templates/{template_id}/execute", timeout=30.0)
                run_id = execute_resp.json()["run_id"]

                # Fetch run detail API to cross-check DOM
                run_detail = httpx.get(f"{base_url}/api/export-runs/{run_id}", timeout=5.0).json()

                # Visit history page
                page.goto(f"{base_url}/exports/{template_id}/history")

                row = page.locator("tr[data-run-id]").first
                expect(row.locator("[data-run-status]")).to_contain_text("completed")
                expect(row.locator("[data-run-copied]")).not_to_have_text("")
                expect(row.locator("[data-run-skipped]")).not_to_have_text("")

                # AC-6: deliveries detail list
                deliveries_section = page.locator(f"tr[data-run-deliveries='{run_id}']")
                expect(deliveries_section).to_be_visible()

                deliveries = run_detail["deliveries"]
                assert len(deliveries) > 0
                for d in deliveries:
                    delivery_row = deliveries_section.locator(f"tr[data-delivery-id='{d['delivery_id']}']")
                    expect(delivery_row).to_be_visible()
                    expect(delivery_row.locator("[data-delivery-target-path]")).to_contain_text(d["target_path"])
                    expect(delivery_row.locator("[data-delivery-result]")).to_contain_text(d["result"])
                    expect(delivery_row.locator("[data-delivery-mov-result]")).to_contain_text(d["mov_result"])

                browser.close()
        finally:
            _terminate_process(process)
