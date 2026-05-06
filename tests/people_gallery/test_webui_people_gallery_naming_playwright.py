from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page
from playwright.sync_api import expect
from playwright.sync_api import sync_playwright

from tests.helpers import (
    find_free_port,
    spawn_hikbox,
    terminate_process,
    wait_for_http_ready,
)
from tests.people_gallery.people_gallery_helpers import (
    assert_name_prg_flow,
    count_person_name_events,
    open_person_detail_from_home,
    people_section_person_ids,
    read_active_assignment_ids,
    read_name_slice_db_snapshot,
    read_person_name_events,
    read_person_record,
    submit_name_form,
)


def test_people_gallery_naming_via_real_serve_real_page_and_real_db(scanned_workspace, tmp_path: Path) -> None:
    workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
    people_by_label = {
        str(person["label"]): person for person in manifest["people"]
    }
    alex_person_id = target_person_ids["target_alex"]
    blair_person_id = target_person_ids["target_blair"]
    casey_person_id = target_person_ids["target_casey"]
    alex_manifest_name = str(people_by_label["target_alex"]["display_name"])
    blair_manifest_name = str(people_by_label["target_blair"]["display_name"])
    alex_temporary_name = "Temporary Alex"
    alex_temporary_input = "  Temporary Alex  "
    duplicate_input = f"  {alex_manifest_name}  "
    noop_spaced_input = f"  {alex_manifest_name}  "

    alex_initial_record = read_person_record(library_db, alex_person_id)
    blair_initial_record = read_person_record(library_db, blair_person_id)
    casey_initial_record = read_person_record(library_db, casey_person_id)
    assert alex_initial_record["display_name"] is None
    assert alex_initial_record["is_named"] is False
    assert blair_initial_record["display_name"] is None
    assert blair_initial_record["is_named"] is False
    assert casey_initial_record["display_name"] is None
    assert casey_initial_record["is_named"] is False

    alex_assignment_ids = read_active_assignment_ids(library_db, alex_person_id)
    blair_assignment_ids = read_active_assignment_ids(library_db, blair_person_id)
    casey_assignment_ids = read_active_assignment_ids(library_db, casey_person_id)
    assert alex_assignment_ids
    assert blair_assignment_ids
    assert casey_assignment_ids
    assert read_person_name_events(library_db, alex_person_id) == []
    assert count_person_name_events(library_db) == 0

    port = find_free_port()
    process = spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
        "--person-detail-page-size",
        "7",
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

            page.goto(f"{base_url}/people", wait_until="networkidle")
            assert alex_person_id in people_section_person_ids(page, section="anonymous")
            assert alex_person_id not in people_section_person_ids(page, section="named")

            alex_detail_pattern = re.compile(rf"{re.escape(base_url)}/people/{re.escape(alex_person_id)}(?:\\?.*)?$")
            open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=alex_person_id)
            expect(page.get_by_label("人物名称")).to_have_value("")

            response_start = len(response_log)
            submit_name_form(
                page,
                detail_url_pattern=alex_detail_pattern,
                display_name=alex_temporary_input,
            )
            assert_name_prg_flow(
                responses=response_log[response_start:],
                base_url=base_url,
                person_id=alex_person_id,
            )
            expect(page.get_by_role("status")).to_contain_text("名称已保存")
            expect(page.get_by_role("heading", name=alex_temporary_name)).to_be_visible()
            expect(page.get_by_label("人物名称")).to_have_value(alex_temporary_name)

            alex_after_named = read_person_record(library_db, alex_person_id)
            assert alex_after_named["id"] == alex_person_id
            assert alex_after_named["display_name"] == alex_temporary_name
            assert alex_after_named["is_named"] is True
            assert read_active_assignment_ids(library_db, alex_person_id) == alex_assignment_ids
            alex_events = read_person_name_events(library_db, alex_person_id)
            assert alex_events == [
                {
                    "id": alex_events[0]["id"],
                    "event_type": "person_named",
                    "old_display_name": None,
                    "new_display_name": alex_temporary_name,
                    "created_at": alex_events[0]["created_at"],
                }
            ]
            assert alex_events[0]["created_at"]

            page.goto(f"{base_url}/people", wait_until="networkidle")
            expect(
                page.locator(f"[data-people-section='named'] [data-person-id='{alex_person_id}'] [data-person-label]")
            ).to_have_text(alex_temporary_name)
            expect(page.locator(f"[data-people-section='anonymous'] [data-person-id='{alex_person_id}']")).to_have_count(0)

            open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=alex_person_id)
            response_start = len(response_log)
            submit_name_form(
                page,
                detail_url_pattern=alex_detail_pattern,
                display_name=alex_manifest_name,
            )
            assert_name_prg_flow(
                responses=response_log[response_start:],
                base_url=base_url,
                person_id=alex_person_id,
            )
            expect(page.get_by_role("status")).to_contain_text("名称已更新")
            expect(page.get_by_role("heading", name=alex_manifest_name)).to_be_visible()
            expect(page.get_by_label("人物名称")).to_have_value(alex_manifest_name)

            alex_after_renamed = read_person_record(library_db, alex_person_id)
            assert alex_after_renamed["id"] == alex_person_id
            assert alex_after_renamed["display_name"] == alex_manifest_name
            assert alex_after_renamed["is_named"] is True
            assert read_active_assignment_ids(library_db, alex_person_id) == alex_assignment_ids
            alex_events = read_person_name_events(library_db, alex_person_id)
            assert [event["event_type"] for event in alex_events] == ["person_named", "person_renamed"]
            assert alex_events[1]["old_display_name"] == alex_temporary_name
            assert alex_events[1]["new_display_name"] == alex_manifest_name
            assert alex_events[1]["created_at"]

            page.goto(f"{base_url}/people", wait_until="networkidle")
            expect(
                page.locator(f"[data-people-section='named'] [data-person-id='{alex_person_id}'] [data-person-label]")
            ).to_have_text(alex_manifest_name)
            expect(page.locator(f"[data-people-section='anonymous'] [data-person-id='{alex_person_id}']")).to_have_count(0)

            alex_before_duplicate_attempt = read_person_record(library_db, alex_person_id)
            alex_assignments_before_duplicate_attempt = read_active_assignment_ids(library_db, alex_person_id)
            open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=blair_person_id)
            response_start = len(response_log)
            page.get_by_label("人物名称").fill(duplicate_input)
            page.get_by_role("button", name="保存名称").click()
            page.wait_for_load_state("networkidle")
            assert not any(
                response["method"] == "POST"
                and response["url"] == f"{base_url}/people/{blair_person_id}/name"
                and int(response["status"]) in {302, 303}
                for response in response_log[response_start:]
            )
            expect(page.get_by_role("alert")).to_contain_text("名称已存在")
            assert read_person_record(library_db, blair_person_id) == blair_initial_record
            assert read_active_assignment_ids(library_db, blair_person_id) == blair_assignment_ids
            assert read_person_record(library_db, alex_person_id) == alex_before_duplicate_attempt
            assert read_active_assignment_ids(library_db, alex_person_id) == alex_assignments_before_duplicate_attempt
            assert read_person_name_events(library_db, blair_person_id) == []
            assert count_person_name_events(library_db) == 2

            open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=casey_person_id)
            response_start = len(response_log)
            page.get_by_label("人物名称").fill("   ")
            page.get_by_role("button", name="保存名称").click()
            page.wait_for_load_state("networkidle")
            assert not any(
                response["method"] == "POST"
                and response["url"] == f"{base_url}/people/{casey_person_id}/name"
                and int(response["status"]) in {302, 303}
                for response in response_log[response_start:]
            )
            expect(page.get_by_role("alert")).to_contain_text("名称不能为空")
            assert read_person_record(library_db, casey_person_id) == casey_initial_record
            assert read_active_assignment_ids(library_db, casey_person_id) == casey_assignment_ids
            assert read_person_name_events(library_db, casey_person_id) == []
            assert count_person_name_events(library_db) == 2

            open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=alex_person_id)
            alex_before_noop = read_person_record(library_db, alex_person_id)
            response_start = len(response_log)
            submit_name_form(
                page,
                detail_url_pattern=alex_detail_pattern,
                display_name=alex_manifest_name,
            )
            assert_name_prg_flow(
                responses=response_log[response_start:],
                base_url=base_url,
                person_id=alex_person_id,
            )
            expect(page.get_by_role("status")).to_contain_text("名称未变化")
            assert read_person_record(library_db, alex_person_id) == alex_before_noop
            assert read_person_name_events(library_db, alex_person_id) == alex_events

            response_start = len(response_log)
            submit_name_form(
                page,
                detail_url_pattern=alex_detail_pattern,
                display_name=noop_spaced_input,
            )
            assert_name_prg_flow(
                responses=response_log[response_start:],
                base_url=base_url,
                person_id=alex_person_id,
            )
            expect(page.get_by_role("status")).to_contain_text("名称未变化")
            assert read_person_record(library_db, alex_person_id) == alex_before_noop
            assert read_person_name_events(library_db, alex_person_id) == alex_events
            assert count_person_name_events(library_db) == 2

            page.goto(f"{base_url}/people", wait_until="networkidle")
            expect(
                page.locator(f"[data-people-section='named'] [data-person-id='{alex_person_id}'] [data-person-label]")
            ).to_have_text(alex_manifest_name)
            expect(page.locator(f"[data-people-section='anonymous'] [data-person-id='{alex_person_id}']")).to_have_count(0)
            expect(page.locator(f"[data-people-section='anonymous'] [data-person-id='{blair_person_id}']")).to_have_count(1)
            expect(page.locator(f"[data-people-section='anonymous'] [data-person-id='{casey_person_id}']")).to_have_count(1)

            missing_person_id = "00000000-0000-0000-0000-000000000000"
            db_snapshot_before_missing = read_name_slice_db_snapshot(library_db)
            audit_count_before_missing = count_person_name_events(library_db)
            missing_response = httpx.post(
                f"{base_url}/people/{missing_person_id}/name",
                data={"display_name": "Nobody"},
                follow_redirects=False,
                timeout=5.0,
            )
            assert missing_response.status_code == 404
            assert "未找到" in missing_response.text or "人物不存在" in missing_response.text
            assert read_name_slice_db_snapshot(library_db) == db_snapshot_before_missing
            assert count_person_name_events(library_db) == audit_count_before_missing

            browser.close()
    finally:
        terminate_process(process)
