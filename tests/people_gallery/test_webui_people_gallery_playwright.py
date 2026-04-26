from __future__ import annotations

from collections.abc import Iterator
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.parse import parse_qs

import httpx
from playwright.sync_api import Page
from playwright.sync_api import expect
from playwright.sync_api import sync_playwright


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "people_gallery_scan"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"
FIXTURE_DIR_2 = REPO_ROOT / "tests" / "fixtures" / "people_gallery_scan_2"
MANIFEST_PATH_2 = FIXTURE_DIR_2 / "manifest.json"
SUPPORTED_SCAN_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".heif"}


def _run_hikbox(
    *args: str,
    cwd: Path | None = None,
    env_updates: dict[str, str] | None = None,
    pythonpath_prepend: list[Path] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    pythonpath_parts = [str(path) for path in (pythonpath_prepend or [])]
    pythonpath_parts.append(str(REPO_ROOT))
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    if env_updates:
        env.update(env_updates)
    return subprocess.run(
        [sys.executable, "-m", "hikbox_pictures", *args],
        cwd=cwd or REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _spawn_hikbox(
    *args: str,
    cwd: Path | None = None,
    env_updates: dict[str, str] | None = None,
    pythonpath_prepend: list[Path] | None = None,
) -> subprocess.Popen[str]:
    env = os.environ.copy()
    pythonpath_parts = [str(path) for path in (pythonpath_prepend or [])]
    pythonpath_parts.append(str(REPO_ROOT))
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    if env_updates:
        env.update(env_updates)
    return subprocess.Popen(
        [sys.executable, "-m", "hikbox_pictures", *args],
        cwd=cwd or REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _init_workspace(workspace: Path, external_root: Path) -> subprocess.CompletedProcess[str]:
    return _run_hikbox(
        "init",
        "--workspace",
        str(workspace),
        "--external-root",
        str(external_root),
    )


def _add_source(workspace: Path, source_dir: Path) -> subprocess.CompletedProcess[str]:
    return _run_hikbox(
        "source",
        "add",
        "--workspace",
        str(workspace),
        str(source_dir),
    )


def _prepare_workspace_models(workspace: Path) -> None:
    source_root = _find_model_root()
    target_root = workspace / ".hikbox" / "models" / "insightface"
    if target_root.exists():
        shutil.rmtree(target_root)
    shutil.copytree(source_root, target_root)


def _find_model_root() -> Path:
    candidates = [REPO_ROOT / ".insightface", Path.home() / ".insightface"]
    candidates.extend(parent / ".insightface" for parent in REPO_ROOT.parents)
    for candidate in candidates:
        if (candidate / "models" / "buffalo_l" / "det_10g.onnx").exists():
            return candidate
    raise AssertionError("缺少 InsightFace buffalo_l 模型目录，无法执行 Playwright 真实集成测试")


def _load_manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _load_incremental_manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH_2.read_text(encoding="utf-8"))


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(sock.getsockname()[1])


def _wait_for_http_ready(base_url: str) -> None:
    deadline = time.time() + 30
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            response = httpx.get(base_url, follow_redirects=True, timeout=1.0)
            if response.status_code < 500:
                return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        time.sleep(0.2)
    raise AssertionError(f"等待服务可用超时: {base_url}; last_error={last_error!r}")


def _terminate_process(process: subprocess.Popen[str]) -> tuple[str, str]:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    try:
        stdout_text, stderr_text = process.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout_text, stderr_text = process.communicate(timeout=30)
    return stdout_text, stderr_text


def _fetch_all(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(db_path)
    try:
        return [tuple(row) for row in connection.execute(sql, params).fetchall()]
    finally:
        connection.close()


def _fetch_one(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> tuple[object, ...]:
    rows = _fetch_all(db_path, sql, params)
    assert rows
    return rows[0]


def _asset_assignment_rows(library_db: Path) -> dict[str, list[tuple[int, str, str]]]:
    rows = _fetch_all(
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


def _expected_target_mapping(library_db: Path, manifest: dict[str, object]) -> dict[str, str]:
    assignment_rows = _asset_assignment_rows(library_db)
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


def _read_active_people(library_db: Path) -> dict[str, dict[str, object]]:
    rows = _fetch_all(
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
            for path, in _fetch_all(
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


def _read_active_assignment_details(library_db: Path, person_id: str) -> list[dict[str, object]]:
    rows = _fetch_all(
        library_db,
        """
        SELECT
          person_face_assignments.id,
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
            "context_path": Path(str(context_path)),
            "asset_id": str(asset_id),
            "file_name": str(file_name),
            "is_live": bool(live_photo_mov_path),
        }
        for assignment_id, context_path, asset_id, file_name, live_photo_mov_path in rows
    ]


def _read_person_record(library_db: Path, person_id: str) -> dict[str, object]:
    person_id_row, display_name, is_named, status, updated_at = _fetch_one(
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


def _read_active_assignment_ids(library_db: Path, person_id: str) -> list[int]:
    return [
        int(assignment_id)
        for assignment_id, in _fetch_all(
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


def _read_person_name_events(library_db: Path, person_id: str) -> list[dict[str, object]]:
    rows = _fetch_all(
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


def _count_person_name_events(library_db: Path) -> int:
    return int(_fetch_one(library_db, "SELECT COUNT(*) FROM person_name_events")[0])


def _read_person_merge_operations(library_db: Path) -> list[dict[str, object]]:
    rows = _fetch_all(
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


def _read_merge_operation_assignment_rows(
    library_db: Path,
    *,
    merge_operation_id: int,
) -> list[dict[str, object]]:
    rows = _fetch_all(
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


def _read_name_slice_db_snapshot(library_db: Path) -> dict[str, object]:
    return {
        "people": _fetch_all(
            library_db,
            """
            SELECT id, display_name, is_named, status, updated_at
            FROM person
            ORDER BY id ASC
            """,
        ),
        "active_assignments": _fetch_all(
            library_db,
            """
            SELECT id, person_id, face_observation_id, active, updated_at
            FROM person_face_assignments
            ORDER BY id ASC
            """,
        ),
        "name_events": _fetch_all(
            library_db,
            """
            SELECT id, person_id, event_type, old_display_name, new_display_name, created_at
            FROM person_name_events
            ORDER BY id ASC
            """,
        ),
    }


def _read_assignment_owner_snapshot(library_db: Path) -> list[tuple[object, ...]]:
    return _fetch_all(
        library_db,
        """
        SELECT id, person_id, face_observation_id, active
        FROM person_face_assignments
        ORDER BY id ASC
        """,
    )


def _fetch_image_bytes(base_url: str, src: str) -> bytes:
    if src.startswith("http://") or src.startswith("https://"):
        url = src
    else:
        url = f"{base_url}{src}"
    response = httpx.get(url, timeout=5.0)
    response.raise_for_status()
    return response.content


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _copy_fixture_files(source_dir: Path, file_names: list[str]) -> None:
    source_dir.mkdir(parents=True, exist_ok=True)
    for file_name in file_names:
        shutil.copy2(FIXTURE_DIR / file_name, source_dir / file_name)


def _set_person_created_at_order(library_db: Path, person_ids: list[str]) -> None:
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


def _page_card_snapshot(page: Page) -> dict[str, dict[str, object]]:
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


def _open_person_detail_from_home(page: Page, *, base_url: str, entry_path: str, person_id: str) -> None:
    page.goto(f"{base_url}{entry_path}", wait_until="networkidle")
    card_link = page.locator(f"[data-person-id='{person_id}'] a").first
    expect(card_link).to_be_visible()
    card_link.click()
    page.wait_for_url(re.compile(rf".*/people/{re.escape(person_id)}(?:\?.*)?$"))


def _iter_rendered_assignment_cards(page: Page) -> Iterator[tuple[int, str, object]]:
    cards = page.locator("[data-assignment-id]")
    for index in range(cards.count()):
        card = cards.nth(index)
        assignment_id = int(str(card.get_attribute("data-assignment-id")))
        asset_id = str(card.get_attribute("data-asset-id"))
        yield assignment_id, asset_id, card


def _people_section_person_ids(page: Page, *, section: str) -> set[str]:
    cards = page.locator(f"[data-people-section='{section}'] [data-person-id]")
    return {
        str(cards.nth(index).get_attribute("data-person-id"))
        for index in range(cards.count())
    }


def _people_section_person_ids_in_rendered_order(page: Page, *, section: str) -> list[str]:
    cards = page.locator(f"[data-people-section='{section}'] [data-person-id]")
    return [
        str(cards.nth(index).get_attribute("data-person-id"))
        for index in range(cards.count())
    ]


def _submit_name_form(
    page: Page,
    *,
    detail_url_pattern: re.Pattern[str],
    display_name: str,
) -> None:
    page.get_by_label("人物名称").fill(display_name)
    page.get_by_role("button", name="保存名称").click()
    page.wait_for_url(detail_url_pattern)
    page.wait_for_load_state("networkidle")


def _assert_name_prg_flow(
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


def _submit_merge_from_home(
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


def _assert_merge_prg_flow(
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


def _submit_undo_from_home(page: Page, *, base_url: str) -> None:
    page.goto(f"{base_url}/people", wait_until="networkidle")
    undo_button = page.locator("[data-undo-submit]")
    expect(undo_button).to_be_visible()
    undo_button.click()
    page.wait_for_url(re.compile(rf"{re.escape(base_url)}/people(?:\\?.*)?$"))
    page.wait_for_load_state("networkidle")


def _assert_undo_prg_flow(
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


def _extract_merge_request_person_ids(
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


def _manifest_files_for_target(manifest: dict[str, object], label: str) -> list[str]:
    return [
        str(asset["file"])
        for asset in manifest["assets"]
        if asset["expected_target_people"] == [label]
    ]


def _create_scanned_workspace(
    tmp_path: Path,
    *,
    fixture_dir: Path = FIXTURE_DIR,
    batch_size: int = 10,
) -> tuple[Path, Path, Path, dict[str, object], dict[str, str]]:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    manifest = _load_manifest()
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, fixture_dir)
    assert add_result.returncode == 0
    scan_result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        str(batch_size),
    )
    assert scan_result.returncode == 0, scan_result.stderr
    library_db = workspace / ".hikbox" / "library.db"
    return workspace, external_root, library_db, manifest, _expected_target_mapping(library_db, manifest)


def test_people_gallery_home_sections_sort_by_sample_count_with_slice0_gallery(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    source_dir = tmp_path / "slice0-subset"
    manifest = _load_manifest()
    _copy_fixture_files(
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
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, source_dir)
    assert add_result.returncode == 0

    scan_result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "6",
    )
    assert scan_result.returncode == 0, scan_result.stderr

    library_db = workspace / ".hikbox" / "library.db"
    target_person_ids = _expected_target_mapping(library_db, manifest)
    alex_person_id = target_person_ids["target_alex"]
    blair_person_id = target_person_ids["target_blair"]
    casey_person_id = target_person_ids["target_casey"]
    expected_people = _read_active_people(library_db)
    assert expected_people[alex_person_id]["sample_count"] == 3
    assert expected_people[blair_person_id]["sample_count"] == 5
    assert expected_people[casey_person_id]["sample_count"] == 4
    _set_person_created_at_order(library_db, [alex_person_id, casey_person_id, blair_person_id])

    port = _find_free_port()
    process = _spawn_hikbox(
        "serve",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
    )
    base_url = f"http://127.0.0.1:{port}"

    try:
        _wait_for_http_ready(f"{base_url}/")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})

            page.goto(f"{base_url}/people", wait_until="networkidle")
            assert _people_section_person_ids_in_rendered_order(page, section="anonymous") == [
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
            _open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=alex_person_id)
            _submit_name_form(
                page,
                detail_url_pattern=alex_detail_pattern,
                display_name="A-Low Sample",
            )
            _open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=blair_person_id)
            _submit_name_form(
                page,
                detail_url_pattern=blair_detail_pattern,
                display_name="Z-High Sample",
            )

            page.goto(f"{base_url}/people", wait_until="networkidle")
            assert _people_section_person_ids_in_rendered_order(page, section="named") == [
                blair_person_id,
                alex_person_id,
            ]
            assert _people_section_person_ids_in_rendered_order(page, section="anonymous") == [
                casey_person_id,
            ]

            browser.close()
    finally:
        _terminate_process(process)


def test_people_gallery_browse_via_real_serve_and_real_page(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    manifest = _load_manifest()
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0

    scan_result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert scan_result.returncode == 0, scan_result.stderr

    library_db = workspace / ".hikbox" / "library.db"
    expected_people = _read_active_people(library_db)
    target_person_ids = _expected_target_mapping(library_db, manifest)
    manifest_asset_id_by_file_name = {
        str(asset["file"]): str(asset["id"]) for asset in manifest["assets"]
    }
    port = _find_free_port()
    process = _spawn_hikbox(
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
        _wait_for_http_ready(f"{base_url}/")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})

            page.goto(f"{base_url}/", wait_until="networkidle")
            expect(page.get_by_role("heading", name="已命名人物")).to_be_visible()
            expect(page.get_by_role("heading", name="匿名人物")).to_be_visible()
            root_cards = _page_card_snapshot(page)
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
                image_bytes = _fetch_image_bytes(base_url, str(card["image_src"]))
                image_sha = _sha256_bytes(image_bytes)
                assert image_sha in {
                    _sha256_bytes(path.read_bytes()) for path in expected_person["context_paths"]
                }

            page.goto(f"{base_url}/people", wait_until="networkidle")
            people_cards = _page_card_snapshot(page)
            assert set(people_cards) == set(expected_people)
            for person_id, label in anonymous_labels_from_root.items():
                assert people_cards[person_id]["label"] == label
            page.reload(wait_until="networkidle")
            refreshed_cards = _page_card_snapshot(page)
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
                expected_assignments = _read_active_assignment_details(library_db, person_id)
                expected_assignment_ids = {item["assignment_id"] for item in expected_assignments}
                expected_assignment_by_id = {
                    int(item["assignment_id"]): item for item in expected_assignments
                }
                seen_assignment_ids: list[int] = []
                seen_live_assets: set[str] = set()

                _open_person_detail_from_home(page, base_url=base_url, entry_path=entry_path, person_id=person_id)
                expect(page.get_by_test_id("person-detail")).to_be_visible()
                rendered_page_one = list(_iter_rendered_assignment_cards(page))
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
                    rendered_cards = list(_iter_rendered_assignment_cards(page))
                    assert len(rendered_cards) == expected_count
                    for assignment_id, asset_id, card in rendered_cards:
                        image_locator = card.locator("img")
                        assert image_locator.count() == 1
                        image_bytes = _fetch_image_bytes(base_url, str(image_locator.get_attribute("src")))
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
                            assignment_id for assignment_id, _, _ in _iter_rendered_assignment_cards(page)
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
        _terminate_process(process)


def test_people_gallery_naming_via_real_serve_real_page_and_real_db(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_root = tmp_path / "external-root"
    manifest = _load_manifest()
    people_by_label = {
        str(person["label"]): person for person in manifest["people"]
    }
    init_result = _init_workspace(workspace, external_root)
    assert init_result.returncode == 0
    _prepare_workspace_models(workspace)
    add_result = _add_source(workspace, FIXTURE_DIR)
    assert add_result.returncode == 0

    scan_result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert scan_result.returncode == 0, scan_result.stderr

    library_db = workspace / ".hikbox" / "library.db"
    target_person_ids = _expected_target_mapping(library_db, manifest)
    alex_person_id = target_person_ids["target_alex"]
    blair_person_id = target_person_ids["target_blair"]
    casey_person_id = target_person_ids["target_casey"]
    alex_manifest_name = str(people_by_label["target_alex"]["display_name"])
    blair_manifest_name = str(people_by_label["target_blair"]["display_name"])
    alex_temporary_name = "Temporary Alex"
    alex_temporary_input = "  Temporary Alex  "
    duplicate_input = f"  {alex_manifest_name}  "
    noop_spaced_input = f"  {alex_manifest_name}  "

    alex_initial_record = _read_person_record(library_db, alex_person_id)
    blair_initial_record = _read_person_record(library_db, blair_person_id)
    casey_initial_record = _read_person_record(library_db, casey_person_id)
    assert alex_initial_record["display_name"] is None
    assert alex_initial_record["is_named"] is False
    assert blair_initial_record["display_name"] is None
    assert blair_initial_record["is_named"] is False
    assert casey_initial_record["display_name"] is None
    assert casey_initial_record["is_named"] is False

    alex_assignment_ids = _read_active_assignment_ids(library_db, alex_person_id)
    blair_assignment_ids = _read_active_assignment_ids(library_db, blair_person_id)
    casey_assignment_ids = _read_active_assignment_ids(library_db, casey_person_id)
    assert alex_assignment_ids
    assert blair_assignment_ids
    assert casey_assignment_ids
    assert _read_person_name_events(library_db, alex_person_id) == []
    assert _count_person_name_events(library_db) == 0

    port = _find_free_port()
    process = _spawn_hikbox(
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
        _wait_for_http_ready(f"{base_url}/")
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
            assert alex_person_id in _people_section_person_ids(page, section="anonymous")
            assert alex_person_id not in _people_section_person_ids(page, section="named")

            alex_detail_pattern = re.compile(rf"{re.escape(base_url)}/people/{re.escape(alex_person_id)}(?:\\?.*)?$")
            _open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=alex_person_id)
            expect(page.get_by_label("人物名称")).to_have_value("")

            response_start = len(response_log)
            _submit_name_form(
                page,
                detail_url_pattern=alex_detail_pattern,
                display_name=alex_temporary_input,
            )
            _assert_name_prg_flow(
                responses=response_log[response_start:],
                base_url=base_url,
                person_id=alex_person_id,
            )
            expect(page.get_by_role("status")).to_contain_text("名称已保存")
            expect(page.get_by_role("heading", name=alex_temporary_name)).to_be_visible()
            expect(page.get_by_label("人物名称")).to_have_value(alex_temporary_name)

            alex_after_named = _read_person_record(library_db, alex_person_id)
            assert alex_after_named["id"] == alex_person_id
            assert alex_after_named["display_name"] == alex_temporary_name
            assert alex_after_named["is_named"] is True
            assert _read_active_assignment_ids(library_db, alex_person_id) == alex_assignment_ids
            alex_events = _read_person_name_events(library_db, alex_person_id)
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

            _open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=alex_person_id)
            response_start = len(response_log)
            _submit_name_form(
                page,
                detail_url_pattern=alex_detail_pattern,
                display_name=alex_manifest_name,
            )
            _assert_name_prg_flow(
                responses=response_log[response_start:],
                base_url=base_url,
                person_id=alex_person_id,
            )
            expect(page.get_by_role("status")).to_contain_text("名称已更新")
            expect(page.get_by_role("heading", name=alex_manifest_name)).to_be_visible()
            expect(page.get_by_label("人物名称")).to_have_value(alex_manifest_name)

            alex_after_renamed = _read_person_record(library_db, alex_person_id)
            assert alex_after_renamed["id"] == alex_person_id
            assert alex_after_renamed["display_name"] == alex_manifest_name
            assert alex_after_renamed["is_named"] is True
            assert _read_active_assignment_ids(library_db, alex_person_id) == alex_assignment_ids
            alex_events = _read_person_name_events(library_db, alex_person_id)
            assert [event["event_type"] for event in alex_events] == ["person_named", "person_renamed"]
            assert alex_events[1]["old_display_name"] == alex_temporary_name
            assert alex_events[1]["new_display_name"] == alex_manifest_name
            assert alex_events[1]["created_at"]

            page.goto(f"{base_url}/people", wait_until="networkidle")
            expect(
                page.locator(f"[data-people-section='named'] [data-person-id='{alex_person_id}'] [data-person-label]")
            ).to_have_text(alex_manifest_name)
            expect(page.locator(f"[data-people-section='anonymous'] [data-person-id='{alex_person_id}']")).to_have_count(0)

            alex_before_duplicate_attempt = _read_person_record(library_db, alex_person_id)
            alex_assignments_before_duplicate_attempt = _read_active_assignment_ids(library_db, alex_person_id)
            _open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=blair_person_id)
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
            assert _read_person_record(library_db, blair_person_id) == blair_initial_record
            assert _read_active_assignment_ids(library_db, blair_person_id) == blair_assignment_ids
            assert _read_person_record(library_db, alex_person_id) == alex_before_duplicate_attempt
            assert _read_active_assignment_ids(library_db, alex_person_id) == alex_assignments_before_duplicate_attempt
            assert _read_person_name_events(library_db, blair_person_id) == []
            assert _count_person_name_events(library_db) == 2

            _open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=casey_person_id)
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
            assert _read_person_record(library_db, casey_person_id) == casey_initial_record
            assert _read_active_assignment_ids(library_db, casey_person_id) == casey_assignment_ids
            assert _read_person_name_events(library_db, casey_person_id) == []
            assert _count_person_name_events(library_db) == 2

            _open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=alex_person_id)
            alex_before_noop = _read_person_record(library_db, alex_person_id)
            response_start = len(response_log)
            _submit_name_form(
                page,
                detail_url_pattern=alex_detail_pattern,
                display_name=alex_manifest_name,
            )
            _assert_name_prg_flow(
                responses=response_log[response_start:],
                base_url=base_url,
                person_id=alex_person_id,
            )
            expect(page.get_by_role("status")).to_contain_text("名称未变化")
            assert _read_person_record(library_db, alex_person_id) == alex_before_noop
            assert _read_person_name_events(library_db, alex_person_id) == alex_events

            response_start = len(response_log)
            _submit_name_form(
                page,
                detail_url_pattern=alex_detail_pattern,
                display_name=noop_spaced_input,
            )
            _assert_name_prg_flow(
                responses=response_log[response_start:],
                base_url=base_url,
                person_id=alex_person_id,
            )
            expect(page.get_by_role("status")).to_contain_text("名称未变化")
            assert _read_person_record(library_db, alex_person_id) == alex_before_noop
            assert _read_person_name_events(library_db, alex_person_id) == alex_events
            assert _count_person_name_events(library_db) == 2

            page.goto(f"{base_url}/people", wait_until="networkidle")
            expect(
                page.locator(f"[data-people-section='named'] [data-person-id='{alex_person_id}'] [data-person-label]")
            ).to_have_text(alex_manifest_name)
            expect(page.locator(f"[data-people-section='anonymous'] [data-person-id='{alex_person_id}']")).to_have_count(0)
            expect(page.locator(f"[data-people-section='anonymous'] [data-person-id='{blair_person_id}']")).to_have_count(1)
            expect(page.locator(f"[data-people-section='anonymous'] [data-person-id='{casey_person_id}']")).to_have_count(1)

            missing_person_id = "00000000-0000-0000-0000-000000000000"
            db_snapshot_before_missing = _read_name_slice_db_snapshot(library_db)
            audit_count_before_missing = _count_person_name_events(library_db)
            missing_response = httpx.post(
                f"{base_url}/people/{missing_person_id}/name",
                data={"display_name": "Nobody"},
                follow_redirects=False,
                timeout=5.0,
            )
            assert missing_response.status_code == 404
            assert "未找到" in missing_response.text or "人物不存在" in missing_response.text
            assert _read_name_slice_db_snapshot(library_db) == db_snapshot_before_missing
            assert _count_person_name_events(library_db) == audit_count_before_missing

            browser.close()
    finally:
        _terminate_process(process)


def test_people_gallery_merge_via_real_serve_real_page_and_real_db(tmp_path: Path) -> None:
    workspace, _, library_db, manifest, target_person_ids = _create_scanned_workspace(tmp_path)
    incremental_manifest = _load_incremental_manifest()
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]
    blair_person_id = target_person_ids["target_blair"]
    winner_person_id = min(alex_person_id, casey_person_id)
    loser_person_id = casey_person_id if winner_person_id == alex_person_id else alex_person_id

    alex_assignment_ids_before_merge = _read_active_assignment_ids(library_db, alex_person_id)
    casey_assignment_ids_before_merge = _read_active_assignment_ids(library_db, casey_person_id)
    blair_assignment_ids_before_merge = _read_active_assignment_ids(library_db, blair_person_id)
    winner_assignment_ids_before_merge = _read_active_assignment_ids(library_db, winner_person_id)
    loser_assignment_ids_before_merge = _read_active_assignment_ids(library_db, loser_person_id)
    expected_union_assignment_ids = set(alex_assignment_ids_before_merge) | set(casey_assignment_ids_before_merge)
    assert len(alex_assignment_ids_before_merge) == len(casey_assignment_ids_before_merge)
    assert set(winner_assignment_ids_before_merge) | set(loser_assignment_ids_before_merge) == expected_union_assignment_ids
    active_people_before_merge = set(_read_active_people(library_db))

    port = _find_free_port()
    process = _spawn_hikbox(
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
        _wait_for_http_ready(f"{base_url}/")
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
            anonymous_order_before_merge = _people_section_person_ids_in_rendered_order(page, section="anonymous")
            assert alex_person_id in anonymous_order_before_merge
            assert casey_person_id in anonymous_order_before_merge

            response_start = len(response_log)
            request_start = len(request_log)
            _submit_merge_from_home(
                page,
                base_url=base_url,
                person_ids=[casey_person_id, alex_person_id],
            )
            _assert_merge_prg_flow(
                responses=response_log[response_start:],
                base_url=base_url,
            )
            posted_person_ids = _extract_merge_request_person_ids(
                requests=request_log[request_start:],
                base_url=base_url,
            )
            assert set(posted_person_ids) == {alex_person_id, casey_person_id}
            expect(page.get_by_role("status")).to_contain_text("人物已合并")

            home_cards_after_merge = _page_card_snapshot(page)
            assert winner_person_id in home_cards_after_merge
            assert loser_person_id not in home_cards_after_merge
            assert set(home_cards_after_merge) == active_people_before_merge - {loser_person_id}
            assert str(len(expected_union_assignment_ids)) in str(home_cards_after_merge[winner_person_id]["sample_count_text"])

            winner_record_after_merge = _read_person_record(library_db, winner_person_id)
            loser_record_after_merge = _read_person_record(library_db, loser_person_id)
            assert winner_record_after_merge["status"] == "active"
            assert loser_record_after_merge["status"] == "inactive"
            assert loser_record_after_merge["display_name"] is None
            assert loser_record_after_merge["is_named"] is False
            assert set(_read_active_assignment_ids(library_db, winner_person_id)) == expected_union_assignment_ids
            assert _read_active_assignment_ids(library_db, loser_person_id) == []

            page.goto(f"{base_url}/people/{winner_person_id}", wait_until="networkidle")
            rendered_assignment_ids = {
                assignment_id for assignment_id, _, _ in _iter_rendered_assignment_cards(page)
            }
            assert rendered_assignment_ids == expected_union_assignment_ids

            loser_detail_response = httpx.get(f"{base_url}/people/{loser_person_id}", timeout=5.0)
            assert loser_detail_response.status_code == 404
            assert "人物不存在" in loser_detail_response.text

            merge_operations = _read_person_merge_operations(library_db)
            assert len(merge_operations) == 1
            assert merge_operations[0]["winner_person_id"] == winner_person_id
            assert merge_operations[0]["loser_person_id"] == loser_person_id
            merge_assignment_rows = _read_merge_operation_assignment_rows(
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
        _terminate_process(process)

    add_second_source_result = _add_source(workspace, FIXTURE_DIR_2)
    assert add_second_source_result.returncode == 0, add_second_source_result.stderr
    incremental_scan_result = _run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "10",
    )
    assert incremental_scan_result.returncode == 0, incremental_scan_result.stderr

    assignment_rows_after_incremental = _asset_assignment_rows(library_db)
    for file_name in _manifest_files_for_target(incremental_manifest, "target_alex"):
        assert {person_id for _, person_id, _ in assignment_rows_after_incremental[file_name]} == {winner_person_id}
    for file_name in _manifest_files_for_target(incremental_manifest, "target_casey"):
        assert {person_id for _, person_id, _ in assignment_rows_after_incremental[file_name]} == {winner_person_id}
    for file_name in _manifest_files_for_target(incremental_manifest, "target_blair"):
        assert {person_id for _, person_id, _ in assignment_rows_after_incremental[file_name]} == {blair_person_id}

    expected_winner_count_after_incremental = len(expected_union_assignment_ids) + 10
    expected_blair_count_after_incremental = len(blair_assignment_ids_before_merge) + 5
    active_people_after_incremental = _read_active_people(library_db)
    assert set(active_people_after_incremental) == active_people_before_merge - {loser_person_id}
    assert int(active_people_after_incremental[winner_person_id]["sample_count"]) == expected_winner_count_after_incremental
    assert int(active_people_after_incremental[blair_person_id]["sample_count"]) == expected_blair_count_after_incremental
    assert _read_person_record(library_db, loser_person_id)["status"] == "inactive"

    port = _find_free_port()
    process = _spawn_hikbox(
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
        _wait_for_http_ready(f"{base_url}/")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(f"{base_url}/people", wait_until="networkidle")
            home_cards_after_incremental = _page_card_snapshot(page)
            assert loser_person_id not in home_cards_after_incremental
            assert str(expected_winner_count_after_incremental) in str(
                home_cards_after_incremental[winner_person_id]["sample_count_text"]
            )
            assert str(expected_blair_count_after_incremental) in str(
                home_cards_after_incremental[blair_person_id]["sample_count_text"]
            )
            browser.close()
    finally:
        _terminate_process(process)


def test_people_gallery_merge_prefers_sample_count_over_request_order(tmp_path: Path) -> None:
    workspace, _, library_db, manifest, target_person_ids = _create_scanned_workspace(tmp_path)
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]
    blair_person_id = target_person_ids["target_blair"]
    first_merge_winner_person_id = min(alex_person_id, casey_person_id)

    port = _find_free_port()
    process = _spawn_hikbox(
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
        _wait_for_http_ready(f"{base_url}/")
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

            _submit_merge_from_home(
                page,
                base_url=base_url,
                person_ids=[casey_person_id, alex_person_id],
            )
            merged_winner_assignment_ids = _read_active_assignment_ids(library_db, first_merge_winner_person_id)
            blair_assignment_ids_before_second_merge = _read_active_assignment_ids(library_db, blair_person_id)
            expected_assignment_ids_after_second_merge = set(merged_winner_assignment_ids) | set(
                blair_assignment_ids_before_second_merge
            )

            response_start = len(response_log)
            request_start = len(request_log)
            _submit_merge_from_home(
                page,
                base_url=base_url,
                person_ids=[blair_person_id, first_merge_winner_person_id],
            )
            _assert_merge_prg_flow(
                responses=response_log[response_start:],
                base_url=base_url,
            )
            posted_person_ids = _extract_merge_request_person_ids(
                requests=request_log[request_start:],
                base_url=base_url,
            )
            assert posted_person_ids == [blair_person_id, first_merge_winner_person_id]
            expect(page.get_by_role("status")).to_contain_text("人物已合并")

            home_cards_after_second_merge = _page_card_snapshot(page)
            assert first_merge_winner_person_id in home_cards_after_second_merge
            assert blair_person_id not in home_cards_after_second_merge
            assert str(len(expected_assignment_ids_after_second_merge)) in str(
                home_cards_after_second_merge[first_merge_winner_person_id]["sample_count_text"]
            )
            assert _read_person_record(library_db, first_merge_winner_person_id)["status"] == "active"
            assert _read_person_record(library_db, blair_person_id)["status"] == "inactive"
            assert set(_read_active_assignment_ids(library_db, first_merge_winner_person_id)) == (
                expected_assignment_ids_after_second_merge
            )
            assert _read_active_assignment_ids(library_db, blair_person_id) == []

            merge_operations = _read_person_merge_operations(library_db)
            assert len(merge_operations) == 2
            assert merge_operations[-1]["winner_person_id"] == first_merge_winner_person_id
            assert merge_operations[-1]["loser_person_id"] == blair_person_id

            loser_detail_response = httpx.get(f"{base_url}/people/{blair_person_id}", timeout=5.0)
            assert loser_detail_response.status_code == 404
            assert "人物不存在" in loser_detail_response.text
            browser.close()
    finally:
        _terminate_process(process)


def test_people_gallery_merge_prefers_named_person_over_anonymous_even_with_fewer_samples(tmp_path: Path) -> None:
    workspace, _, library_db, manifest, target_person_ids = _create_scanned_workspace(tmp_path)
    people_by_label = {str(person["label"]): person for person in manifest["people"]}
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]
    blair_person_id = target_person_ids["target_blair"]
    merged_anonymous_winner_person_id = min(alex_person_id, casey_person_id)
    blair_display_name = str(people_by_label["target_blair"]["display_name"])

    port = _find_free_port()
    process = _spawn_hikbox(
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
        _wait_for_http_ready(f"{base_url}/")
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

            _submit_merge_from_home(
                page,
                base_url=base_url,
                person_ids=[casey_person_id, alex_person_id],
            )
            merged_anonymous_assignment_ids = _read_active_assignment_ids(library_db, merged_anonymous_winner_person_id)
            blair_assignment_ids_before_merge = _read_active_assignment_ids(library_db, blair_person_id)

            blair_detail_pattern = re.compile(
                rf"{re.escape(base_url)}/people/{re.escape(blair_person_id)}(?:\\?.*)?$"
            )
            _open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=blair_person_id)
            response_start = len(response_log)
            _submit_name_form(
                page,
                detail_url_pattern=blair_detail_pattern,
                display_name=blair_display_name,
            )
            _assert_name_prg_flow(
                responses=response_log[response_start:],
                base_url=base_url,
                person_id=blair_person_id,
            )

            page.goto(f"{base_url}/people", wait_until="networkidle")
            response_start = len(response_log)
            _submit_merge_from_home(
                page,
                base_url=base_url,
                person_ids=[merged_anonymous_winner_person_id, blair_person_id],
            )
            _assert_merge_prg_flow(
                responses=response_log[response_start:],
                base_url=base_url,
            )
            expect(page.get_by_role("status")).to_contain_text("人物已合并")

            winner_record = _read_person_record(library_db, blair_person_id)
            loser_record = _read_person_record(library_db, merged_anonymous_winner_person_id)
            assert winner_record["status"] == "active"
            assert winner_record["is_named"] is True
            assert winner_record["display_name"] == blair_display_name
            assert loser_record["status"] == "inactive"
            assert set(_read_active_assignment_ids(library_db, blair_person_id)) == (
                set(blair_assignment_ids_before_merge) | set(merged_anonymous_assignment_ids)
            )
            assert _read_active_assignment_ids(library_db, merged_anonymous_winner_person_id) == []

            merge_operations = _read_person_merge_operations(library_db)
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
        _terminate_process(process)


def test_people_gallery_merge_rejects_two_named_people_via_real_home(tmp_path: Path) -> None:
    workspace, _, library_db, manifest, target_person_ids = _create_scanned_workspace(tmp_path)
    people_by_label = {str(person["label"]): person for person in manifest["people"]}
    alex_person_id = target_person_ids["target_alex"]
    blair_person_id = target_person_ids["target_blair"]
    alex_display_name = str(people_by_label["target_alex"]["display_name"])
    blair_display_name = str(people_by_label["target_blair"]["display_name"])

    port = _find_free_port()
    process = _spawn_hikbox(
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
        _wait_for_http_ready(f"{base_url}/")
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
            _open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=alex_person_id)
            _submit_name_form(
                page,
                detail_url_pattern=alex_detail_pattern,
                display_name=alex_display_name,
            )
            _open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=blair_person_id)
            _submit_name_form(
                page,
                detail_url_pattern=blair_detail_pattern,
                display_name=blair_display_name,
            )

            db_snapshot_before_merge_attempt = _read_name_slice_db_snapshot(library_db)
            page.goto(f"{base_url}/people", wait_until="networkidle")
            response_start = len(response_log)
            _submit_merge_from_home(
                page,
                base_url=base_url,
                person_ids=[alex_person_id, blair_person_id],
            )
            assert not any(
                response["method"] == "POST"
                and response["url"] == f"{base_url}/people/merge"
                and int(response["status"]) == 303
                for response in response_log[response_start:]
            )
            expect(page.get_by_role("alert")).to_contain_text("不支持合并两个已命名人物")
            assert _read_name_slice_db_snapshot(library_db) == db_snapshot_before_merge_attempt
            assert _count_person_name_events(library_db) == 2
            assert _read_person_merge_operations(library_db) == []
            browser.close()
    finally:
        _terminate_process(process)


def test_people_gallery_undo_restores_latest_merge_via_real_home_and_db(tmp_path: Path) -> None:
    workspace, _, library_db, _, target_person_ids = _create_scanned_workspace(tmp_path)
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]
    winner_person_id = min(alex_person_id, casey_person_id)
    loser_person_id = casey_person_id if winner_person_id == alex_person_id else alex_person_id
    people_before_merge = _read_active_people(library_db)
    winner_record_before_merge = _read_person_record(library_db, winner_person_id)
    loser_record_before_merge = _read_person_record(library_db, loser_person_id)
    winner_detail_before_merge = _read_active_assignment_details(library_db, winner_person_id)
    loser_detail_before_merge = _read_active_assignment_details(library_db, loser_person_id)
    assignment_owner_snapshot_before_merge = _read_assignment_owner_snapshot(library_db)

    port = _find_free_port()
    process = _spawn_hikbox(
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
        _wait_for_http_ready(f"{base_url}/")
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

            _submit_merge_from_home(
                page,
                base_url=base_url,
                person_ids=[casey_person_id, alex_person_id],
            )
            expect(page.locator("[data-undo-submit]")).to_be_enabled()

            response_start = len(response_log)
            _submit_undo_from_home(page, base_url=base_url)
            _assert_undo_prg_flow(
                responses=response_log[response_start:],
                base_url=base_url,
            )
            expect(page.get_by_role("status")).to_contain_text("最近一次合并已撤销")
            expect(page.locator("[data-undo-submit]")).to_be_disabled()

            assert _page_card_snapshot(page).keys() == people_before_merge.keys()
            winner_record_after_undo = _read_person_record(library_db, winner_person_id)
            loser_record_after_undo = _read_person_record(library_db, loser_person_id)
            assert winner_record_after_undo["id"] == winner_record_before_merge["id"]
            assert winner_record_after_undo["display_name"] == winner_record_before_merge["display_name"]
            assert winner_record_after_undo["is_named"] == winner_record_before_merge["is_named"]
            assert winner_record_after_undo["status"] == winner_record_before_merge["status"]
            assert loser_record_after_undo["id"] == loser_record_before_merge["id"]
            assert loser_record_after_undo["display_name"] == loser_record_before_merge["display_name"]
            assert loser_record_after_undo["is_named"] == loser_record_before_merge["is_named"]
            assert loser_record_after_undo["status"] == loser_record_before_merge["status"]
            assert _read_active_assignment_details(library_db, winner_person_id) == winner_detail_before_merge
            assert _read_active_assignment_details(library_db, loser_person_id) == loser_detail_before_merge
            assert _read_assignment_owner_snapshot(library_db) == assignment_owner_snapshot_before_merge
            merge_operations = _read_person_merge_operations(library_db)
            assert len(merge_operations) == 1
            assert merge_operations[0]["undone_at"] is not None
            browser.close()
    finally:
        _terminate_process(process)


def test_people_gallery_undo_is_disabled_without_merge_and_after_already_undone_merge(tmp_path: Path) -> None:
    workspace, _, library_db, _, target_person_ids = _create_scanned_workspace(tmp_path)
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]

    port = _find_free_port()
    process = _spawn_hikbox(
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
        _wait_for_http_ready(f"{base_url}/")
        page_snapshot_before_merge = _read_name_slice_db_snapshot(library_db)
        no_merge_response = httpx.post(
            f"{base_url}/people/merge/undo",
            follow_redirects=False,
            timeout=5.0,
        )
        assert no_merge_response.status_code == 400
        assert "当前没有可撤销的最近一次合并" in no_merge_response.text
        assert _read_name_slice_db_snapshot(library_db) == page_snapshot_before_merge

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(f"{base_url}/people", wait_until="networkidle")
            expect(page.locator("[data-undo-submit]")).to_be_disabled()

            _submit_merge_from_home(
                page,
                base_url=base_url,
                person_ids=[casey_person_id, alex_person_id],
            )
            _submit_undo_from_home(page, base_url=base_url)
            expect(page.locator("[data-undo-submit]")).to_be_disabled()
            browser.close()

        snapshot_after_undo = _read_name_slice_db_snapshot(library_db)
        already_undone_response = httpx.post(
            f"{base_url}/people/merge/undo",
            follow_redirects=False,
            timeout=5.0,
        )
        assert already_undone_response.status_code == 400
        assert "最近一次成功合并已经撤销" in already_undone_response.text
        assert _read_name_slice_db_snapshot(library_db) == snapshot_after_undo
    finally:
        _terminate_process(process)


def test_people_gallery_undo_remains_available_after_third_person_rename(tmp_path: Path) -> None:
    workspace, _, library_db, manifest, target_person_ids = _create_scanned_workspace(tmp_path)
    people_by_label = {str(person["label"]): person for person in manifest["people"]}
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]
    blair_person_id = target_person_ids["target_blair"]
    winner_person_id = min(alex_person_id, casey_person_id)
    loser_person_id = casey_person_id if winner_person_id == alex_person_id else alex_person_id
    winner_record_before_merge = _read_person_record(library_db, winner_person_id)
    loser_record_before_merge = _read_person_record(library_db, loser_person_id)
    renamed_blair = f"{people_by_label['target_blair']['display_name']} Undo 保留"

    port = _find_free_port()
    process = _spawn_hikbox(
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
        _wait_for_http_ready(f"{base_url}/")
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
            _submit_merge_from_home(
                page,
                base_url=base_url,
                person_ids=[casey_person_id, alex_person_id],
            )

            blair_detail_pattern = re.compile(
                rf"{re.escape(base_url)}/people/{re.escape(blair_person_id)}(?:\\?.*)?$"
            )
            _open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=blair_person_id)
            _submit_name_form(
                page,
                detail_url_pattern=blair_detail_pattern,
                display_name=renamed_blair,
            )

            page.goto(f"{base_url}/people", wait_until="networkidle")
            expect(page.locator("[data-undo-submit]")).to_be_enabled()
            response_start = len(response_log)
            _submit_undo_from_home(page, base_url=base_url)
            _assert_undo_prg_flow(
                responses=response_log[response_start:],
                base_url=base_url,
            )

            winner_record_after_undo = _read_person_record(library_db, winner_person_id)
            loser_record_after_undo = _read_person_record(library_db, loser_person_id)
            assert winner_record_after_undo["id"] == winner_record_before_merge["id"]
            assert winner_record_after_undo["display_name"] == winner_record_before_merge["display_name"]
            assert winner_record_after_undo["is_named"] == winner_record_before_merge["is_named"]
            assert winner_record_after_undo["status"] == winner_record_before_merge["status"]
            assert loser_record_after_undo["id"] == loser_record_before_merge["id"]
            assert loser_record_after_undo["display_name"] == loser_record_before_merge["display_name"]
            assert loser_record_after_undo["is_named"] == loser_record_before_merge["is_named"]
            assert loser_record_after_undo["status"] == loser_record_before_merge["status"]
            assert _read_person_record(library_db, blair_person_id)["display_name"] == renamed_blair
            browser.close()
    finally:
        _terminate_process(process)


def test_people_gallery_undo_rejects_after_incremental_assignment_write(tmp_path: Path) -> None:
    workspace, _, library_db, _, target_person_ids = _create_scanned_workspace(tmp_path)
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]
    winner_person_id = min(alex_person_id, casey_person_id)
    merged_snapshot = _read_name_slice_db_snapshot(library_db)

    port = _find_free_port()
    process = _spawn_hikbox(
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
        _wait_for_http_ready(f"{base_url}/")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            _submit_merge_from_home(
                page,
                base_url=base_url,
                person_ids=[casey_person_id, alex_person_id],
            )
            browser.close()

        add_result = _add_source(workspace, FIXTURE_DIR_2)
        assert add_result.returncode == 0
        scan_result = _run_hikbox(
            "scan",
            "start",
            "--workspace",
            str(workspace),
            "--batch-size",
            "10",
        )
        assert scan_result.returncode == 0, scan_result.stderr

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
        assert _read_person_record(library_db, winner_person_id)["status"] == "active"
        assert _read_person_merge_operations(library_db)[0]["undone_at"] is None
        assert _read_name_slice_db_snapshot(library_db) != merged_snapshot
    finally:
        _terminate_process(process)


def test_people_gallery_undo_rejects_real_winner_name_writes_but_keeps_noop_eligible(tmp_path: Path) -> None:
    def _run_anonymous_winner_named_then_rejected(case_tmp_path: Path) -> None:
        workspace, _, library_db, _, target_person_ids = _create_scanned_workspace(case_tmp_path)
        alex_person_id = target_person_ids["target_alex"]
        casey_person_id = target_person_ids["target_casey"]
        winner_person_id = min(alex_person_id, casey_person_id)
        port = _find_free_port()
        process = _spawn_hikbox(
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
            _wait_for_http_ready(f"{base_url}/")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                _submit_merge_from_home(
                    page,
                    base_url=base_url,
                    person_ids=[casey_person_id, alex_person_id],
                )
                winner_detail_pattern = re.compile(
                    rf"{re.escape(base_url)}/people/{re.escape(winner_person_id)}(?:\\?.*)?$"
                )
                _open_person_detail_from_home(
                    page,
                    base_url=base_url,
                    entry_path="/people",
                    person_id=winner_person_id,
                )
                _submit_name_form(
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
            _terminate_process(process)

    def _run_named_winner_renamed_then_rejected(case_tmp_path: Path) -> None:
        workspace, _, _, manifest, target_person_ids = _create_scanned_workspace(case_tmp_path)
        people_by_label = {str(person["label"]): person for person in manifest["people"]}
        alex_person_id = target_person_ids["target_alex"]
        casey_person_id = target_person_ids["target_casey"]
        blair_person_id = target_person_ids["target_blair"]
        merged_anonymous_winner_person_id = min(alex_person_id, casey_person_id)
        port = _find_free_port()
        process = _spawn_hikbox(
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
            _wait_for_http_ready(f"{base_url}/")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                _submit_merge_from_home(
                    page,
                    base_url=base_url,
                    person_ids=[casey_person_id, alex_person_id],
                )
                blair_detail_pattern = re.compile(
                    rf"{re.escape(base_url)}/people/{re.escape(blair_person_id)}(?:\\?.*)?$"
                )
                _open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=blair_person_id)
                _submit_name_form(
                    page,
                    detail_url_pattern=blair_detail_pattern,
                    display_name=str(people_by_label["target_blair"]["display_name"]),
                )
                _submit_merge_from_home(
                    page,
                    base_url=base_url,
                    person_ids=[merged_anonymous_winner_person_id, blair_person_id],
                )
                _open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=blair_person_id)
                _submit_name_form(
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
            _terminate_process(process)

    def _run_named_winner_noop_stays_eligible(case_tmp_path: Path) -> None:
        workspace, _, library_db, manifest, target_person_ids = _create_scanned_workspace(case_tmp_path)
        people_by_label = {str(person["label"]): person for person in manifest["people"]}
        alex_person_id = target_person_ids["target_alex"]
        casey_person_id = target_person_ids["target_casey"]
        blair_person_id = target_person_ids["target_blair"]
        merged_anonymous_winner_person_id = min(alex_person_id, casey_person_id)
        expected_blair_name = str(people_by_label["target_blair"]["display_name"])
        port = _find_free_port()
        process = _spawn_hikbox(
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
            _wait_for_http_ready(f"{base_url}/")
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
                _submit_merge_from_home(
                    page,
                    base_url=base_url,
                    person_ids=[casey_person_id, alex_person_id],
                )
                blair_detail_pattern = re.compile(
                    rf"{re.escape(base_url)}/people/{re.escape(blair_person_id)}(?:\\?.*)?$"
                )
                _open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=blair_person_id)
                _submit_name_form(
                    page,
                    detail_url_pattern=blair_detail_pattern,
                    display_name=expected_blair_name,
                )
                _submit_merge_from_home(
                    page,
                    base_url=base_url,
                    person_ids=[merged_anonymous_winner_person_id, blair_person_id],
                )
                name_event_count_before_noop = _count_person_name_events(library_db)
                _open_person_detail_from_home(page, base_url=base_url, entry_path="/people", person_id=blair_person_id)
                _submit_name_form(
                    page,
                    detail_url_pattern=blair_detail_pattern,
                    display_name=expected_blair_name,
                )
                assert _count_person_name_events(library_db) == name_event_count_before_noop
                page.goto(f"{base_url}/people", wait_until="networkidle")
                expect(page.locator("[data-undo-submit]")).to_be_enabled()
                response_start = len(response_log)
                _submit_undo_from_home(page, base_url=base_url)
                _assert_undo_prg_flow(
                    responses=response_log[response_start:],
                    base_url=base_url,
                )
                assert _read_person_merge_operations(library_db)[-1]["undone_at"] is not None
                browser.close()
        finally:
            _terminate_process(process)

    _run_anonymous_winner_named_then_rejected(tmp_path / "anonymous-winner-named")
    _run_named_winner_renamed_then_rejected(tmp_path / "named-winner-renamed")
    _run_named_winner_noop_stays_eligible(tmp_path / "named-winner-noop")


def test_people_gallery_undo_only_rolls_back_latest_merge(tmp_path: Path) -> None:
    workspace, _, library_db, _, target_person_ids = _create_scanned_workspace(tmp_path)
    alex_person_id = target_person_ids["target_alex"]
    casey_person_id = target_person_ids["target_casey"]
    blair_person_id = target_person_ids["target_blair"]
    first_merge_winner_person_id = min(alex_person_id, casey_person_id)
    first_merge_loser_person_id = casey_person_id if first_merge_winner_person_id == alex_person_id else alex_person_id

    port = _find_free_port()
    process = _spawn_hikbox(
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
        _wait_for_http_ready(f"{base_url}/")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            _submit_merge_from_home(
                page,
                base_url=base_url,
                person_ids=[casey_person_id, alex_person_id],
            )
            first_merge_assignment_ids = _read_active_assignment_ids(library_db, first_merge_winner_person_id)
            blair_assignment_ids_before_second_merge = _read_active_assignment_ids(library_db, blair_person_id)
            expected_first_merge_assignment_ids = set(first_merge_assignment_ids)
            expected_second_merge_union = expected_first_merge_assignment_ids | set(blair_assignment_ids_before_second_merge)

            _submit_merge_from_home(
                page,
                base_url=base_url,
                person_ids=[blair_person_id, first_merge_winner_person_id],
            )
            _submit_undo_from_home(page, base_url=base_url)
            expect(page.locator("[data-undo-submit]")).to_be_disabled()

            assert set(_read_active_assignment_ids(library_db, first_merge_winner_person_id)) == expected_first_merge_assignment_ids
            assert set(_read_active_assignment_ids(library_db, blair_person_id)) == set(blair_assignment_ids_before_second_merge)
            assert _read_active_assignment_ids(library_db, first_merge_loser_person_id) == []
            assert set(_read_active_assignment_ids(library_db, first_merge_winner_person_id)) != expected_second_merge_union
            merge_operations = _read_person_merge_operations(library_db)
            assert len(merge_operations) == 2
            assert merge_operations[0]["undone_at"] is None
            assert merge_operations[1]["undone_at"] is not None
            browser.close()

        snapshot_after_latest_undo = _read_name_slice_db_snapshot(library_db)
        response = httpx.post(
            f"{base_url}/people/merge/undo",
            follow_redirects=False,
            timeout=5.0,
        )
        assert response.status_code == 400
        assert "最近一次成功合并已经撤销" in response.text
        assert _read_name_slice_db_snapshot(library_db) == snapshot_after_latest_undo
    finally:
        _terminate_process(process)
