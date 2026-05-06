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
    open_person_detail_from_home,
    read_active_assignment_ids,
    read_name_slice_db_snapshot,
    read_person_merge_operations,
    read_person_record,
    submit_merge_from_home,
    submit_name_form,
    submit_undo_from_home,
)


def test_people_gallery_undo_remains_available_after_third_person_rename(scanned_workspace, tmp_path: Path) -> None:
    workspace, _, library_db, manifest, target_person_ids = scanned_workspace
    people_by_label = {str(person["label"]): person for person in manifest["people"]}
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]
    blair_person_id = target_person_ids["target_blair"]
    winner_person_id = min(alex_person_id, casey_person_id)
    loser_person_id = casey_person_id if winner_person_id == alex_person_id else alex_person_id
    winner_record_before_merge = read_person_record(library_db, winner_person_id)
    loser_record_before_merge = read_person_record(library_db, loser_person_id)
    renamed_blair = f"{people_by_label['target_blair']['display_name']} Undo 保留"

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

            blair_detail_pattern = re.compile(
                rf"{re.escape(base_url)}/people/{re.escape(blair_person_id)}(?:\\?.*)?$"
            )
            open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=blair_person_id)
            submit_name_form(
                page,
                detail_url_pattern=blair_detail_pattern,
                display_name=renamed_blair,
            )

            page.goto(f"{base_url}/people", wait_until="networkidle")
            expect(page.locator("[data-undo-submit]")).to_be_enabled()
            response_start = len(response_log)
            submit_undo_from_home(page, base_url=base_url)
            assert_undo_prg_flow(
                responses=response_log[response_start:],
                base_url=base_url,
            )

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
            assert read_person_record(library_db, blair_person_id)["display_name"] == renamed_blair
            browser.close()
    finally:
        terminate_process(process)


def test_people_gallery_undo_only_rolls_back_latest_merge(scanned_workspace, tmp_path: Path) -> None:
    workspace, _, library_db, _, target_person_ids = scanned_workspace
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]
    blair_person_id = target_person_ids["target_blair"]
    first_merge_winner_person_id = min(alex_person_id, casey_person_id)
    first_merge_loser_person_id = casey_person_id if first_merge_winner_person_id == alex_person_id else alex_person_id

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
            submit_merge_from_home(
                page,
                base_url=base_url,
                person_ids=[casey_person_id, alex_person_id],
            )
            first_merge_assignment_ids = read_active_assignment_ids(library_db, first_merge_winner_person_id)
            blair_assignment_ids_before_second_merge = read_active_assignment_ids(library_db, blair_person_id)
            expected_first_merge_assignment_ids = set(first_merge_assignment_ids)
            expected_second_merge_union = expected_first_merge_assignment_ids | set(blair_assignment_ids_before_second_merge)

            submit_merge_from_home(
                page,
                base_url=base_url,
                person_ids=[blair_person_id, first_merge_winner_person_id],
            )
            submit_undo_from_home(page, base_url=base_url)
            expect(page.locator("[data-undo-submit]")).to_be_disabled()

            assert set(read_active_assignment_ids(library_db, first_merge_winner_person_id)) == expected_first_merge_assignment_ids
            assert set(read_active_assignment_ids(library_db, blair_person_id)) == set(blair_assignment_ids_before_second_merge)
            assert read_active_assignment_ids(library_db, first_merge_loser_person_id) == []
            assert set(read_active_assignment_ids(library_db, first_merge_winner_person_id)) != expected_second_merge_union
            merge_operations = read_person_merge_operations(library_db)
            assert len(merge_operations) == 2
            assert merge_operations[0]["undone_at"] is None
            assert merge_operations[1]["undone_at"] is not None
            browser.close()

        snapshot_after_latest_undo = read_name_slice_db_snapshot(library_db)
        response = httpx.post(
            f"{base_url}/people/merge/undo",
            follow_redirects=False,
            timeout=5.0,
        )
        assert response.status_code == 400
        assert "最近一次成功合并已经撤销" in response.text
        assert read_name_slice_db_snapshot(library_db) == snapshot_after_latest_undo
    finally:
        terminate_process(process)
