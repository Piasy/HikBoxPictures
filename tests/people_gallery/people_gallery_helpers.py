"""test_webui_people_gallery 子文件共享 helper 函数与常量。"""

from __future__ import annotations

from collections.abc import Iterator
import hashlib
import json
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
from playwright.sync_api import Page
from playwright.sync_api import expect

from tests.helpers import (
    REPO_ROOT,
    FIXTURE_DIR,
    MANIFEST_PATH,
    fetch_all,
)


FIXTURE_DIR_2 = REPO_ROOT / "tests" / "fixtures" / "people_gallery_scan_2"
MANIFEST_PATH_2 = FIXTURE_DIR_2 / "manifest.json"
SUPPORTED_SCAN_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".heif"}


# ---------------------------------------------------------------------------
# 增量 manifest 加载
# ---------------------------------------------------------------------------


def load_incremental_manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH_2.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# DB 读取辅助
# ---------------------------------------------------------------------------


def fetch_one(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> tuple[object, ...]:
    rows = fetch_all(db_path, sql, params)
    assert rows
    return rows[0]


def asset_assignment_rows(library_db: Path) -> dict[str, list[tuple[int, str, str]]]:
    rows = fetch_all(
        library_db,
        """
        SELECT
          assets.file_name,
          face_observations.face_index,
          person_face_assignments.person_id,
          person_face_assignments.assignment_source
        FROM person_face_assignments
        INNER JOIN face_observations
          ON face_observations.id = person_face_assignments.face_observation_id
        INNER JOIN assets
          ON assets.id = face_observations.asset_id
        WHERE person_face_assignments.active = 1
        ORDER BY assets.file_name ASC, face_observations.face_index ASC
        """,
    )
    result: dict[str, list[tuple[int, str, str]]] = {}
    for file_name, face_index, person_id, assignment_source in rows:
        result.setdefault(str(file_name), []).append((int(face_index), str(person_id), str(assignment_source)))
    return result


def expected_target_mapping(library_db: Path, manifest: dict[str, object]) -> dict[str, str]:
    assignment_rows = asset_assignment_rows(library_db)
    mapping: dict[str, str] = {}
    for label in manifest["expected_person_groups"]:
        observed_person_ids: set[str] = set()
        observed_asset_files: list[str] = []
        for asset in manifest["assets"]:
            if asset["expected_target_people"] != [label]:
                continue
            file_name = str(asset["file"])
            assigned_rows = assignment_rows.get(file_name, [])
            if not assigned_rows:
                continue
            observed_asset_files.append(file_name)
            observed_person_ids.update(person_id for _, person_id, _ in assigned_rows)
        assert observed_asset_files, f"{label} 缺少可用于建立人物映射的实际 target assignment"
        assert len(observed_person_ids) == 1, (
            f"{label} 的实际 target assignment 未稳定映射到唯一 person: {sorted(observed_person_ids)}"
        )
        mapping[str(label)] = next(iter(observed_person_ids))
    assert len(set(mapping.values())) == len(mapping)
    return mapping


def read_active_people(library_db: Path) -> dict[str, dict[str, object]]:
    rows = fetch_all(
        library_db,
        """
        SELECT
          person.id,
          person.display_name,
          person.is_named,
          COUNT(person_face_assignments.id) AS sample_count
        FROM person
        INNER JOIN person_face_assignments
          ON person_face_assignments.person_id = person.id
         AND person_face_assignments.active = 1
        WHERE person.status = 'active'
        GROUP BY person.id, person.display_name, person.is_named
        ORDER BY person.id ASC
        """,
    )
    people: dict[str, dict[str, object]] = {}
    for person_id, display_name, is_named, sample_count in rows:
        context_paths = [
            Path(str(path))
            for path, in fetch_all(
                library_db,
                """
                SELECT face_observations.context_path
                FROM person_face_assignments
                INNER JOIN face_observations
                  ON face_observations.id = person_face_assignments.face_observation_id
                WHERE person_face_assignments.person_id = ?
                  AND person_face_assignments.active = 1
                ORDER BY person_face_assignments.id ASC
                """,
                (str(person_id),),
            )
        ]
        people[str(person_id)] = {
            "display_name": None if display_name is None else str(display_name),
            "is_named": bool(is_named),
            "sample_count": int(sample_count),
            "context_paths": context_paths,
        }
    return people


def read_active_assignment_details(library_db: Path, person_id: str) -> list[dict[str, object]]:
    rows = fetch_all(
        library_db,
        """
        SELECT
          person_face_assignments.id,
          person_face_assignments.face_observation_id,
          face_observations.context_path,
          assets.id,
          assets.file_name,
          COALESCE(assets.live_photo_mov_path, '')
        FROM person_face_assignments
        INNER JOIN face_observations
          ON face_observations.id = person_face_assignments.face_observation_id
        INNER JOIN assets
          ON assets.id = face_observations.asset_id
        WHERE person_face_assignments.person_id = ?
          AND person_face_assignments.active = 1
        ORDER BY person_face_assignments.id ASC
        """,
        (person_id,),
    )
    return [
        {
            "assignment_id": int(assignment_id),
            "face_observation_id": int(face_observation_id),
            "context_path": Path(str(context_path)),
            "asset_id": str(asset_id),
            "file_name": str(file_name),
            "is_live": bool(live_photo_mov_path),
        }
        for assignment_id, face_observation_id, context_path, asset_id, file_name, live_photo_mov_path in rows
    ]


def read_person_record(library_db: Path, person_id: str) -> dict[str, object]:
    person_id_row, display_name, is_named, status, updated_at = fetch_one(
        library_db,
        """
        SELECT id, display_name, is_named, status, updated_at
        FROM person
        WHERE id = ?
        """,
        (person_id,),
    )
    return {
        "id": str(person_id_row),
        "display_name": None if display_name is None else str(display_name),
        "is_named": bool(is_named),
        "status": str(status),
        "updated_at": str(updated_at),
    }


def read_active_assignment_ids(library_db: Path, person_id: str) -> list[int]:
    return [
        int(assignment_id)
        for assignment_id, in fetch_all(
            library_db,
            """
            SELECT id
            FROM person_face_assignments
            WHERE person_id = ?
              AND active = 1
            ORDER BY id ASC
            """,
            (person_id,),
        )
    ]


def read_face_assignment_rows(library_db: Path, face_observation_id: int) -> list[dict[str, object]]:
    rows = fetch_all(
        library_db,
        """
        SELECT id, person_id, active
        FROM person_face_assignments
        WHERE face_observation_id = ?
        ORDER BY id ASC
        """,
        (face_observation_id,),
    )
    return [
        {
            "assignment_id": int(assignment_id),
            "person_id": str(person_id),
            "active": bool(active),
        }
        for assignment_id, person_id, active in rows
    ]


def read_person_face_exclusions(
    library_db: Path,
    *,
    face_observation_id: int | None = None,
    excluded_person_id: str | None = None,
) -> list[dict[str, object]]:
    sql = """
        SELECT
          id,
          face_observation_id,
          excluded_person_id,
          source_assignment_id,
          created_at
        FROM person_face_exclusions
    """
    params: list[object] = []
    clauses: list[str] = []
    if face_observation_id is not None:
        clauses.append("face_observation_id = ?")
        params.append(face_observation_id)
    if excluded_person_id is not None:
        clauses.append("excluded_person_id = ?")
        params.append(excluded_person_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id ASC"
    rows = fetch_all(library_db, sql, tuple(params))
    return [
        {
            "id": int(exclusion_id),
            "face_observation_id": int(face_id),
            "excluded_person_id": str(person_id),
            "source_assignment_id": None if assignment_id is None else int(assignment_id),
            "created_at": str(created_at),
        }
        for exclusion_id, face_id, person_id, assignment_id, created_at in rows
    ]


def active_assignment_details_by_file_name(
    library_db: Path,
    *,
    person_id: str,
) -> dict[str, dict[str, object]]:
    return {
        str(item["file_name"]): item
        for item in read_active_assignment_details(library_db, person_id)
    }


def read_person_name_events(library_db: Path, person_id: str) -> list[dict[str, object]]:
    rows = fetch_all(
        library_db,
        """
        SELECT
          id,
          event_type,
          old_display_name,
          new_display_name,
          created_at
        FROM person_name_events
        WHERE person_id = ?
        ORDER BY id ASC
        """,
        (person_id,),
    )
    return [
        {
            "id": int(event_id),
            "event_type": str(event_type),
            "old_display_name": None if old_display_name is None else str(old_display_name),
            "new_display_name": str(new_display_name),
            "created_at": str(created_at),
        }
        for event_id, event_type, old_display_name, new_display_name, created_at in rows
    ]


def count_person_name_events(library_db: Path) -> int:
    return int(fetch_one(library_db, "SELECT COUNT(*) FROM person_name_events")[0])


def read_person_merge_operations(library_db: Path) -> list[dict[str, object]]:
    rows = fetch_all(
        library_db,
        """
        SELECT
          id,
          winner_person_id,
          loser_person_id,
          winner_display_name_before,
          winner_is_named_before,
          winner_status_before,
          loser_display_name_before,
          loser_is_named_before,
          loser_status_before,
          merged_at,
          undone_at
        FROM person_merge_operations
        ORDER BY id ASC
        """,
    )
    return [
        {
            "id": int(merge_id),
            "winner_person_id": str(winner_person_id),
            "loser_person_id": str(loser_person_id),
            "winner_display_name_before": (
                None if winner_display_name_before is None else str(winner_display_name_before)
            ),
            "winner_is_named_before": bool(winner_is_named_before),
            "winner_status_before": str(winner_status_before),
            "loser_display_name_before": None if loser_display_name_before is None else str(loser_display_name_before),
            "loser_is_named_before": bool(loser_is_named_before),
            "loser_status_before": str(loser_status_before),
            "merged_at": str(merged_at),
            "undone_at": None if undone_at is None else str(undone_at),
        }
        for (
            merge_id,
            winner_person_id,
            loser_person_id,
            winner_display_name_before,
            winner_is_named_before,
            winner_status_before,
            loser_display_name_before,
            loser_is_named_before,
            loser_status_before,
            merged_at,
            undone_at,
        ) in rows
    ]


def read_merge_operation_assignment_rows(
    library_db: Path,
    *,
    merge_operation_id: int,
) -> list[dict[str, object]]:
    rows = fetch_all(
        library_db,
        """
        SELECT assignment_id, person_role
        FROM person_merge_operation_assignments
        WHERE merge_operation_id = ?
        ORDER BY id ASC
        """,
        (merge_operation_id,),
    )
    return [
        {
            "assignment_id": int(assignment_id),
            "person_role": str(person_role),
        }
        for assignment_id, person_role in rows
    ]


def read_name_slice_db_snapshot(library_db: Path) -> dict[str, object]:
    return {
        "people": fetch_all(
            library_db,
            """
            SELECT id, display_name, is_named, status, updated_at
            FROM person
            ORDER BY id ASC
            """,
        ),
        "active_assignments": fetch_all(
            library_db,
            """
            SELECT id, person_id, face_observation_id, active, updated_at
            FROM person_face_assignments
            ORDER BY id ASC
            """,
        ),
        "name_events": fetch_all(
            library_db,
            """
            SELECT id, person_id, event_type, old_display_name, new_display_name, created_at
            FROM person_name_events
            ORDER BY id ASC
            """,
        ),
    }


def read_assignment_owner_snapshot(library_db: Path) -> list[tuple[object, ...]]:
    return fetch_all(
        library_db,
        """
        SELECT id, person_id, face_observation_id, active
        FROM person_face_assignments
        ORDER BY id ASC
        """,
    )


# ---------------------------------------------------------------------------
# 图片 / SHA 辅助
# ---------------------------------------------------------------------------


def fetch_image_bytes(base_url: str, src: str) -> bytes:
    if src.startswith("http://") or src.startswith("https://"):
        url = src
    else:
        url = f"{base_url}{src}"
    response = httpx.get(url, timeout=5.0)
    response.raise_for_status()
    return response.content


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# fixture 文件复制
# ---------------------------------------------------------------------------


def copy_fixture_files(source_dir: Path, file_names: list[str]) -> None:
    source_dir.mkdir(parents=True, exist_ok=True)
    for file_name in file_names:
        shutil.copy2(FIXTURE_DIR / file_name, source_dir / file_name)


def set_person_created_at_order(library_db: Path, person_ids: list[str]) -> None:
    connection = sqlite3.connect(library_db)
    try:
        with connection:
            for index, person_id in enumerate(person_ids, start=1):
                timestamp = f"2026-04-24T00:00:{index:02d}Z"
                connection.execute(
                    """
                    UPDATE person
                    SET created_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (timestamp, timestamp, person_id),
                )
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# manifest 辅助
# ---------------------------------------------------------------------------


def manifest_files_for_target(manifest: dict[str, object], label: str) -> list[str]:
    return [
        str(asset["file"])
        for asset in manifest["assets"]
        if asset["expected_target_people"] == [label]
    ]


# ---------------------------------------------------------------------------
# 页面交互辅助
# ---------------------------------------------------------------------------


def page_card_snapshot(page: Page) -> dict[str, dict[str, object]]:
    cards = page.locator("[data-person-id]")
    result: dict[str, dict[str, object]] = {}
    for index in range(cards.count()):
        card = cards.nth(index)
        person_id = str(card.get_attribute("data-person-id"))
        result[person_id] = {
            "label": card.locator("[data-person-label]").inner_text().strip(),
            "sample_count_text": card.locator("[data-sample-count]").inner_text().strip(),
            "image_src": str(card.locator("img").get_attribute("src")),
        }
    return result


def open_person_detail_from_home(page: Page, *, base_url: str, entry_path: str, person_id: str) -> None:
    page.goto(f"{base_url}{entry_path}", wait_until="networkidle")
    card_link = page.locator(f"[data-person-id='{person_id}'] a").first
    expect(card_link).to_be_visible()
    card_link.click()
    page.wait_for_url(re.compile(rf".*/people/{re.escape(person_id)}(?:\?.*)?$"))


def iter_rendered_assignment_cards(page: Page) -> Iterator[tuple[int, str, object]]:
    cards = page.locator("[data-assignment-id]")
    for index in range(cards.count()):
        card = cards.nth(index)
        assignment_id = int(str(card.get_attribute("data-assignment-id")))
        asset_id = str(card.get_attribute("data-asset-id"))
        yield assignment_id, asset_id, card


def people_section_person_ids(page: Page, *, section: str) -> set[str]:
    cards = page.locator(f"[data-people-section='{section}'] [data-person-id]")
    return {
        str(cards.nth(index).get_attribute("data-person-id"))
        for index in range(cards.count())
    }


def people_section_person_ids_in_rendered_order(page: Page, *, section: str) -> list[str]:
    cards = page.locator(f"[data-people-section='{section}'] [data-person-id]")
    return [
        str(cards.nth(index).get_attribute("data-person-id"))
        for index in range(cards.count())
    ]


def submit_name_form(
    page: Page,
    *,
    detail_url_pattern: re.Pattern[str],
    display_name: str,
) -> None:
    page.get_by_label("人物名称").fill(display_name)
    page.get_by_role("button", name="保存名称").click()
    page.wait_for_url(detail_url_pattern)
    page.wait_for_load_state("networkidle")


def assert_name_prg_flow(
    *,
    responses: list[dict[str, object]],
    base_url: str,
    person_id: str,
) -> None:
    post_url = f"{base_url}/people/{person_id}/name"
    detail_url = f"{base_url}/people/{person_id}"
    assert any(
        response["method"] == "POST"
        and response["url"] == post_url
        and int(response["status"]) in {302, 303}
        for response in responses
    ), responses
    assert any(
        response["method"] == "GET"
        and response["url"] == detail_url
        and int(response["status"]) == 200
        for response in responses
    ), responses


def submit_merge_from_home(
    page: Page,
    *,
    base_url: str,
    person_ids: list[str],
) -> None:
    page.goto(f"{base_url}/people", wait_until="networkidle")
    for person_id in person_ids:
        checkbox = page.locator(f"[data-person-id='{person_id}'] [data-merge-checkbox]")
        expect(checkbox).to_be_visible()
        checkbox.check()
    page.get_by_role("button", name="合并所选人物").click()
    page.wait_for_url(re.compile(rf"{re.escape(base_url)}/people(?:\\?.*)?$"))
    page.wait_for_load_state("networkidle")


def assert_merge_prg_flow(
    *,
    responses: list[dict[str, object]],
    base_url: str,
) -> None:
    post_url = f"{base_url}/people/merge"
    people_url = f"{base_url}/people"
    assert any(
        response["method"] == "POST"
        and response["url"] == post_url
        and int(response["status"]) == 303
        for response in responses
    ), responses
    assert any(
        response["method"] == "GET"
        and response["url"] == people_url
        and int(response["status"]) == 200
        for response in responses
    ), responses


def submit_undo_from_home(page: Page, *, base_url: str) -> None:
    page.goto(f"{base_url}/people", wait_until="networkidle")
    undo_button = page.locator("[data-undo-submit]")
    expect(undo_button).to_be_visible()
    undo_button.click()
    page.wait_for_url(re.compile(rf"{re.escape(base_url)}/people(?:\\?.*)?$"))
    page.wait_for_load_state("networkidle")


def assert_undo_prg_flow(
    *,
    responses: list[dict[str, object]],
    base_url: str,
) -> None:
    post_url = f"{base_url}/people/merge/undo"
    people_url = f"{base_url}/people"
    assert any(
        response["method"] == "POST"
        and response["url"] == post_url
        and int(response["status"]) == 303
        for response in responses
    ), responses
    assert any(
        response["method"] == "GET"
        and response["url"] == people_url
        and int(response["status"]) == 200
        for response in responses
    ), responses


def submit_exclusion_from_detail(
    page: Page,
    *,
    base_url: str,
    person_id: str,
    assignment_ids: list[int],
) -> None:
    detail_url_pattern = re.compile(rf"{re.escape(base_url)}/people(?:/{re.escape(person_id)})?(?:\\?.*)?$")
    page.goto(f"{base_url}/people/{person_id}", wait_until="networkidle")
    for assignment_id in assignment_ids:
        checkbox = page.locator(
            f"[data-assignment-id='{assignment_id}'] [data-exclude-checkbox]"
        )
        expect(checkbox).to_be_visible()
        checkbox.check()
    page.get_by_role("button", name="批量排除所选样本").click()
    page.wait_for_url(detail_url_pattern)
    page.wait_for_load_state("networkidle")


def assert_exclusion_prg_flow(
    *,
    responses: list[dict[str, object]],
    base_url: str,
    person_id: str,
    redirected_to_home: bool,
) -> None:
    post_url = f"{base_url}/people/{person_id}/exclude"
    target_url = f"{base_url}/people" if redirected_to_home else f"{base_url}/people/{person_id}"
    assert any(
        response["method"] == "POST"
        and response["url"] == post_url
        and int(response["status"]) == 303
        for response in responses
    ), responses
    assert any(
        response["method"] == "GET"
        and response["url"] == target_url
        and int(response["status"]) == 200
        for response in responses
    ), responses


def extract_merge_request_person_ids(
    *,
    requests: list[dict[str, object]],
    base_url: str,
) -> list[str]:
    merge_url = f"{base_url}/people/merge"
    for request in requests:
        if request["method"] != "POST" or request["url"] != merge_url:
            continue
        body = str(request["post_data"] or "")
        return [str(person_id) for person_id in parse_qs(body, keep_blank_values=True).get("person_id", [])]
    raise AssertionError(f"未捕获到 {merge_url} 的 POST 请求: {requests}")
