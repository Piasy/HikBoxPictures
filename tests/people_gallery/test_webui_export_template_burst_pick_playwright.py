from __future__ import annotations

from pathlib import Path
import time

import httpx
from playwright.sync_api import expect
from playwright.sync_api import sync_playwright

from tests.helpers import (
    fetch_all,
    find_free_port,
    name_person_via_api,
    spawn_hikbox,
    terminate_process,
    wait_for_http_ready,
)


def _await_burst_pick_completed(base_url: str, template_id: str) -> dict[str, object]:
    deadline = time.time() + 30
    last_data: dict[str, object] | None = None
    while time.time() < deadline:
        response = httpx.get(
            f"{base_url}/api/export-templates/{template_id}/burst-pick",
            timeout=5.0,
        )
        assert response.status_code == 200, response.text
        data = response.json()
        last_data = data
        if data["status"] == "completed":
            return data
        time.sleep(0.1)
    raise AssertionError(f"等待连拍挑选完成超时: {last_data}")


class TestExportTemplateBurstPickWebUI:
    def test_list_entry_form_validation_and_successful_submit(
        self,
        scanned_workspace,
        tmp_path: Path,
    ) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = find_free_port()
        process = spawn_hikbox(
            "serve",
            "--workspace",
            str(workspace),
            "--port",
            str(port),
            env_updates={"HIKBOX_TEST_BURST_PICK_FINGERPRINT_DELAY_SECONDS": "0.05"},
        )
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            wait_for_http_ready(f"{base_url}/")
            assert name_person_via_api(base_url, alex_id, "Alex Chen").status_code in (302, 303)
            assert name_person_via_api(base_url, blair_id, "Blair Lin").status_code in (302, 303)

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 900})

                page.goto(f"{base_url}/exports/new")
                page.fill("input#name", "Alex & Blair")
                page.fill("input#output_root", output_root)
                page.locator(f"article[data-person-id='{alex_id}'] input[type=checkbox]").check()
                page.locator(f"article[data-person-id='{blair_id}'] input[type=checkbox]").check()
                page.locator("button[type=submit]").click()
                expect(page).to_have_url(f"{base_url}/exports")

                row = page.locator("tr[data-template-id]").first
                template_id = row.get_attribute("data-template-id")
                burst_link = row.locator("a[data-template-burst-pick-link]")
                expect(burst_link).to_contain_text("连拍挑选")
                assert burst_link.get_attribute("href") == f"/exports/{template_id}/burst-pick"

                burst_link.click()
                expect(page).to_have_url(f"{base_url}/exports/{template_id}/burst-pick")
                expect(page.locator("[data-burst-pick-status='running']")).to_be_visible()

                burst_data = _await_burst_pick_completed(base_url, str(template_id))
                page.reload()
                expect(page.locator("form[data-burst-pick-form]").first).to_be_visible()

                group_cards = page.locator("[data-burst-group-key]")
                expect(group_cards).not_to_have_count(0)
                hidden_group_keys = page.locator("input[type=hidden][name=group_key]")
                expect(hidden_group_keys).to_have_count(group_cards.count())
                assert hidden_group_keys.count() == len(burst_data["groups"])

                first_form = page.locator("form[data-burst-pick-form]").first
                submitted_group_key = first_form.locator("input[type=hidden][name=group_key]").input_value()
                first_image_src = first_form.locator("img[data-burst-slide-image]").first.get_attribute("src")
                assert first_image_src is not None
                assert "/images/assets/" in first_image_src
                assert first_image_src.endswith("/original")
                assert "/images/assignments/" not in first_image_src

                first_slides = first_form.locator("[data-burst-slide]")
                if first_slides.count() > 1:
                    expect(first_slides.nth(0)).to_be_visible()
                    first_form.locator("[data-burst-next]").click()
                    expect(first_slides.nth(1)).to_be_visible()
                    first_form.locator("[data-burst-prev]").click()
                    expect(first_slides.nth(0)).to_be_visible()

                keep_inputs = page.locator("input[type=checkbox][name^='keep_asset_id__']")
                expect(keep_inputs).not_to_have_count(0)
                for index in range(keep_inputs.count()):
                    assert not keep_inputs.nth(index).is_checked()

                first_form.locator("button[type=submit]").click()
                expect(page.locator("[role=alert]")).to_contain_text("每个相似组至少保留 1 张")
                assert fetch_all(library_db, "SELECT COUNT(*) FROM export_abandoned_asset")[0][0] == 0

                first_form = page.locator("form[data-burst-pick-form]").first
                first_form.locator("input[type=checkbox][name^='keep_asset_id__']").first.check()
                first_form.locator("button[type=submit]").click()
                expect(page.locator("[role=status]")).to_contain_text("连拍挑选已保存")

                abandoned_rows = fetch_all(
                    library_db,
                    "SELECT asset_id, triggered_template_id FROM export_abandoned_asset ORDER BY asset_id",
                )
                assert abandoned_rows
                assert {str(row[1]) for row in abandoned_rows} == {template_id}
                submitted_rows = fetch_all(
                    library_db,
                    """
                    SELECT submitted_at
                    FROM export_burst_pick_group
                    WHERE group_key = ?
                    """,
                    (submitted_group_key,),
                )
                assert submitted_rows and submitted_rows[0][0] is not None

                response = httpx.get(
                    f"{base_url}/api/export-templates/{template_id}/burst-pick",
                    timeout=30.0,
                )
                assert response.status_code == 200
                remaining_group_keys = {str(group["group_key"]) for group in response.json()["groups"]}
                assert submitted_group_key not in remaining_group_keys

                browser.close()
        finally:
            terminate_process(process)
