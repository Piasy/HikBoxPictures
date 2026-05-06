from __future__ import annotations

import re
from pathlib import Path
import sqlite3

import httpx
from playwright.sync_api import Page
from playwright.sync_api import expect
from playwright.sync_api import sync_playwright

import pytest

from tests.helpers import (
    add_source,
    fetch_all,
    find_free_port,
    run_hikbox,
    spawn_hikbox,
    terminate_process,
    wait_for_http_ready,
)
from tests.people_gallery.people_gallery_helpers import (
    assert_exclusion_prg_flow,
    iter_rendered_assignment_cards,
    read_active_assignment_details,
    read_active_assignment_ids,
    read_active_people,
    read_face_assignment_rows,
    read_person_face_exclusions,
    read_person_record,
    submit_exclusion_from_detail,
)


def test_people_gallery_exclude_single_sample_prg_rescan_and_db_truth(scanned_workspace, tmp_path: Path) -> None:
    workspace, _, library_db, manifest, target_person_ids = scanned_workspace
    alex_person_id = target_person_ids["target_alex"]
    alex_assignments_before = read_active_assignment_details(library_db, alex_person_id)
    excluded_assignment = alex_assignments_before[0]
    exclusions_before = read_person_face_exclusions(library_db)

    port = find_free_port()
    process = spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
        "--person-detail-page-size",
        "50",
    )
    base_url = f"http://127.0.0.1:{port}"

    try:
        wait_for_http_ready(f"{base_url}/")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            response_log: list[dict[str, object]] = []

            def _record_response(response: object) -> None:
                request = response.request
                response_log.append(
                    {
                        "method": str(request.method),
                        "url": str(response.url),
                        "status": int(response.status),
                    }
                )

            page.on("response", _record_response)

            response_start = len(response_log)
            submit_exclusion_from_detail(
                page,
                base_url=base_url,
                person_id=alex_person_id,
                assignment_ids=[int(excluded_assignment["assignment_id"])],
            )
            assert_exclusion_prg_flow(
                responses=response_log[response_start:],
                base_url=base_url,
                person_id=alex_person_id,
                redirected_to_home=False,
            )
            expect(page.get_by_role("status")).to_contain_text("已排除")
            expect(
                page.locator(f"[data-assignment-id='{excluded_assignment['assignment_id']}']")
            ).to_have_count(0)
            expect(page.locator("[data-assignment-id]")).to_have_count(len(alex_assignments_before) - 1)
            browser.close()
    finally:
        terminate_process(process)

    alex_assignment_ids_after = read_active_assignment_ids(library_db, alex_person_id)
    assert alex_assignment_ids_after == [
        int(item["assignment_id"])
        for item in alex_assignments_before
        if int(item["assignment_id"]) != int(excluded_assignment["assignment_id"])
    ]
    excluded_face_rows = read_face_assignment_rows(library_db, int(excluded_assignment["face_observation_id"]))
    assert any(
        row["assignment_id"] == int(excluded_assignment["assignment_id"]) and row["active"] is False
        for row in excluded_face_rows
    )
    exclusion_rows = read_person_face_exclusions(
        library_db,
        face_observation_id=int(excluded_assignment["face_observation_id"]),
        excluded_person_id=alex_person_id,
    )
    assert len(exclusion_rows) == 1
    assert exclusion_rows[0]["source_assignment_id"] == int(excluded_assignment["assignment_id"])
    assert exclusion_rows[0]["created_at"]

    rescan_result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert rescan_result.returncode == 0, rescan_result.stderr
    exclusion_rows_after_rescan = read_person_face_exclusions(
        library_db,
        face_observation_id=int(excluded_assignment["face_observation_id"]),
        excluded_person_id=alex_person_id,
    )
    assert exclusion_rows_after_rescan == exclusion_rows
    assert read_person_face_exclusions(library_db) == exclusions_before + exclusion_rows
    assert not any(
        row["active"] is True
        for row in read_face_assignment_rows(library_db, int(excluded_assignment["face_observation_id"]))
    )
    assert len(read_active_assignment_ids(library_db, alex_person_id)) == len(alex_assignments_before) - 1


def test_people_gallery_exclude_two_samples_updates_exact_active_set(scanned_workspace, tmp_path: Path) -> None:
    workspace, _, library_db, _, target_person_ids = scanned_workspace
    blair_person_id = target_person_ids["target_blair"]
    blair_assignments_before = read_active_assignment_details(library_db, blair_person_id)
    selected_assignments = blair_assignments_before[:2]
    selected_assignment_ids = {int(item["assignment_id"]) for item in selected_assignments}

    port = find_free_port()
    process = spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
        "--person-detail-page-size",
        "50",
    )
    base_url = f"http://127.0.0.1:{port}"

    try:
        wait_for_http_ready(f"{base_url}/")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            response_log: list[dict[str, object]] = []

            def _record_response(response: object) -> None:
                request = response.request
                response_log.append(
                    {
                        "method": str(request.method),
                        "url": str(response.url),
                        "status": int(response.status),
                    }
                )

            page.on("response", _record_response)

            response_start = len(response_log)
            submit_exclusion_from_detail(
                page,
                base_url=base_url,
                person_id=blair_person_id,
                assignment_ids=sorted(selected_assignment_ids),
            )
            assert_exclusion_prg_flow(
                responses=response_log[response_start:],
                base_url=base_url,
                person_id=blair_person_id,
                redirected_to_home=False,
            )
            rendered_assignment_ids = {
                assignment_id for assignment_id, _, _ in iter_rendered_assignment_cards(page)
            }
            expected_assignment_ids = {
                int(item["assignment_id"])
                for item in blair_assignments_before
                if int(item["assignment_id"]) not in selected_assignment_ids
            }
            assert rendered_assignment_ids == expected_assignment_ids
            browser.close()
    finally:
        terminate_process(process)

    assert set(read_active_assignment_ids(library_db, blair_person_id)) == {
        int(item["assignment_id"])
        for item in blair_assignments_before
        if int(item["assignment_id"]) not in selected_assignment_ids
    }
    for selected in selected_assignments:
        exclusion_rows = read_person_face_exclusions(
            library_db,
            face_observation_id=int(selected["face_observation_id"]),
            excluded_person_id=blair_person_id,
        )
        assert len(exclusion_rows) == 1
        assert exclusion_rows[0]["source_assignment_id"] == int(selected["assignment_id"])


def test_people_gallery_exclude_all_samples_redirects_home_and_person_detail_404(scanned_workspace, tmp_path: Path) -> None:
    workspace, _, library_db, _, _ = scanned_workspace
    active_people_before = read_active_people(library_db)
    target_person_id = next(
        person_id
        for person_id, person in active_people_before.items()
        if int(person["sample_count"]) == 18
    )
    target_assignments = read_active_assignment_details(library_db, target_person_id)
    assert len(target_assignments) == 18

    port = find_free_port()
    process = spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
        "--person-detail-page-size",
        "50",
    )
    base_url = f"http://127.0.0.1:{port}"

    try:
        wait_for_http_ready(f"{base_url}/")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            response_log: list[dict[str, object]] = []

            def _record_response(response: object) -> None:
                request = response.request
                response_log.append(
                    {
                        "method": str(request.method),
                        "url": str(response.url),
                        "status": int(response.status),
                    }
                )

            page.on("response", _record_response)

            response_start = len(response_log)
            submit_exclusion_from_detail(
                page,
                base_url=base_url,
                person_id=target_person_id,
                assignment_ids=[int(item["assignment_id"]) for item in target_assignments],
            )
            assert_exclusion_prg_flow(
                responses=response_log[response_start:],
                base_url=base_url,
                person_id=target_person_id,
                redirected_to_home=True,
            )
            expect(page.locator(f"[data-person-id='{target_person_id}']")).to_have_count(0)
            browser.close()

        missing_detail = httpx.get(f"{base_url}/people/{target_person_id}", timeout=5.0)
        assert missing_detail.status_code == 404
        assert "人物不存在" in missing_detail.text or target_person_id in missing_detail.text
    finally:
        terminate_process(process)

    assert read_active_assignment_ids(library_db, target_person_id) == []
    assert read_person_record(library_db, target_person_id)["status"] == "inactive"
    assert len(
        read_person_face_exclusions(library_db, excluded_person_id=target_person_id)
    ) == len(target_assignments)
    assert all(Path(item["context_path"]).exists() for item in target_assignments)
    remaining_face_rows = fetch_all(
        library_db,
        """
        SELECT COUNT(*)
        FROM face_observations
        WHERE id IN ({placeholders})
        """.format(
            placeholders=", ".join("?" for _ in target_assignments)
        ),
        tuple(int(item["face_observation_id"]) for item in target_assignments),
    )
    assert int(remaining_face_rows[0][0]) == len(target_assignments)
