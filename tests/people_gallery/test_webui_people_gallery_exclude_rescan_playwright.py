from __future__ import annotations

import re
from pathlib import Path

import httpx
from playwright.sync_api import Page
from playwright.sync_api import expect
from playwright.sync_api import sync_playwright

import pytest

from tests.helpers import (
    FIXTURE_DIR,
    add_source,
    fetch_all,
    find_free_port,
    run_hikbox,
    spawn_hikbox,
    terminate_process,
    wait_for_http_ready,
)
from tests.people_gallery.people_gallery_helpers import (
    FIXTURE_DIR_2,
    active_assignment_details_by_file_name,
    assert_exclusion_prg_flow,
    assert_name_prg_flow,
    load_incremental_manifest,
    manifest_files_for_target,
    open_person_detail_from_home,
    read_active_assignment_details,
    read_active_assignment_ids,
    read_active_people,
    read_face_assignment_rows,
    read_person_face_exclusions,
    read_person_record,
    submit_exclusion_from_detail,
    submit_merge_from_home,
    submit_name_form,
)


def test_people_gallery_full_exclusion_rescan_same_gallery_keeps_faces_unassigned(scanned_workspace, tmp_path: Path) -> None:
    workspace, _, library_db, _, _ = scanned_workspace
    active_people_before = read_active_people(library_db)
    target_person_id = next(
        person_id
        for person_id, person in active_people_before.items()
        if int(person["sample_count"]) == 18
    )
    target_assignments = read_active_assignment_details(library_db, target_person_id)
    excluded_face_ids = [int(item["face_observation_id"]) for item in target_assignments]

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
            submit_exclusion_from_detail(
                page,
                base_url=base_url,
                person_id=target_person_id,
                assignment_ids=[int(item["assignment_id"]) for item in target_assignments],
            )
            expect(page.locator(f"[data-person-id='{target_person_id}']")).to_have_count(0)
            browser.close()
    finally:
        terminate_process(process)

    assert read_person_record(library_db, target_person_id)["status"] == "inactive"

    rescan_result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert rescan_result.returncode == 0, rescan_result.stderr

    for face_observation_id in excluded_face_ids:
        assert not any(
            row["active"] is True for row in read_face_assignment_rows(library_db, face_observation_id)
        )


def test_people_gallery_full_exclusion_releases_name_for_reuse_via_real_page(scanned_workspace, tmp_path: Path) -> None:
    workspace, _, library_db, _, _ = scanned_workspace
    active_people_before = read_active_people(library_db)
    target_person_id = next(
        person_id
        for person_id, person in active_people_before.items()
        if int(person["sample_count"]) == 18
    )
    other_person_id = next(person_id for person_id in active_people_before if person_id != target_person_id)
    target_assignments = read_active_assignment_details(library_db, target_person_id)
    recycled_name = "可复用名称"

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

            detail_pattern = re.compile(rf"{re.escape(base_url)}/people/{re.escape(target_person_id)}(?:\\?.*)?$")
            open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=target_person_id)
            submit_name_form(page, detail_url_pattern=detail_pattern, display_name=recycled_name)
            expect(page.get_by_role("status")).to_contain_text("名称已保存")

            submit_exclusion_from_detail(
                page,
                base_url=base_url,
                person_id=target_person_id,
                assignment_ids=[int(item["assignment_id"]) for item in target_assignments],
            )
            expect(page.locator(f"[data-person-id='{target_person_id}']")).to_have_count(0)

            other_detail_pattern = re.compile(rf"{re.escape(base_url)}/people/{re.escape(other_person_id)}(?:\\?.*)?$")
            response_start = len(response_log)
            open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=other_person_id)
            submit_name_form(page, detail_url_pattern=other_detail_pattern, display_name=recycled_name)
            assert_name_prg_flow(
                responses=response_log[response_start:],
                base_url=base_url,
                person_id=other_person_id,
            )
            expect(page.get_by_role("status")).to_contain_text("名称已保存")
            expect(page.get_by_role("heading", name=recycled_name)).to_be_visible()
            browser.close()
    finally:
        terminate_process(process)

    target_record = read_person_record(library_db, target_person_id)
    other_record = read_person_record(library_db, other_person_id)
    assert target_record["display_name"] == recycled_name
    assert target_record["status"] == "inactive"
    assert other_record["display_name"] == recycled_name
    assert other_record["is_named"] is True


def test_people_gallery_exclusion_respects_incremental_source_and_accumulates_person_truth(scanned_workspace, tmp_path: Path) -> None:
    workspace, _, library_db, manifest, target_person_ids = scanned_workspace
    people_by_label = {str(person["label"]): person for person in manifest["people"]}
    incremental_manifest = load_incremental_manifest()
    alex_person_id = target_person_ids["target_alex"]
    blair_person_id = target_person_ids["target_blair"]
    alex_display_name = str(people_by_label["target_alex"]["display_name"])
    old_blair_files = set(manifest_files_for_target(manifest, "target_blair"))
    new_blair_files = set(manifest_files_for_target(incremental_manifest, "target_blair"))
    old_blair_details = read_active_assignment_details(library_db, blair_person_id)
    old_blair_assignment_ids = [int(item["assignment_id"]) for item in old_blair_details]
    old_blair_face_ids = {int(item["face_observation_id"]) for item in old_blair_details}

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
            alex_detail_pattern = re.compile(rf"{re.escape(base_url)}/people/{re.escape(alex_person_id)}(?:\\?.*)?$")

            open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=alex_person_id)
            submit_name_form(
                page,
                detail_url_pattern=alex_detail_pattern,
                display_name=alex_display_name,
            )
            submit_merge_from_home(
                page,
                base_url=base_url,
                person_ids=[blair_person_id, alex_person_id],
            )
            submit_exclusion_from_detail(
                page,
                base_url=base_url,
                person_id=alex_person_id,
                assignment_ids=old_blair_assignment_ids,
            )
            browser.close()
    finally:
        terminate_process(process)

    for face_observation_id in old_blair_face_ids:
        assert not any(
            row["active"] is True
            for row in read_face_assignment_rows(library_db, face_observation_id)
        )
        exclusion_rows = read_person_face_exclusions(
            library_db,
            face_observation_id=face_observation_id,
            excluded_person_id=alex_person_id,
        )
        assert len(exclusion_rows) == 1

    add_result = add_source(workspace, FIXTURE_DIR_2)
    assert add_result.returncode == 0
    rescan_result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert rescan_result.returncode == 0, rescan_result.stderr

    active_people_after_rescan = read_active_people(library_db)
    new_blair_person_candidates: list[str] = []
    for person_id, person in active_people_after_rescan.items():
        assignment_files = set(active_assignment_details_by_file_name(library_db, person_id=person_id))
        if old_blair_files | new_blair_files <= assignment_files:
            new_blair_person_candidates.append(person_id)
            assert person["display_name"] is None
    assert new_blair_person_candidates == [person_id for person_id in new_blair_person_candidates if person_id != alex_person_id]
    assert len(new_blair_person_candidates) == 1
    new_blair_person_id = new_blair_person_candidates[0]
    assert new_blair_person_id != alex_person_id

    alex_assignment_files_after_rescan = set(
        active_assignment_details_by_file_name(library_db, person_id=alex_person_id)
    )
    assert not (old_blair_files & alex_assignment_files_after_rescan)
    assert not (new_blair_files & alex_assignment_files_after_rescan)

    new_blair_assignment_by_file = active_assignment_details_by_file_name(
        library_db,
        person_id=new_blair_person_id,
    )
    assert old_blair_files | new_blair_files <= set(new_blair_assignment_by_file)
    recycled_old_blair_detail = next(
        item
        for item in new_blair_assignment_by_file.values()
        if str(item["file_name"]) in old_blair_files
    )
    initial_exclusion_rows = read_person_face_exclusions(
        library_db,
        face_observation_id=int(recycled_old_blair_detail["face_observation_id"]),
    )
    assert {str(item["excluded_person_id"]) for item in initial_exclusion_rows} == {alex_person_id}

    second_port = find_free_port()
    second_process = spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(second_port),
        "--person-detail-page-size",
        "50",
    )
    second_base_url = f"http://127.0.0.1:{second_port}"
    try:
        wait_for_http_ready(f"{second_base_url}/")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            submit_exclusion_from_detail(
                page,
                base_url=second_base_url,
                person_id=new_blair_person_id,
                assignment_ids=[int(recycled_old_blair_detail["assignment_id"])],
            )
            expect(
                page.locator(f"[data-assignment-id='{recycled_old_blair_detail['assignment_id']}']")
            ).to_have_count(0)
            browser.close()
    finally:
        terminate_process(second_process)

    accumulated_exclusion_rows = read_person_face_exclusions(
        library_db,
        face_observation_id=int(recycled_old_blair_detail["face_observation_id"]),
    )
    assert len(accumulated_exclusion_rows) == 2
    assert {str(item["excluded_person_id"]) for item in accumulated_exclusion_rows} == {
        alex_person_id,
        new_blair_person_id,
    }
    assert all(str(item["created_at"]) for item in accumulated_exclusion_rows)
