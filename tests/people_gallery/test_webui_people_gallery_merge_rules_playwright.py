from __future__ import annotations

import re
from pathlib import Path

import httpx
from playwright.sync_api import Page
from playwright.sync_api import expect
from playwright.sync_api import sync_playwright

import pytest

from tests.helpers import (
    find_free_port,
    spawn_hikbox,
    terminate_process,
    wait_for_http_ready,
)
from tests.people_gallery.people_gallery_helpers import (
    assert_merge_prg_flow,
    assert_name_prg_flow,
    extract_merge_request_person_ids,
    open_person_detail_from_home,
    page_card_snapshot,
    read_active_assignment_ids,
    read_active_people,
    read_merge_operation_assignment_rows,
    read_person_merge_operations,
    read_person_record,
    submit_merge_from_home,
    submit_name_form,
)


def test_people_gallery_merge_prefers_sample_count_over_request_order(scanned_workspace, tmp_path: Path) -> None:
    workspace, _, library_db, manifest, target_person_ids = scanned_workspace
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]
    blair_person_id = target_person_ids["target_blair"]
    first_merge_winner_person_id = min(alex_person_id, casey_person_id)

    port = find_free_port()
    process = spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
        "--person-detail-page-size",
        "100",
    )
    base_url = f"http://127.0.0.1:{port}"

    try:
        wait_for_http_ready(f"{base_url}/")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            request_log: list[dict[str, object]] = []
            response_log: list[dict[str, object]] = []

            def _record_request(request: object) -> None:
                request_log.append(
                    {
                        "method": str(request.method),
                        "url": str(request.url),
                        "post_data": request.post_data,
                    }
                )

            def _record_response(response: object) -> None:
                request = response.request
                response_log.append(
                    {
                        "method": str(request.method),
                        "url": str(response.url),
                        "status": int(response.status),
                    }
                )

            page.on("request", _record_request)
            page.on("response", _record_response)

            submit_merge_from_home(
                page,
                base_url=base_url,
                person_ids=[casey_person_id, alex_person_id],
            )
            merged_winner_assignment_ids = read_active_assignment_ids(library_db, first_merge_winner_person_id)
            blair_assignment_ids_before_second_merge = read_active_assignment_ids(library_db, blair_person_id)
            expected_assignment_ids_after_second_merge = set(merged_winner_assignment_ids) | set(
                blair_assignment_ids_before_second_merge
            )

            response_start = len(response_log)
            request_start = len(request_log)
            submit_merge_from_home(
                page,
                base_url=base_url,
                person_ids=[blair_person_id, first_merge_winner_person_id],
            )
            assert_merge_prg_flow(
                responses=response_log[response_start:],
                base_url=base_url,
            )
            posted_person_ids = extract_merge_request_person_ids(
                requests=request_log[request_start:],
                base_url=base_url,
            )
            assert posted_person_ids == [blair_person_id, first_merge_winner_person_id]
            expect(page.get_by_role("status")).to_contain_text("人物已合并")

            home_cards_after_second_merge = page_card_snapshot(page)
            assert first_merge_winner_person_id in home_cards_after_second_merge
            assert blair_person_id not in home_cards_after_second_merge
            assert str(len(expected_assignment_ids_after_second_merge)) in str(
                home_cards_after_second_merge[first_merge_winner_person_id]["sample_count_text"]
            )
            assert read_person_record(library_db, first_merge_winner_person_id)["status"] == "active"
            assert read_person_record(library_db, blair_person_id)["status"] == "inactive"
            assert set(read_active_assignment_ids(library_db, first_merge_winner_person_id)) == (
                expected_assignment_ids_after_second_merge
            )
            assert read_active_assignment_ids(library_db, blair_person_id) == []

            merge_operations = read_person_merge_operations(library_db)
            assert len(merge_operations) == 2
            assert merge_operations[-1]["winner_person_id"] == first_merge_winner_person_id
            assert merge_operations[-1]["loser_person_id"] == blair_person_id

            loser_detail_response = httpx.get(f"{base_url}/people/{blair_person_id}", timeout=5.0)
            assert loser_detail_response.status_code == 404
            assert "人物不存在" in loser_detail_response.text
            browser.close()
    finally:
        terminate_process(process)


def test_people_gallery_merge_prefers_named_person_over_anonymous_even_with_fewer_samples(scanned_workspace, tmp_path: Path) -> None:
    workspace, _, library_db, manifest, target_person_ids = scanned_workspace
    people_by_label = {str(person["label"]): person for person in manifest["people"]}
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]
    blair_person_id = target_person_ids["target_blair"]
    merged_anonymous_winner_person_id = min(alex_person_id, casey_person_id)
    blair_display_name = str(people_by_label["target_blair"]["display_name"])

    port = find_free_port()
    process = spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
        "--person-detail-page-size",
        "100",
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

            submit_merge_from_home(
                page,
                base_url=base_url,
                person_ids=[casey_person_id, alex_person_id],
            )
            merged_anonymous_assignment_ids = read_active_assignment_ids(library_db, merged_anonymous_winner_person_id)
            blair_assignment_ids_before_merge = read_active_assignment_ids(library_db, blair_person_id)

            blair_detail_pattern = re.compile(
                rf"{re.escape(base_url)}/people/{re.escape(blair_person_id)}(?:\\?.*)?$"
            )
            open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=blair_person_id)
            response_start = len(response_log)
            submit_name_form(
                page,
                detail_url_pattern=blair_detail_pattern,
                display_name=blair_display_name,
            )
            assert_name_prg_flow(
                responses=response_log[response_start:],
                base_url=base_url,
                person_id=blair_person_id,
            )

            page.goto(f"{base_url}/people", wait_until="networkidle")
            response_start = len(response_log)
            submit_merge_from_home(
                page,
                base_url=base_url,
                person_ids=[merged_anonymous_winner_person_id, blair_person_id],
            )
            assert_merge_prg_flow(
                responses=response_log[response_start:],
                base_url=base_url,
            )
            expect(page.get_by_role("status")).to_contain_text("人物已合并")

            winner_record = read_person_record(library_db, blair_person_id)
            loser_record = read_person_record(library_db, merged_anonymous_winner_person_id)
            assert winner_record["status"] == "active"
            assert winner_record["is_named"] is True
            assert winner_record["display_name"] == blair_display_name
            assert loser_record["status"] == "inactive"
            assert set(read_active_assignment_ids(library_db, blair_person_id)) == (
                set(blair_assignment_ids_before_merge) | set(merged_anonymous_assignment_ids)
            )
            assert read_active_assignment_ids(library_db, merged_anonymous_winner_person_id) == []

            merge_operations = read_person_merge_operations(library_db)
            assert len(merge_operations) == 2
            assert merge_operations[-1]["winner_person_id"] == blair_person_id
            assert merge_operations[-1]["loser_person_id"] == merged_anonymous_winner_person_id
            assert merge_operations[-1]["winner_display_name_before"] == blair_display_name
            assert merge_operations[-1]["winner_is_named_before"] is True
            assert merge_operations[-1]["winner_status_before"] == "active"
            assert merge_operations[-1]["loser_display_name_before"] is None
            assert merge_operations[-1]["loser_is_named_before"] is False
            assert merge_operations[-1]["loser_status_before"] == "active"

            page.goto(f"{base_url}/people", wait_until="networkidle")
            expect(
                page.locator(f"[data-people-section='named'] [data-person-id='{blair_person_id}'] [data-person-label]")
            ).to_have_text(blair_display_name)
            expect(
                page.locator(
                    f"[data-people-section='anonymous'] [data-person-id='{merged_anonymous_winner_person_id}']"
                )
            ).to_have_count(0)

            page.goto(f"{base_url}/people/{blair_person_id}", wait_until="networkidle")
            expect(page.get_by_test_id("person-detail")).to_be_visible()
            expect(page.locator("h1")).to_have_text(blair_display_name)
            expect(page.locator("[data-testid='person-detail'] .sample-card")).to_have_count(
                len(set(blair_assignment_ids_before_merge) | set(merged_anonymous_assignment_ids))
            )

            loser_detail_response = httpx.get(
                f"{base_url}/people/{merged_anonymous_winner_person_id}",
                timeout=5.0,
            )
            assert loser_detail_response.status_code == 404
            assert "人物不存在" in loser_detail_response.text
            browser.close()
    finally:
        terminate_process(process)


def test_people_gallery_merge_two_named_people_succeeds_via_real_home(scanned_workspace, tmp_path: Path) -> None:
    workspace, _, library_db, manifest, target_person_ids = scanned_workspace
    people_by_label = {str(person["label"]): person for person in manifest["people"]}
    alex_person_id = target_person_ids["target_alex"]
    blair_person_id = target_person_ids["target_blair"]
    alex_display_name = str(people_by_label["target_alex"]["display_name"])
    blair_display_name = str(people_by_label["target_blair"]["display_name"])

    port = find_free_port()
    process = spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
        "--person-detail-page-size",
        "100",
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

            alex_detail_pattern = re.compile(
                rf"{re.escape(base_url)}/people/{re.escape(alex_person_id)}(?:\\?.*)?$"
            )
            blair_detail_pattern = re.compile(
                rf"{re.escape(base_url)}/people/{re.escape(blair_person_id)}(?:\\?.*)?$"
            )
            open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=alex_person_id)
            submit_name_form(
                page,
                detail_url_pattern=alex_detail_pattern,
                display_name=alex_display_name,
            )
            open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=blair_person_id)
            submit_name_form(
                page,
                detail_url_pattern=blair_detail_pattern,
                display_name=blair_display_name,
            )

            page.goto(f"{base_url}/people", wait_until="networkidle")
            response_start = len(response_log)
            submit_merge_from_home(
                page,
                base_url=base_url,
                person_ids=[alex_person_id, blair_person_id],
            )
            assert_merge_prg_flow(
                responses=response_log[response_start:],
                base_url=base_url,
            )
            expect(page.locator("[data-undo-submit]")).to_be_enabled()
            assert len(read_person_merge_operations(library_db)) == 1
            active_people_after = read_active_people(library_db)
            assert len(active_people_after) == 2
            browser.close()
    finally:
        terminate_process(process)
