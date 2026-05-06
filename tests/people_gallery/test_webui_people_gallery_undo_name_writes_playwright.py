from __future__ import annotations

import re
from pathlib import Path

import httpx
from playwright.sync_api import Page
from playwright.sync_api import expect
from playwright.sync_api import sync_playwright

import pytest

from tests.conftest import copy_scanned_workspace
from tests.helpers import (
    find_free_port,
    spawn_hikbox,
    terminate_process,
    wait_for_http_ready,
)
from tests.people_gallery.people_gallery_helpers import (
    assert_undo_prg_flow,
    count_person_name_events,
    open_person_detail_from_home,
    read_active_assignment_ids,
    read_person_merge_operations,
    read_person_record,
    submit_merge_from_home,
    submit_name_form,
    submit_undo_from_home,
)


def test_people_gallery_undo_rejects_anonymous_winner_named_then_rejected(tmp_path: Path) -> None:
    """合并后对匿名 winner 命名，应使 undo 被拒绝。"""
    workspace, _, library_db, _, target_person_ids = copy_scanned_workspace(tmp_path / "anonymous-winner-named")
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]
    winner_person_id = min(alex_person_id, casey_person_id)
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
            winner_detail_pattern = re.compile(
                rf"{re.escape(base_url)}/people/{re.escape(winner_person_id)}(?:\\?.*)?$"
            )
            open_person_detail_from_home(
                page,
                base_url=base_url,
                entry_path="/people",
                person_id=winner_person_id,
            )
            submit_name_form(
                page,
                detail_url_pattern=winner_detail_pattern,
                display_name="Undo 首次命名",
            )
            page.goto(f"{base_url}/people", wait_until="networkidle")
            expect(page.locator("[data-undo-submit]")).to_be_disabled()
            browser.close()

        response = httpx.post(
            f"{base_url}/people/merge/undo",
            follow_redirects=False,
            timeout=5.0,
        )
        assert response.status_code == 400
        assert "合并之后已发生新的人物相关写入" in response.text
    finally:
        terminate_process(process)


def test_people_gallery_undo_rejects_named_winner_renamed_then_rejected(tmp_path: Path) -> None:
    """合并后对 named winner 再次改名，应使 undo 被拒绝。"""
    workspace, _, _, manifest, target_person_ids = copy_scanned_workspace(tmp_path / "named-winner-renamed")
    people_by_label = {str(person["label"]): person for person in manifest["people"]}
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]
    blair_person_id = target_person_ids["target_blair"]
    merged_anonymous_winner = min(alex_person_id, casey_person_id)
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
                display_name=blair_display_name,
            )
            submit_merge_from_home(
                page,
                base_url=base_url,
                person_ids=[merged_anonymous_winner, blair_person_id],
            )
            open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=blair_person_id)
            submit_name_form(
                page,
                detail_url_pattern=blair_detail_pattern,
                display_name="Undo 再次改名",
            )
            page.goto(f"{base_url}/people", wait_until="networkidle")
            expect(page.locator("[data-undo-submit]")).to_be_disabled()
            browser.close()

        response = httpx.post(
            f"{base_url}/people/merge/undo",
            follow_redirects=False,
            timeout=5.0,
        )
        assert response.status_code == 400
        assert "合并之后已发生新的人物相关写入" in response.text
    finally:
        terminate_process(process)


def test_people_gallery_undo_named_winner_noop_stays_eligible(tmp_path: Path) -> None:
    """合并后对 named winner 提交相同名称（noop），undo 仍可用。"""
    workspace, _, library_db, manifest, target_person_ids = copy_scanned_workspace(tmp_path / "named-winner-noop")
    people_by_label = {str(person["label"]): person for person in manifest["people"]}
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]
    blair_person_id = target_person_ids["target_blair"]
    merged_anonymous_winner = min(alex_person_id, casey_person_id)
    expected_blair_name = str(people_by_label["target_blair"]["display_name"])
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
                display_name=expected_blair_name,
            )
            submit_merge_from_home(
                page,
                base_url=base_url,
                person_ids=[merged_anonymous_winner, blair_person_id],
            )
            name_event_count_before_noop = count_person_name_events(library_db)
            open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=blair_person_id)
            submit_name_form(
                page,
                detail_url_pattern=blair_detail_pattern,
                display_name=expected_blair_name,
            )
            assert count_person_name_events(library_db) == name_event_count_before_noop
            page.goto(f"{base_url}/people", wait_until="networkidle")
            expect(page.locator("[data-undo-submit]")).to_be_enabled()
            response_start = len(response_log)
            submit_undo_from_home(page, base_url=base_url)
            assert_undo_prg_flow(
                responses=response_log[response_start:],
                base_url=base_url,
            )
            assert read_person_merge_operations(library_db)[-1]["undone_at"] is not None
            browser.close()
    finally:
        terminate_process(process)
