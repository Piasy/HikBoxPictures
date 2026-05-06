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
    find_free_port,
    run_hikbox,
    spawn_hikbox,
    terminate_process,
    wait_for_http_ready,
)
from tests.people_gallery.people_gallery_helpers import (
    FIXTURE_DIR_2,
    assert_merge_prg_flow,
    asset_assignment_rows,
    extract_merge_request_person_ids,
    iter_rendered_assignment_cards,
    load_incremental_manifest,
    manifest_files_for_target,
    page_card_snapshot,
    people_section_person_ids_in_rendered_order,
    read_active_assignment_ids,
    read_active_people,
    read_merge_operation_assignment_rows,
    read_person_merge_operations,
    read_person_record,
    submit_merge_from_home,
)


def test_people_gallery_merge_via_real_serve_real_page_and_real_db(scanned_workspace, tmp_path: Path) -> None:
    workspace, _, library_db, manifest, target_person_ids = scanned_workspace
    incremental_manifest = load_incremental_manifest()
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]
    blair_person_id = target_person_ids["target_blair"]
    winner_person_id = min(alex_person_id, casey_person_id)
    loser_person_id = casey_person_id if winner_person_id == alex_person_id else alex_person_id

    alex_assignment_ids_before_merge = read_active_assignment_ids(library_db, alex_person_id)
    casey_assignment_ids_before_merge = read_active_assignment_ids(library_db, casey_person_id)
    blair_assignment_ids_before_merge = read_active_assignment_ids(library_db, blair_person_id)
    winner_assignment_ids_before_merge = read_active_assignment_ids(library_db, winner_person_id)
    loser_assignment_ids_before_merge = read_active_assignment_ids(library_db, loser_person_id)
    expected_union_assignment_ids = set(alex_assignment_ids_before_merge) | set(casey_assignment_ids_before_merge)
    assert len(alex_assignment_ids_before_merge) == len(casey_assignment_ids_before_merge)
    assert set(winner_assignment_ids_before_merge) | set(loser_assignment_ids_before_merge) == expected_union_assignment_ids
    active_people_before_merge = set(read_active_people(library_db))

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
            request_log: list[dict[str, object]] = []

            def _record_response(response: object) -> None:
                request = response.request
                response_log.append(
                    {
                        "method": str(request.method),
                        "url": str(response.url),
                        "status": int(response.status),
                    }
                )

            def _record_request(request: object) -> None:
                request_log.append(
                    {
                        "method": str(request.method),
                        "url": str(request.url),
                        "post_data": request.post_data,
                    }
                )

            page.on("response", _record_response)
            page.on("request", _record_request)

            page.goto(f"{base_url}/people", wait_until="networkidle")
            anonymous_order_before_merge = people_section_person_ids_in_rendered_order(page, section="anonymous")
            assert alex_person_id in anonymous_order_before_merge
            assert casey_person_id in anonymous_order_before_merge

            response_start = len(response_log)
            request_start = len(request_log)
            submit_merge_from_home(
                page,
                base_url=base_url,
                person_ids=[casey_person_id, alex_person_id],
            )
            assert_merge_prg_flow(
                responses=response_log[response_start:],
                base_url=base_url,
            )
            posted_person_ids = extract_merge_request_person_ids(
                requests=request_log[request_start:],
                base_url=base_url,
            )
            assert set(posted_person_ids) == {alex_person_id, casey_person_id}
            expect(page.get_by_role("status")).to_contain_text("人物已合并")

            home_cards_after_merge = page_card_snapshot(page)
            assert winner_person_id in home_cards_after_merge
            assert loser_person_id not in home_cards_after_merge
            assert set(home_cards_after_merge) == active_people_before_merge - {loser_person_id}
            assert str(len(expected_union_assignment_ids)) in str(home_cards_after_merge[winner_person_id]["sample_count_text"])

            winner_record_after_merge = read_person_record(library_db, winner_person_id)
            loser_record_after_merge = read_person_record(library_db, loser_person_id)
            assert winner_record_after_merge["status"] == "active"
            assert loser_record_after_merge["status"] == "inactive"
            assert loser_record_after_merge["display_name"] is None
            assert loser_record_after_merge["is_named"] is False
            assert set(read_active_assignment_ids(library_db, winner_person_id)) == expected_union_assignment_ids
            assert read_active_assignment_ids(library_db, loser_person_id) == []

            page.goto(f"{base_url}/people/{winner_person_id}", wait_until="networkidle")
            rendered_assignment_ids = {
                assignment_id for assignment_id, _, _ in iter_rendered_assignment_cards(page)
            }
            assert rendered_assignment_ids == expected_union_assignment_ids

            loser_detail_response = httpx.get(f"{base_url}/people/{loser_person_id}", timeout=5.0)
            assert loser_detail_response.status_code == 404
            assert "人物不存在" in loser_detail_response.text

            merge_operations = read_person_merge_operations(library_db)
            assert len(merge_operations) == 1
            assert merge_operations[0]["winner_person_id"] == winner_person_id
            assert merge_operations[0]["loser_person_id"] == loser_person_id
            merge_assignment_rows = read_merge_operation_assignment_rows(
                library_db,
                merge_operation_id=int(merge_operations[0]["id"]),
            )
            assert {
                int(row["assignment_id"])
                for row in merge_assignment_rows
                if row["person_role"] == "winner"
            } == set(winner_assignment_ids_before_merge)
            assert {
                int(row["assignment_id"])
                for row in merge_assignment_rows
                if row["person_role"] == "loser"
            } == set(loser_assignment_ids_before_merge)

            browser.close()
    finally:
        terminate_process(process)

    add_second_source_result = add_source(workspace, FIXTURE_DIR_2)
    assert add_second_source_result.returncode == 0, add_second_source_result.stderr
    incremental_scan_result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert incremental_scan_result.returncode == 0, incremental_scan_result.stderr

    assignment_rows_after_incremental = asset_assignment_rows(library_db)
    for file_name in manifest_files_for_target(incremental_manifest, "target_alex"):
        assert {person_id for _, person_id, _ in assignment_rows_after_incremental[file_name]} == {winner_person_id}
    for file_name in manifest_files_for_target(incremental_manifest, "target_casey"):
        assert {person_id for _, person_id, _ in assignment_rows_after_incremental[file_name]} == {winner_person_id}
    for file_name in manifest_files_for_target(incremental_manifest, "target_blair"):
        assert {person_id for _, person_id, _ in assignment_rows_after_incremental[file_name]} == {blair_person_id}

    expected_winner_count_after_incremental = len(expected_union_assignment_ids) + 10
    expected_blair_count_after_incremental = len(blair_assignment_ids_before_merge) + 5
    active_people_after_incremental = read_active_people(library_db)
    assert set(active_people_after_incremental) == active_people_before_merge - {loser_person_id}
    assert int(active_people_after_incremental[winner_person_id]["sample_count"]) == expected_winner_count_after_incremental
    assert int(active_people_after_incremental[blair_person_id]["sample_count"]) == expected_blair_count_after_incremental
    assert read_person_record(library_db, loser_person_id)["status"] == "inactive"

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
            page.goto(f"{base_url}/people", wait_until="networkidle")
            home_cards_after_incremental = page_card_snapshot(page)
            assert loser_person_id not in home_cards_after_incremental
            assert str(expected_winner_count_after_incremental) in str(
                home_cards_after_incremental[winner_person_id]["sample_count_text"]
            )
            assert str(expected_blair_count_after_incremental) in str(
                home_cards_after_incremental[blair_person_id]["sample_count_text"]
            )
            browser.close()
    finally:
        terminate_process(process)
