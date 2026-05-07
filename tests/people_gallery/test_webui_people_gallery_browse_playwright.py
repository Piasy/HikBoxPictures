from __future__ import annotations

import re
from pathlib import Path
import sqlite3

import httpx
import pytest
from playwright.sync_api import Page
from playwright.sync_api import expect
from playwright.sync_api import sync_playwright

from tests.helpers import (
    FIXTURE_DIR,
    add_source,
    fetch_all,
    find_free_port,
    init_workspace,
    load_manifest,
    prepare_workspace_models,
    run_hikbox,
    spawn_hikbox,
    terminate_process,
    wait_for_http_ready,
)
from tests.people_gallery.people_gallery_helpers import (
    copy_fixture_files,
    expected_target_mapping,
    fetch_image_bytes,
    iter_rendered_assignment_cards,
    open_person_detail_from_home,
    page_card_snapshot,
    people_section_person_ids,
    people_section_person_ids_in_rendered_order,
    read_active_assignment_details,
    read_active_people,
    set_person_created_at_order,
    sha256_bytes,
    submit_name_form,
)


def test_people_gallery_home_sections_sort_by_sample_count_with_slice0_gallery(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    source_dir = tmp_path / "slice0-subset"
    manifest = load_manifest()
    copy_fixture_files(
        source_dir,
        [
            "pg_001_single_alex_01.jpg",
            "pg_002_single_alex_02.jpg",
            "pg_003_single_alex_03.jpg",
            "pg_011_single_blair_01.jpg",
            "pg_012_single_blair_02.jpg",
            "pg_013_single_blair_03.jpg",
            "pg_014_single_blair_04.jpg",
            "pg_015_single_blair_05.jpg",
            "pg_021_single_casey_01.jpg",
            "pg_022_single_casey_02.jpg",
            "pg_023_single_casey_03.jpg",
            "pg_024_single_casey_04.jpg",
        ],
    )
    init_result = init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    prepare_workspace_models(workspace)
    add_result = add_source(workspace, source_dir)
    assert add_result.returncode == 0

    scan_result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "6",
    )
    assert scan_result.returncode == 0, scan_result.stderr

    library_db = workspace / ".hikbox" / "library.db"
    target_person_ids = expected_target_mapping(library_db, manifest)
    alex_person_id = target_person_ids["target_alex"]
    blair_person_id = target_person_ids["target_blair"]
    casey_person_id = target_person_ids["target_casey"]
    expected_people = read_active_people(library_db)
    assert expected_people[alex_person_id]["sample_count"] == 3
    assert expected_people[blair_person_id]["sample_count"] == 5
    assert expected_people[casey_person_id]["sample_count"] == 4
    set_person_created_at_order(library_db, [alex_person_id, casey_person_id, blair_person_id])

    port = find_free_port()
    process = spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
    )
    base_url = f"http://127.0.0.1:{port}"

    try:
        wait_for_http_ready(f"{base_url}/")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})

            page.goto(f"{base_url}/people", wait_until="networkidle")
            assert people_section_person_ids_in_rendered_order(page, section="anonymous") == [
                blair_person_id,
                casey_person_id,
                alex_person_id,
            ]

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
                display_name="A-Low Sample",
            )
            open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=blair_person_id)
            submit_name_form(
                page,
                detail_url_pattern=blair_detail_pattern,
                display_name="Z-High Sample",
            )

            page.goto(f"{base_url}/people", wait_until="networkidle")
            assert people_section_person_ids_in_rendered_order(page, section="named") == [
                blair_person_id,
                alex_person_id,
            ]
            assert people_section_person_ids_in_rendered_order(page, section="anonymous") == [
                casey_person_id,
            ]

            browser.close()
    finally:
        terminate_process(process)


def test_people_gallery_browse_via_real_serve_and_real_page(scanned_workspace, tmp_path: Path) -> None:
    workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
    expected_people = read_active_people(library_db)
    manifest_asset_id_by_file_name = {
        str(asset["file"]): str(asset["id"]) for asset in manifest["assets"]
    }
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

            page.goto(f"{base_url}/", wait_until="networkidle")
            expect(page.get_by_role("heading", name="已命名人物")).to_be_visible()
            expect(page.get_by_role("heading", name="匿名人物")).to_be_visible()
            root_cards = page_card_snapshot(page)
            assert set(root_cards) == set(expected_people)

            anonymous_labels_from_root: dict[str, str] = {}
            for person_id, expected_person in expected_people.items():
                card = root_cards[person_id]
                assert str(expected_person["sample_count"]) in str(card["sample_count_text"])
                if expected_person["is_named"]:
                    assert card["label"] == expected_person["display_name"]
                else:
                    assert card["label"]
                    anonymous_labels_from_root[person_id] = str(card["label"])
                image_bytes = fetch_image_bytes(base_url, str(card["image_src"]))
                image_sha = sha256_bytes(image_bytes)
                assert image_sha in {
                    sha256_bytes(path.read_bytes()) for path in expected_person["context_paths"]
                }

            page.goto(f"{base_url}/people", wait_until="networkidle")
            people_cards = page_card_snapshot(page)
            assert set(people_cards) == set(expected_people)
            for person_id, label in anonymous_labels_from_root.items():
                assert people_cards[person_id]["label"] == label
            page.reload(wait_until="networkidle")
            refreshed_cards = page_card_snapshot(page)
            for person_id, label in anonymous_labels_from_root.items():
                assert refreshed_cards[person_id]["label"] == label

            expected_page_sizes = [7, 7, 4]
            all_live_assets: dict[str, set[str]] = {}
            for entry_path, label in [
                ("/", "target_alex"),
                ("/people", "target_blair"),
                ("/people", "target_casey"),
            ]:
                person_id = target_person_ids[label]
                expected_assignments = read_active_assignment_details(library_db, person_id)
                expected_assignment_ids = {item["assignment_id"] for item in expected_assignments}
                expected_assignment_by_id = {
                    int(item["assignment_id"]): item for item in expected_assignments
                }
                seen_assignment_ids: list[int] = []
                seen_live_assets: set[str] = set()

                open_person_detail_from_home(page, base_url=base_url, entry_path=entry_path, person_id=person_id)
                expect(page.get_by_test_id("person-detail")).to_be_visible()
                rendered_page_one = list(iter_rendered_assignment_cards(page))
                assert len(rendered_page_one) == 7

                if label == "target_alex":
                    sample_boxes = [
                        card.bounding_box()
                        for _, _, card in rendered_page_one
                    ]
                    assert all(box is not None for box in sample_boxes[:7])
                    first_row_y = round(float(sample_boxes[0]["y"]), 1)
                    assert all(round(float(box["y"]), 1) == first_row_y for box in sample_boxes[:6])
                    assert round(float(sample_boxes[6]["y"]), 1) > first_row_y

                for page_number, expected_count in enumerate(expected_page_sizes, start=1):
                    expect(page.locator("[data-current-page]")).to_have_attribute("data-current-page", str(page_number))
                    expect(page.locator("[data-total-pages]")).to_have_attribute("data-total-pages", "3")
                    rendered_cards = list(iter_rendered_assignment_cards(page))
                    assert len(rendered_cards) == expected_count
                    for assignment_id, asset_id, card in rendered_cards:
                        image_locator = card.locator("img")
                        assert image_locator.count() == 1
                        image_bytes = fetch_image_bytes(base_url, str(image_locator.get_attribute("src")))
                        expected_row = expected_assignment_by_id[assignment_id]
                        assert image_bytes == expected_row["context_path"].read_bytes()
                        badge_locator = card.locator("[data-live-badge]")
                        if expected_row["is_live"]:
                            expect(badge_locator).to_have_text("Live")
                            seen_live_assets.add(manifest_asset_id_by_file_name[expected_row["file_name"]])
                        else:
                            assert badge_locator.count() == 0
                        seen_assignment_ids.append(assignment_id)

                    if page_number == 2:
                        assert "page=2" in page.url
                        page.reload(wait_until="networkidle")
                        reloaded_ids = [
                            assignment_id for assignment_id, _, _ in iter_rendered_assignment_cards(page)
                        ]
                        assert reloaded_ids == [assignment_id for assignment_id, _, _ in rendered_cards]

                    if page_number < 3:
                        page.get_by_role("link", name=f"第 {page_number + 1} 页").click()
                        page.wait_for_url(
                            re.compile(
                                rf".*/people/{re.escape(person_id)}\?page={page_number + 1}$"
                            )
                        )

                assert set(seen_assignment_ids) == expected_assignment_ids
                assert len(seen_assignment_ids) == len(expected_assignment_ids)
                all_live_assets[label] = seen_live_assets
                page.get_by_role("link", name="返回人物首页").click()

            assert "asset_047" in all_live_assets["target_alex"]
            assert "asset_048" in all_live_assets["target_casey"]
            assert "asset_049" not in all_live_assets["target_alex"]
            assert "asset_050" not in all_live_assets["target_casey"]
            assert all_live_assets["target_blair"] == set()

            page.goto(f"{base_url}/people/not-a-real-person", wait_until="networkidle")
            assert page.locator("body").inner_text().strip()
            browser.close()
    finally:
        terminate_process(process)


def test_people_home_section_counts(scanned_workspace, tmp_path: Path) -> None:
    workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
    expected_people = read_active_people(library_db)
    named_count = sum(1 for p in expected_people.values() if p["is_named"])
    anonymous_count = sum(1 for p in expected_people.values() if not p["is_named"])
    total_asset_count = int(
        fetch_all(
            library_db,
            """
            SELECT COUNT(DISTINCT assets.id)
            FROM person_face_assignments
            INNER JOIN face_observations
              ON face_observations.id = person_face_assignments.face_observation_id
            INNER JOIN assets
              ON assets.id = face_observations.asset_id
            INNER JOIN person
              ON person.id = person_face_assignments.person_id
            WHERE person_face_assignments.active = 1
              AND person.status = 'active'
            """,
        )[0][0]
    )

    port = find_free_port()
    process = spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
    )
    base_url = f"http://127.0.0.1:{port}"

    try:
        wait_for_http_ready(f"{base_url}/")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})

            page.goto(f"{base_url}/people", wait_until="networkidle")

            # "人物库浏览" 标题旁显示总照片资产数
            hero_heading = page.locator("h1")
            expect(hero_heading).to_contain_text("人物库浏览")
            hero_count = page.locator("[data-total-asset-count]")
            expect(hero_count).to_be_visible()
            expect(hero_count).to_contain_text(str(total_asset_count))

            # "已命名人物" 标题旁显示人物数
            named_heading = page.locator("[data-people-section='named'] h2")
            expect(named_heading).to_contain_text("已命名人物")
            named_count_el = page.locator("[data-named-people-count]")
            expect(named_count_el).to_be_visible()
            expect(named_count_el).to_contain_text(str(named_count))

            # "匿名人物" 标题旁显示人物数
            anonymous_heading = page.locator("[data-people-section='anonymous'] h2")
            expect(anonymous_heading).to_contain_text("匿名人物")
            anonymous_count_el = page.locator("[data-anonymous-people-count]")
            expect(anonymous_count_el).to_be_visible()
            expect(anonymous_count_el).to_contain_text(str(anonymous_count))

            browser.close()
    finally:
        terminate_process(process)
