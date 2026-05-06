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
    read_name_slice_db_snapshot,
    read_person_merge_operations,
    read_person_record,
    submit_merge_from_home,
)

FIXTURE_DIR_2 = FIXTURE_DIR.with_name("people_gallery_scan_2")


def test_people_gallery_undo_rejects_after_incremental_assignment_write(scanned_workspace, tmp_path: Path) -> None:
    """合并后执行增量扫描，新的 assignment 写入应使 undo 被拒绝。"""
    workspace, _, library_db, _, target_person_ids = scanned_workspace
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]
    winner_person_id = min(alex_person_id, casey_person_id)
    merged_snapshot = read_name_slice_db_snapshot(library_db)

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
            browser.close()
    finally:
        terminate_process(process)

    add_result = add_source(workspace, FIXTURE_DIR_2)
    assert add_result.returncode == 0
    scan_result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert scan_result.returncode == 0, scan_result.stderr

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
            expect(page.locator("[data-undo-submit]")).to_be_disabled()
            browser.close()

        response = httpx.post(
            f"{base_url}/people/merge/undo",
            follow_redirects=False,
            timeout=5.0,
        )
        assert response.status_code == 400
        assert "合并之后已发生新的人物相关写入" in response.text
        assert read_person_record(library_db, winner_person_id)["status"] == "active"
        assert read_person_merge_operations(library_db)[0]["undone_at"] is None
        assert read_name_slice_db_snapshot(library_db) != merged_snapshot
    finally:
        terminate_process(process)
