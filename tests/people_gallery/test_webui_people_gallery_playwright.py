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

import httpx
from playwright.sync_api import Page
from playwright.sync_api import expect
from playwright.sync_api import sync_playwright


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "people_gallery_scan"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"
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
        [sys.executable, "-m", "hikbox", *args],
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
        [sys.executable, "-m", "hikbox", *args],
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
        "--label",
        "fixture",
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
