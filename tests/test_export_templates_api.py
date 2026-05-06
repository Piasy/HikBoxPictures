from __future__ import annotations

from pathlib import Path
import sqlite3

import httpx
import pytest

from tests.helpers import (
    create_template_via_api,
    fetch_all,
    find_free_port,
    list_templates_via_api,
    merge_people_via_api,
    name_person_via_api,
    spawn_hikbox,
    terminate_process,
    wait_for_http_ready,
)


def _assert_name_ok(base_url: str, person_id: str, display_name: str) -> None:
    resp = name_person_via_api(base_url, person_id, display_name)
    assert resp.status_code in (302, 303)


def _assert_merge_ok(base_url: str, person_ids: list[str]) -> None:
    resp = merge_people_via_api(base_url, person_ids)
    assert resp.status_code == 303


class TestExportTemplateCreation:
    def test_create_template_success(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            wait_for_http_ready(f"{base_url}/")

            _assert_name_ok(base_url, alex_id, "Alex Chen")
            _assert_name_ok(base_url, blair_id, "Blair Lin")

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

            template_rows = fetch_all(library_db, "SELECT template_id, name, output_root, status FROM export_template")
            assert len(template_rows) == 1
            assert template_rows[0][1] == "Alex & Blair"
            assert template_rows[0][2] == output_root
            assert template_rows[0][3] == "active"

            person_rows = fetch_all(library_db, "SELECT template_id, person_id FROM export_template_person")
            assert len(person_rows) == 2
            person_ids_in_db = {str(row[1]) for row in person_rows}
            assert person_ids_in_db == {alex_id, blair_id}
        finally:
            terminate_process(process)

    def test_create_template_rejects_zero_or_one_person(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            wait_for_http_ready(f"{base_url}/")
            _assert_name_ok(base_url, alex_id, "Alex Chen")

            for person_ids in [[], [alex_id]]:
                snapshot_before = fetch_all(library_db, "SELECT COUNT(*) FROM export_template")
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
                snapshot_after = fetch_all(library_db, "SELECT COUNT(*) FROM export_template")
                assert snapshot_before == snapshot_after
        finally:
            terminate_process(process)

    def test_create_template_rejects_missing_or_relative_or_uncreatable_output_root(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        try:
            wait_for_http_ready(f"{base_url}/")
            _assert_name_ok(base_url, alex_id, "Alex Chen")
            _assert_name_ok(base_url, blair_id, "Blair Lin")

            # Create a file to block directory creation
            blocked_path = tmp_path / "blocked-file"
            blocked_path.write_text("block")

            invalid_cases = [
                ("", "output_root missing"),
                ("relative/path", "relative path"),
                (str(blocked_path / "subdir"), "uncreatable output_root"),
            ]
            for output_root, _desc in invalid_cases:
                snapshot_before = fetch_all(library_db, "SELECT COUNT(*) FROM export_template")
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
                snapshot_after = fetch_all(library_db, "SELECT COUNT(*) FROM export_template")
                assert snapshot_before == snapshot_after
        finally:
            terminate_process(process)

    def test_create_template_rejects_inactive_or_anonymous_person(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        casey_id = target_person_ids["target_casey"]

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            wait_for_http_ready(f"{base_url}/")
            _assert_name_ok(base_url, alex_id, "Alex Chen")
            # casey remains unnamed; merge makes casey inactive and anonymous
            _assert_merge_ok(base_url, [alex_id, casey_id])

            casey_is_active = fetch_all(library_db, "SELECT status FROM person WHERE id = ?", (casey_id,))[0][0]
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
            assert fetch_all(library_db, "SELECT COUNT(*) FROM export_template")[0][0] == 0
        finally:
            terminate_process(process)

    def test_create_template_rejects_active_anonymous_person(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        casey_id = target_person_ids["target_casey"]

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            wait_for_http_ready(f"{base_url}/")
            _assert_name_ok(base_url, alex_id, "Alex Chen")
            # casey remains unnamed (anonymous) but still active

            casey_is_active = fetch_all(library_db, "SELECT status FROM person WHERE id = ?", (casey_id,))[0][0]
            assert casey_is_active == "active"
            casey_display_name = fetch_all(library_db, "SELECT display_name FROM person WHERE id = ?", (casey_id,))[0][0]
            assert casey_display_name is None

            snapshot_before = fetch_all(library_db, "SELECT COUNT(*) FROM export_template")
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
            snapshot_after = fetch_all(library_db, "SELECT COUNT(*) FROM export_template")
            assert snapshot_before == snapshot_after
        finally:
            terminate_process(process)

    def test_create_template_dedup_by_person_ids_and_output_root(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            wait_for_http_ready(f"{base_url}/")
            _assert_name_ok(base_url, alex_id, "Alex Chen")
            _assert_name_ok(base_url, blair_id, "Blair Lin")

            create_template_via_api(base_url, name="First", person_ids=[alex_id, blair_id], output_root=output_root)
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
            assert fetch_all(library_db, "SELECT COUNT(*) FROM export_template")[0][0] == 1
        finally:
            terminate_process(process)

    def test_create_template_rejects_blank_name(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            wait_for_http_ready(f"{base_url}/")
            _assert_name_ok(base_url, alex_id, "Alex Chen")
            _assert_name_ok(base_url, blair_id, "Blair Lin")

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
            assert fetch_all(library_db, "SELECT COUNT(*) FROM export_template")[0][0] == 0
        finally:
            terminate_process(process)

    def test_template_stores_person_ids_not_display_names(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            wait_for_http_ready(f"{base_url}/")
            _assert_name_ok(base_url, alex_id, "Alex Chen")
            _assert_name_ok(base_url, blair_id, "Blair Lin")

            create_template_via_api(base_url, name="Test", person_ids=[alex_id, blair_id], output_root=output_root)

            person_rows = fetch_all(library_db, "SELECT person_id FROM export_template_person")
            assert {str(row[0]) for row in person_rows} == {alex_id, blair_id}

            _assert_name_ok(base_url, alex_id, "Alex Renamed")

            person_rows_after = fetch_all(library_db, "SELECT person_id FROM export_template_person")
            assert {str(row[0]) for row in person_rows_after} == {alex_id, blair_id}
        finally:
            terminate_process(process)

    def test_api_list_returns_status(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            wait_for_http_ready(f"{base_url}/")
            _assert_name_ok(base_url, alex_id, "Alex Chen")
            _assert_name_ok(base_url, blair_id, "Blair Lin")

            create_template_via_api(base_url, name="Test", person_ids=[alex_id, blair_id], output_root=output_root)

            templates = list_templates_via_api(base_url)
            assert len(templates) == 1
            assert templates[0]["status"] == "active"
            assert templates[0]["name"] == "Test"
            assert templates[0]["output_root"] == output_root
            assert templates[0]["person_count"] == 2
        finally:
            terminate_process(process)


class TestExportTemplateCascadeInvalidation:
    def test_merge_winner_absorption_keeps_template_active(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]
        casey_id = target_person_ids["target_casey"]

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            wait_for_http_ready(f"{base_url}/")
            _assert_name_ok(base_url, alex_id, "Alex Chen")
            _assert_name_ok(base_url, blair_id, "Blair Lin")
            # casey remains unnamed (anonymous)

            create_template_via_api(base_url, name="Alex & Blair", person_ids=[alex_id, blair_id], output_root=output_root)

            # Merge named winner (alex) with anonymous loser (casey); template stays active.
            _assert_merge_ok(base_url, [alex_id, casey_id])

            templates = list_templates_via_api(base_url)
            assert len(templates) == 1
            assert templates[0]["status"] == "active"
        finally:
            terminate_process(process)

    def test_exclusion_emptying_person_invalidates_template(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            wait_for_http_ready(f"{base_url}/")
            _assert_name_ok(base_url, alex_id, "Alex Chen")
            _assert_name_ok(base_url, blair_id, "Blair Lin")

            create_template_via_api(base_url, name="Alex & Blair", person_ids=[alex_id, blair_id], output_root=output_root)

            assignment_ids = [
                int(row[0])
                for row in fetch_all(
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

            templates = list_templates_via_api(base_url)
            assert len(templates) == 1
            assert templates[0]["status"] == "invalid"
        finally:
            terminate_process(process)
