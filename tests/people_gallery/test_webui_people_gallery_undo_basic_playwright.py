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
    assert_undo_prg_flow,
    page_card_snapshot,
    read_active_assignment_details,
    read_active_people,
    read_assignment_owner_snapshot,
    read_name_slice_db_snapshot,
    read_person_merge_operations,
    read_person_record,
    submit_merge_from_home,
    submit_undo_from_home,
)


def test_people_gallery_undo_restores_latest_merge_via_real_home_and_db(scanned_workspace, tmp_path: Path) -> None:
    workspace, _, library_db, _, target_person_ids = scanned_workspace
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]
    winner_person_id = min(alex_person_id, casey_person_id)
    loser_person_id = casey_person_id if winner_person_id == alex_person_id else alex_person_id
    people_before_merge = read_active_people(library_db)
    winner_record_before_merge = read_person_record(library_db, winner_person_id)
    loser_record_before_merge = read_person_record(library_db, loser_person_id)
    winner_detail_before_merge = read_active_assignment_details(library_db, winner_person_id)
    loser_detail_before_merge = read_active_assignment_details(library_db, loser_person_id)
    assignment_owner_snapshot_before_merge = read_assignment_owner_snapshot(library_db)

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

            page.goto(f"{base_url}/people", wait_until="networkidle")
            expect(page.locator("[data-undo-submit]")).to_be_disabled()

            submit_merge_from_home(
                page,
                base_url=base_url,
                person_ids=[casey_person_id, alex_person_id],
            )
            expect(page.locator("[data-undo-submit]")).to_be_enabled()

            response_start = len(response_log)
            submit_undo_from_home(page, base_url=base_url)
            assert_undo_prg_flow(
                responses=response_log[response_start:],
                base_url=base_url,
            )
            expect(page.get_by_role("status")).to_contain_text("最近一次合并已撤销")
            expect(page.locator("[data-undo-submit]")).to_be_disabled()

            assert page_card_snapshot(page).keys() == people_before_merge.keys()
            winner_record_after_undo = read_person_record(library_db, winner_person_id)
            loser_record_after_undo = read_person_record(library_db, loser_person_id)
            assert winner_record_after_undo["id"] == winner_record_before_merge["id"]
            assert winner_record_after_undo["display_name"] == winner_record_before_merge["display_name"]
            assert winner_record_after_undo["is_named"] == winner_record_before_merge["is_named"]
            assert winner_record_after_undo["status"] == winner_record_before_merge["status"]
            assert loser_record_after_undo["id"] == loser_record_before_merge["id"]
            assert loser_record_after_undo["display_name"] == loser_record_before_merge["display_name"]
            assert loser_record_after_undo["is_named"] == loser_record_before_merge["is_named"]
            assert loser_record_after_undo["status"] == loser_record_before_merge["status"]
            assert read_active_assignment_details(library_db, winner_person_id) == winner_detail_before_merge
            assert read_active_assignment_details(library_db, loser_person_id) == loser_detail_before_merge
            assert read_assignment_owner_snapshot(library_db) == assignment_owner_snapshot_before_merge
            merge_operations = read_person_merge_operations(library_db)
            assert len(merge_operations) == 1
            assert merge_operations[0]["undone_at"] is not None
            browser.close()
    finally:
        terminate_process(process)


def test_people_gallery_undo_is_disabled_without_merge_and_after_already_undone_merge(scanned_workspace, tmp_path: Path) -> None:
    workspace, _, library_db, _, target_person_ids = scanned_workspace
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]

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
        page_snapshot_before_merge = read_name_slice_db_snapshot(library_db)
        no_merge_response = httpx.post(
            f"{base_url}/people/merge/undo",
            follow_redirects=False,
            timeout=5.0,
        )
        assert no_merge_response.status_code == 400
        assert "当前没有可撤销的最近一次合并" in no_merge_response.text
        assert read_name_slice_db_snapshot(library_db) == page_snapshot_before_merge

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(f"{base_url}/people", wait_until="networkidle")
            expect(page.locator("[data-undo-submit]")).to_be_disabled()

            submit_merge_from_home(
                page,
                base_url=base_url,
                person_ids=[casey_person_id, alex_person_id],
            )
            submit_undo_from_home(page, base_url=base_url)
            expect(page.locator("[data-undo-submit]")).to_be_disabled()
            browser.close()

        snapshot_after_undo = read_name_slice_db_snapshot(library_db)
        already_undone_response = httpx.post(
            f"{base_url}/people/merge/undo",
            follow_redirects=False,
            timeout=5.0,
        )
        assert already_undone_response.status_code == 400
        assert "最近一次成功合并已经撤销" in already_undone_response.text
        assert read_name_slice_db_snapshot(library_db) == snapshot_after_undo
    finally:
        terminate_process(process)
