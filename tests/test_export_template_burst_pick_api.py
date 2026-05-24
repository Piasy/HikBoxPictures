from __future__ import annotations

import math
import re
import sqlite3
import time
from pathlib import Path

import httpx
from PIL import ExifTags
from PIL import Image
from PIL import ImageEnhance
from PIL import ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:
    pass

from tests.helpers import (
    FIXTURE_DIR,
    add_source,
    create_template_via_api,
    fetch_all,
    find_free_port,
    name_person_via_api,
    run_hikbox,
    spawn_hikbox,
    terminate_process,
    wait_for_http_ready,
)


_EXIF_ID_BY_NAME = {name: tag_id for tag_id, name in ExifTags.TAGS.items()}


def _name_required_people(base_url: str, target_person_ids: dict[str, str]) -> None:
    for key, display_name in {
        "target_alex": "Alex Chen",
        "target_blair": "Blair Lin",
        "target_casey": "Casey Wu",
    }.items():
        response = name_person_via_api(base_url, target_person_ids[key], display_name)
        assert response.status_code in (302, 303)


def _create_alex_blair_template(
    base_url: str,
    tmp_path: Path,
    target_person_ids: dict[str, str],
) -> str:
    result = create_template_via_api(
        base_url,
        name="Alex & Blair",
        person_ids=[target_person_ids["target_alex"], target_person_ids["target_blair"]],
        output_root=str(tmp_path / "export-output-alex-blair"),
    )
    return str(result["template_id"])


def _create_alex_casey_template(
    base_url: str,
    tmp_path: Path,
    target_person_ids: dict[str, str],
) -> str:
    result = create_template_via_api(
        base_url,
        name="Alex & Casey",
        person_ids=[target_person_ids["target_alex"], target_person_ids["target_casey"]],
        output_root=str(tmp_path / "export-output-alex-casey"),
    )
    return str(result["template_id"])


def _asset_id_by_file(library_db: Path, file_name: str) -> int:
    rows = fetch_all(library_db, "SELECT id FROM assets WHERE file_name = ?", (file_name,))
    assert rows, file_name
    return int(rows[0][0])


def _asset_path(library_db: Path, asset_id: int) -> Path:
    rows = fetch_all(library_db, "SELECT absolute_path FROM assets WHERE id = ?", (asset_id,))
    assert rows, asset_id
    return Path(str(rows[0][0]))


def _preview_asset_ids(preview_json: dict[str, object]) -> set[int]:
    asset_ids: set[int] = set()
    for month in preview_json["months"]:
        for bucket in ("only", "group"):
            for asset in month[bucket]:
                asset_ids.add(int(asset["asset_id"]))
    return asset_ids


def _burst_asset_ids(groups: list[dict[str, object]]) -> set[int]:
    return {
        int(asset["asset_id"])
        for group in groups
        for asset in group["assets"]
    }


def _find_group_containing(
    groups: list[dict[str, object]],
    required_asset_ids: set[int],
) -> dict[str, object]:
    for group in groups:
        member_ids = {int(asset["asset_id"]) for asset in group["assets"]}
        if required_asset_ids.issubset(member_ids):
            return group
    raise AssertionError(f"未找到包含 {sorted(required_asset_ids)} 的相似组")


def _find_edge(
    group: dict[str, object],
    asset_a: int,
    asset_b: int,
) -> dict[str, object]:
    expected = sorted([asset_a, asset_b])
    for edge in group["match_evidence"]["edges"]:
        if edge["asset_ids"] == expected:
            return edge
    raise AssertionError(f"未找到 edge: {expected}")


def _direct_edge_or_none(
    group: dict[str, object],
    asset_a: int,
    asset_b: int,
) -> dict[str, object] | None:
    expected = sorted([asset_a, asset_b])
    for edge in group["match_evidence"]["edges"]:
        if edge["asset_ids"] == expected:
            return edge
    return None


def _reference_fingerprint(path: Path) -> dict[str, object]:
    image = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    luminance = image.convert("L")

    horizontal = luminance.resize((9, 8), Image.Resampling.BILINEAR)
    horizontal_pixels = _image_pixels(horizontal)
    dhash_bits: list[int] = []
    for y in range(8):
        row = y * 9
        for x in range(8):
            dhash_bits.append(1 if horizontal_pixels[row + x] > horizontal_pixels[row + x + 1] else 0)

    vertical = luminance.resize((8, 9), Image.Resampling.BILINEAR)
    vertical_pixels = _image_pixels(vertical)
    for y in range(8):
        for x in range(8):
            dhash_bits.append(1 if vertical_pixels[y * 8 + x] > vertical_pixels[(y + 1) * 8 + x] else 0)

    thumbnail_pixels = _image_pixels(luminance.resize((16, 16), Image.Resampling.BILINEAR))
    mean_value = sum(thumbnail_pixels) / len(thumbnail_pixels)
    centered = [float(value) - mean_value for value in thumbnail_pixels]
    norm = math.sqrt(sum(value * value for value in centered))
    luminance_vector = [0.0 for _ in centered] if norm == 0 else [value / norm for value in centered]

    histogram = [0 for _ in range(64)]
    for red, green, blue in _image_pixels(image):
        histogram[(red // 64) * 16 + (green // 64) * 4 + (blue // 64)] += 1
    total_pixels = sum(histogram)
    color_histogram = [value / total_pixels for value in histogram]

    return {
        "dhash_bits": dhash_bits,
        "luminance_vector": luminance_vector,
        "color_histogram": color_histogram,
    }


def _image_pixels(image: Image.Image) -> list[object]:
    if hasattr(image, "get_flattened_data"):
        return list(image.get_flattened_data())
    return list(image.getdata())


def _reference_metrics(path_a: Path, path_b: Path) -> dict[str, float | int]:
    first = _reference_fingerprint(path_a)
    second = _reference_fingerprint(path_b)
    first_vec = first["luminance_vector"]
    second_vec = second["luminance_vector"]
    first_zero = all(value == 0 for value in first_vec)
    second_zero = all(value == 0 for value in second_vec)
    if first_zero and second_zero:
        cosine = 1.0
    elif first_zero or second_zero:
        cosine = 0.0
    else:
        cosine = sum(a * b for a, b in zip(first_vec, second_vec))
    return {
        "dhash_hamming": sum(
            a != b for a, b in zip(first["dhash_bits"], second["dhash_bits"])
        ),
        "luminance_cosine": cosine,
        "color_histogram_intersection": sum(
            min(a, b) for a, b in zip(first["color_histogram"], second["color_histogram"])
        ),
    }


def _assert_edge_matches_reference(
    library_db: Path,
    edge: dict[str, object],
) -> None:
    asset_a, asset_b = [int(value) for value in edge["asset_ids"]]
    expected = _reference_metrics(_asset_path(library_db, asset_a), _asset_path(library_db, asset_b))

    assert int(edge["dhash_hamming"]) == expected["dhash_hamming"]
    assert float(edge["luminance_cosine"]) == expected["luminance_cosine"]
    assert abs(float(edge["color_histogram_intersection"]) - float(expected["color_histogram_intersection"])) <= 1e-6

    threshold = str(edge["threshold"])
    if threshold == "strict":
        assert expected["dhash_hamming"] <= 10
        assert expected["color_histogram_intersection"] >= 0.88
    elif threshold == "resave_or_light_edit":
        assert expected["dhash_hamming"] <= 18
        assert expected["luminance_cosine"] >= 0.96
        assert expected["color_histogram_intersection"] >= 0.80
    elif threshold == "metadata_assisted":
        legacy_metadata_match = (
            expected["dhash_hamming"] <= 24
            and expected["luminance_cosine"] >= 0.94
            and expected["color_histogram_intersection"] >= 0.72
        )
        short_interval_burst_match = (
            expected["dhash_hamming"] <= 18
            and expected["luminance_cosine"] >= 0.88
            and expected["color_histogram_intersection"] >= 0.90
            and edge["capture_time_delta_seconds"] is not None
            and float(edge["capture_time_delta_seconds"]) <= 2
        )
        same_second_burst_match = (
            expected["dhash_hamming"] <= 20
            and expected["luminance_cosine"] >= 0.92
            and expected["color_histogram_intersection"] >= 0.94
            and edge["capture_time_delta_seconds"] is not None
            and float(edge["capture_time_delta_seconds"]) <= 1
        )
        assert legacy_metadata_match or short_interval_burst_match or same_second_burst_match
        assert edge["metadata_assisted"] is True
    else:
        raise AssertionError(f"未知 threshold: {threshold}")


def _assert_not_same_visual_burst(path_a: Path, path_b: Path) -> None:
    metrics = _reference_metrics(path_a, path_b)
    assert not (
        metrics["dhash_hamming"] <= 10
        and metrics["color_histogram_intersection"] >= 0.88
    )
    assert not (
        metrics["dhash_hamming"] <= 18
        and metrics["luminance_cosine"] >= 0.96
        and metrics["color_histogram_intersection"] >= 0.80
    )
    assert not (
        metrics["dhash_hamming"] <= 24
        and metrics["luminance_cosine"] >= 0.94
        and metrics["color_histogram_intersection"] >= 0.72
    )


def _save_reencoded_variant(
    source_path: Path,
    target_path: Path,
    *,
    brightness: float = 1.0,
    fake_metadata: bool = False,
) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    image = ImageOps.exif_transpose(Image.open(source_path)).convert("RGB")
    if brightness != 1.0:
        image = ImageEnhance.Brightness(image).enhance(brightness)
    save_kwargs: dict[str, object] = {"quality": 92}
    if fake_metadata:
        exif = Image.Exif()
        exif[_EXIF_ID_BY_NAME["Make"]] = "Different Test Make"
        exif[_EXIF_ID_BY_NAME["Model"]] = "Different Test Model"
        exif[_EXIF_ID_BY_NAME["DateTimeOriginal"]] = "1999:01:02 03:04:05"
        save_kwargs["exif"] = exif
    image.save(target_path, "JPEG", **save_kwargs)


def _assert_no_capture_or_device_exif(path: Path) -> None:
    exif = Image.open(path).getexif()
    for tag_name in ("Make", "Model", "DateTimeOriginal", "DateTimeDigitized", "DateTime"):
        tag_id = _EXIF_ID_BY_NAME.get(tag_name)
        if tag_id is not None:
            assert not exif.get(tag_id)


def _scan_incremental_sources(workspace: Path, source_dirs: list[Path]) -> None:
    for source_dir in source_dirs:
        add_result = add_source(workspace, source_dir)
        assert add_result.returncode == 0, add_result.stderr
    scan_result = run_hikbox(
        "scan",
        "start",
        "--workspace",
        str(workspace),
        "--batch-size",
        "4",
        timeout=180,
    )
    assert scan_result.returncode == 0, scan_result.stderr


def _post_keep_first_asset_per_group(
    base_url: str,
    template_id: str,
    groups: list[dict[str, object]],
) -> tuple[dict[str, object], set[int], set[int]]:
    kept_asset_ids: set[int] = set()
    abandoned_asset_ids: set[int] = set()
    aggregate = {
        "abandoned_asset_ids": [],
        "kept_asset_ids": [],
        "created_count": 0,
        "already_abandoned_count": 0,
    }
    for group in groups:
        member_ids = [int(asset["asset_id"]) for asset in group["assets"]]
        keep_asset_id = member_ids[0]
        kept_asset_ids.add(keep_asset_id)
        abandoned_asset_ids.update(member_ids[1:])
        response = httpx.post(
            f"{base_url}/api/export-templates/{template_id}/burst-pick",
            json={
                "group_key": group["group_key"],
                "keep_asset_ids": [keep_asset_id],
            },
            timeout=30.0,
        )
        assert response.status_code == 200, response.text
        result = response.json()
        aggregate["abandoned_asset_ids"].extend(result["abandoned_asset_ids"])
        aggregate["kept_asset_ids"].extend(result["kept_asset_ids"])
        aggregate["created_count"] += int(result["created_count"])
        aggregate["already_abandoned_count"] += int(result["already_abandoned_count"])

    aggregate["abandoned_asset_ids"] = sorted(aggregate["abandoned_asset_ids"])
    aggregate["kept_asset_ids"] = sorted(aggregate["kept_asset_ids"])
    return aggregate, kept_asset_ids, abandoned_asset_ids


def _planned_target_paths(
    library_db: Path,
    *,
    template_id: str,
    output_root: Path,
    asset_ids: set[int],
) -> dict[int, Path]:
    if not asset_ids:
        return {}
    placeholders = ", ".join("?" for _ in asset_ids)
    rows = fetch_all(
        library_db,
        f"""
        SELECT asset_id, bucket, month, file_name
        FROM export_plan
        WHERE template_id = ?
          AND asset_id IN ({placeholders})
        ORDER BY asset_id
        """,
        (template_id, *sorted(asset_ids)),
    )
    return {
        int(asset_id): output_root / str(bucket) / str(month) / str(file_name)
        for asset_id, bucket, month, file_name in rows
    }


def _await_burst_pick_completed(
    base_url: str,
    template_id: str,
    *,
    timeout_seconds: float = 30.0,
) -> dict[str, object]:
    deadline = time.time() + timeout_seconds
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
        if data["status"] == "failed":
            raise AssertionError(data.get("error_message") or data)
        time.sleep(0.1)
    raise AssertionError(f"等待连拍挑选后台处理完成超时: {last_data}")


def _await_burst_pick_failed(
    base_url: str,
    template_id: str,
    *,
    timeout_seconds: float = 10.0,
) -> dict[str, object]:
    deadline = time.time() + timeout_seconds
    last_data: dict[str, object] | None = None
    while time.time() < deadline:
        response = httpx.get(
            f"{base_url}/api/export-templates/{template_id}/burst-pick",
            timeout=5.0,
        )
        assert response.status_code == 200, response.text
        data = response.json()
        last_data = data
        if data["status"] == "failed":
            return data
        time.sleep(0.1)
    raise AssertionError(f"等待连拍挑选后台处理失败状态超时: {last_data}")


class TestExportTemplateBurstPickApi:
    def test_get_starts_async_run_and_persists_completed_groups(
        self,
        scanned_workspace,
        tmp_path: Path,
    ) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
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
        try:
            wait_for_http_ready(f"{base_url}/")
            _name_required_people(base_url, target_person_ids)
            template_id = _create_alex_blair_template(base_url, tmp_path, target_person_ids)

            response = httpx.get(
                f"{base_url}/api/export-templates/{template_id}/burst-pick",
                timeout=5.0,
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "running"
            assert data["groups"] == []
            assert data["progress"]["total_candidate_count"] > 0
            assert data["progress"]["processed_candidate_count"] < data["progress"]["total_candidate_count"]

            run_rows = fetch_all(
                library_db,
                """
                SELECT id, status, total_candidate_count, processed_candidate_count
                FROM export_burst_pick_run
                WHERE template_id = ?
                ORDER BY id DESC
                """,
                (template_id,),
            )
            assert len(run_rows) == 1
            run_id = int(run_rows[0][0])
            assert str(run_rows[0][1]) == "running"

            completed = _await_burst_pick_completed(base_url, template_id, timeout_seconds=30.0)
            assert completed["status"] == "completed"
            assert completed["run_id"] == run_id
            assert completed["groups"]
            assert completed["diagnostics"]["skipped_missing_or_unreadable_count"] == 0

            persisted_counts = fetch_all(
                library_db,
                """
                SELECT
                  (SELECT COUNT(*) FROM export_burst_pick_group WHERE run_id = ?),
                  (SELECT COUNT(*) FROM export_burst_pick_group_asset WHERE group_id IN (
                    SELECT id FROM export_burst_pick_group WHERE run_id = ?
                  )),
                  (SELECT COUNT(*) FROM export_burst_pick_group_edge WHERE group_id IN (
                    SELECT id FROM export_burst_pick_group WHERE run_id = ?
                  ))
                """,
                (run_id, run_id, run_id),
            )[0]
            assert int(persisted_counts[0]) == len(completed["groups"])
            assert int(persisted_counts[1]) == sum(len(group["assets"]) for group in completed["groups"])
            assert int(persisted_counts[2]) == sum(
                len(group["match_evidence"]["edges"]) for group in completed["groups"]
            )
        finally:
            terminate_process(process)

    def test_incremental_reencoded_images_without_reliable_metadata_group_by_content(
        self,
        scanned_workspace,
        tmp_path: Path,
    ) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        source_dir = tmp_path / "ac2-metadata-variant-source"
        original_fixture = FIXTURE_DIR / "pg_031_group_alex_blair_01.jpg"
        no_exif_path = source_dir / "ac2_no_exif_reencoded_9001.jpg"
        fake_metadata_path = source_dir / "ac2_fake_device_light_9002.jpg"
        _save_reencoded_variant(original_fixture, no_exif_path)
        _save_reencoded_variant(original_fixture, fake_metadata_path, brightness=1.02, fake_metadata=True)
        _assert_no_capture_or_device_exif(no_exif_path)
        fake_exif = Image.open(fake_metadata_path).getexif()
        assert fake_exif[_EXIF_ID_BY_NAME["Make"]] == "Different Test Make"
        assert fake_exif[_EXIF_ID_BY_NAME["DateTimeOriginal"]] == "1999:01:02 03:04:05"

        _scan_incremental_sources(workspace, [source_dir])

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        try:
            wait_for_http_ready(f"{base_url}/")
            _name_required_people(base_url, target_person_ids)
            template_id = _create_alex_blair_template(base_url, tmp_path, target_person_ids)

            original_asset_id = _asset_id_by_file(library_db, "pg_031_group_alex_blair_01.jpg")
            no_exif_asset_id = _asset_id_by_file(library_db, no_exif_path.name)
            fake_metadata_asset_id = _asset_id_by_file(library_db, fake_metadata_path.name)
            preview = httpx.get(
                f"{base_url}/api/export-templates/{template_id}/preview",
                timeout=30.0,
            ).json()
            assert {original_asset_id, no_exif_asset_id, fake_metadata_asset_id}.issubset(
                _preview_asset_ids(preview)
            )

            groups = _await_burst_pick_completed(base_url, template_id)["groups"]
            group = _find_group_containing(
                groups,
                {original_asset_id, no_exif_asset_id, fake_metadata_asset_id},
            )
            assert group["match_evidence"]["algorithm"] == "visual_fingerprint_v1"

            for new_asset_id in (no_exif_asset_id, fake_metadata_asset_id):
                edge = _find_edge(group, original_asset_id, new_asset_id)
                assert edge["metadata_assisted"] is False
                assert edge["threshold"] in {"strict", "resave_or_light_edit"}
                _assert_edge_matches_reference(library_db, edge)
        finally:
            terminate_process(process)

    def test_filename_adjacency_is_ignored_and_non_adjacent_content_match_groups(
        self,
        scanned_workspace,
        tmp_path: Path,
    ) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        adjacent_source_dir = tmp_path / "ac4-adjacent-source"
        positive_source_a = tmp_path / "ac4-positive-left-source"
        positive_source_b = tmp_path / "ac4-positive-right-source"
        alex_blair_fixture = FIXTURE_DIR / "pg_031_group_alex_blair_01.jpg"
        all_targets_fixture = FIXTURE_DIR / "pg_037_group_all_targets_07.jpg"

        adjacent_first = adjacent_source_dir / "ac4_adjacent_0001.jpg"
        adjacent_second = adjacent_source_dir / "ac4_adjacent_0002.jpg"
        positive_first = positive_source_a / "ac4_positive_left_1101.jpg"
        positive_second = positive_source_b / "ac4_positive_right_9909.jpg"
        _save_reencoded_variant(alex_blair_fixture, adjacent_first)
        _save_reencoded_variant(all_targets_fixture, adjacent_second)
        _save_reencoded_variant(alex_blair_fixture, positive_first, brightness=1.01)
        _save_reencoded_variant(alex_blair_fixture, positive_second, brightness=0.99)

        _scan_incremental_sources(
            workspace,
            [adjacent_source_dir, positive_source_a, positive_source_b],
        )

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        try:
            wait_for_http_ready(f"{base_url}/")
            _name_required_people(base_url, target_person_ids)
            template_id = _create_alex_blair_template(base_url, tmp_path, target_person_ids)

            adjacent_first_id = _asset_id_by_file(library_db, adjacent_first.name)
            adjacent_second_id = _asset_id_by_file(library_db, adjacent_second.name)
            positive_first_id = _asset_id_by_file(library_db, positive_first.name)
            positive_second_id = _asset_id_by_file(library_db, positive_second.name)
            preview = httpx.get(
                f"{base_url}/api/export-templates/{template_id}/preview",
                timeout=30.0,
            ).json()
            assert {
                adjacent_first_id,
                adjacent_second_id,
                positive_first_id,
                positive_second_id,
            }.issubset(_preview_asset_ids(preview))

            groups = _await_burst_pick_completed(base_url, template_id)["groups"]

            assert not any(
                {adjacent_first_id, adjacent_second_id}.issubset(
                    {int(asset["asset_id"]) for asset in group["assets"]}
                )
                for group in groups
            )
            _assert_not_same_visual_burst(
                _asset_path(library_db, adjacent_first_id),
                _asset_path(library_db, adjacent_second_id),
            )

            positive_group = _find_group_containing(groups, {positive_first_id, positive_second_id})
            edge = _find_edge(positive_group, positive_first_id, positive_second_id)
            assert edge["threshold"] in {"strict", "resave_or_light_edit", "metadata_assisted"}
            _assert_edge_matches_reference(library_db, edge)
        finally:
            terminate_process(process)

    def test_get_returns_visual_groups_with_reference_metrics_and_stable_schema(
        self,
        scanned_workspace,
        tmp_path: Path,
    ) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        try:
            wait_for_http_ready(f"{base_url}/")
            _name_required_people(base_url, target_person_ids)
            template_id = _create_alex_blair_template(base_url, tmp_path, target_person_ids)

            preview = httpx.get(
                f"{base_url}/api/export-templates/{template_id}/preview",
                timeout=30.0,
            ).json()
            data = _await_burst_pick_completed(base_url, template_id)
            assert data["template_id"] == template_id
            assert data["diagnostics"]["skipped_missing_or_unreadable_count"] == 0
            groups = data["groups"]
            assert groups
            assert [min(int(asset["asset_id"]) for asset in group["assets"]) for group in groups] == sorted(
                min(int(asset["asset_id"]) for asset in group["assets"]) for group in groups
            )

            displayed_asset_ids = _burst_asset_ids(groups)
            assert displayed_asset_ids.issubset(_preview_asset_ids(preview))

            first_asset_id = _asset_id_by_file(library_db, "pg_031_group_alex_blair_01.jpg")
            second_asset_id = _asset_id_by_file(library_db, "pg_032_group_alex_blair_02.jpg")
            group = _find_group_containing(groups, {first_asset_id, second_asset_id})
            assert re.fullmatch(r"[A-Za-z0-9_-]+", group["group_key"])
            assert [int(asset["asset_id"]) for asset in group["assets"]] == sorted(
                int(asset["asset_id"]) for asset in group["assets"]
            )
            assert group["match_evidence"]["algorithm"] == "visual_fingerprint_v1"

            edge = _find_edge(group, first_asset_id, second_asset_id)
            assert set(edge) == {
                "asset_ids",
                "threshold",
                "metadata_assisted",
                "dhash_hamming",
                "luminance_cosine",
                "color_histogram_intersection",
                "capture_time_delta_seconds",
                "normalized_device_match",
            }
            assert edge["asset_ids"] == [first_asset_id, second_asset_id]
            _assert_edge_matches_reference(library_db, edge)

            for group in groups:
                for asset in group["assets"]:
                    assert set(asset) >= {
                        "asset_id",
                        "file_name",
                        "bucket",
                        "month",
                        "context_url",
                        "original_url",
                        "is_live",
                    }
                    assert str(asset["original_url"]) == f"/images/assets/{asset['asset_id']}/original"
                edge_pairs = [tuple(edge["asset_ids"]) for edge in group["match_evidence"]["edges"]]
                assert edge_pairs == sorted(edge_pairs)
        finally:
            terminate_process(process)

    def test_post_marks_abandoned_assets_and_preview_execute_filter_old_plan(
        self,
        scanned_workspace,
        tmp_path: Path,
    ) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        output_root = tmp_path / "export-output-alex-blair"
        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        try:
            wait_for_http_ready(f"{base_url}/")
            _name_required_people(base_url, target_person_ids)
            template_id = _create_alex_blair_template(base_url, tmp_path, target_person_ids)
            other_template_id = _create_alex_casey_template(base_url, tmp_path, target_person_ids)

            preview_before = httpx.get(
                f"{base_url}/api/export-templates/{template_id}/preview",
                timeout=30.0,
            ).json()
            assert fetch_all(library_db, "SELECT COUNT(*) FROM export_plan WHERE template_id = ?", (template_id,))[0][0] > 0

            groups = _await_burst_pick_completed(base_url, template_id)["groups"]
            abandoned_before_submit: set[int] = set()
            for group in groups:
                member_ids = [int(asset["asset_id"]) for asset in group["assets"]]
                abandoned_before_submit.update(member_ids[1:])
            planned_abandoned_paths = _planned_target_paths(
                library_db,
                template_id=template_id,
                output_root=output_root,
                asset_ids=abandoned_before_submit,
            )
            assert set(planned_abandoned_paths) == abandoned_before_submit

            submit_result, kept_asset_ids, abandoned_asset_ids = _post_keep_first_asset_per_group(
                base_url,
                template_id,
                groups,
            )
            assert abandoned_asset_ids == abandoned_before_submit

            assert set(submit_result["kept_asset_ids"]) == kept_asset_ids
            assert set(submit_result["abandoned_asset_ids"]) == abandoned_asset_ids
            assert submit_result["created_count"] == len(abandoned_asset_ids)
            assert submit_result["already_abandoned_count"] == 0

            marker_rows = fetch_all(
                library_db,
                """
                SELECT asset_id, triggered_template_id, group_key
                FROM export_abandoned_asset
                ORDER BY asset_id
                """,
            )
            assert {int(row[0]) for row in marker_rows} == abandoned_asset_ids
            assert {str(row[1]) for row in marker_rows} == {template_id}
            assert all(re.fullmatch(r"[A-Za-z0-9_-]+", str(row[2])) for row in marker_rows)

            preview_after = httpx.get(
                f"{base_url}/api/export-templates/{template_id}/preview",
                timeout=30.0,
            ).json()
            assert _preview_asset_ids(preview_after).isdisjoint(abandoned_asset_ids)
            assert kept_asset_ids.intersection(_preview_asset_ids(preview_after))
            assert preview_after["total_count"] == preview_before["total_count"] - len(
                abandoned_asset_ids.intersection(_preview_asset_ids(preview_before))
            )

            other_preview = httpx.get(
                f"{base_url}/api/export-templates/{other_template_id}/preview",
                timeout=30.0,
            ).json()
            assert _preview_asset_ids(other_preview).isdisjoint(abandoned_asset_ids)

            execute_response = httpx.post(
                f"{base_url}/api/export-templates/{template_id}/execute",
                timeout=30.0,
            )
            assert execute_response.status_code == 200, execute_response.text
            run_id = int(execute_response.json()["run_id"])
            delivered_asset_ids = {
                int(row[0])
                for row in fetch_all(
                    library_db,
                    "SELECT asset_id FROM export_delivery WHERE run_id = ?",
                    (run_id,),
                )
            }
            assert delivered_asset_ids.isdisjoint(abandoned_asset_ids)
            delivered_target_paths = {
                Path(str(row[0]))
                for row in fetch_all(
                    library_db,
                    "SELECT target_path FROM export_delivery WHERE run_id = ?",
                    (run_id,),
                )
            }
            assert delivered_target_paths.isdisjoint(set(planned_abandoned_paths.values()))
            for target_path in planned_abandoned_paths.values():
                assert not target_path.exists(), str(target_path)
        finally:
            terminate_process(process)

    def test_post_resolves_only_one_group_and_keeps_other_groups_pending(
        self,
        scanned_workspace,
        tmp_path: Path,
    ) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        try:
            wait_for_http_ready(f"{base_url}/")
            _name_required_people(base_url, target_person_ids)
            template_id = _create_alex_blair_template(base_url, tmp_path, target_person_ids)
            groups = _await_burst_pick_completed(base_url, template_id)["groups"]
            assert len(groups) >= 2
            first_group = groups[0]
            second_group = groups[1]
            first_member_ids = [int(asset["asset_id"]) for asset in first_group["assets"]]

            response = httpx.post(
                f"{base_url}/api/export-templates/{template_id}/burst-pick",
                json={
                    "group_key": first_group["group_key"],
                    "keep_asset_ids": [first_member_ids[0]],
                },
                timeout=30.0,
            )

            assert response.status_code == 200, response.text
            assert set(response.json()["abandoned_asset_ids"]) == set(first_member_ids[1:])
            marker_rows = fetch_all(
                library_db,
                "SELECT asset_id FROM export_abandoned_asset ORDER BY asset_id",
            )
            assert {int(row[0]) for row in marker_rows} == set(first_member_ids[1:])

            remaining = httpx.get(
                f"{base_url}/api/export-templates/{template_id}/burst-pick",
                timeout=30.0,
            ).json()
            remaining_keys = {str(group["group_key"]) for group in remaining["groups"]}
            assert str(first_group["group_key"]) not in remaining_keys
            assert str(second_group["group_key"]) in remaining_keys
        finally:
            terminate_process(process)

    def test_post_rejects_missing_empty_and_stale_groups_without_db_changes(
        self,
        scanned_workspace,
        tmp_path: Path,
    ) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        try:
            wait_for_http_ready(f"{base_url}/")
            _name_required_people(base_url, target_person_ids)
            template_id = _create_alex_blair_template(base_url, tmp_path, target_person_ids)
            groups = _await_burst_pick_completed(base_url, template_id)["groups"]
            assert len(groups) >= 2

            invalid_payloads = [
                {"groups": [123]},
                {"groups": []},
                {
                    "groups": [
                        {
                            "group_key": groups[0]["group_key"],
                            "keep_asset_ids": [groups[0]["assets"][0]["asset_id"]],
                        },
                        {
                            "group_key": groups[1]["group_key"],
                            "keep_asset_ids": [groups[1]["assets"][0]["asset_id"]],
                        },
                    ]
                },
                {
                    "group_key": groups[0]["group_key"],
                    "keep_asset_ids": [],
                },
                {
                    "group_key": groups[0]["group_key"],
                    "keep_asset_ids": [groups[1]["assets"][0]["asset_id"]],
                },
                {
                    "group_key": f"{groups[0]['group_key']}_stale",
                    "keep_asset_ids": [groups[0]["assets"][0]["asset_id"]],
                },
            ]

            for payload in invalid_payloads:
                before = fetch_all(library_db, "SELECT COUNT(*) FROM export_abandoned_asset")[0][0]
                plan_before = fetch_all(library_db, "SELECT COUNT(*) FROM export_plan")[0][0]
                response = httpx.post(
                    f"{base_url}/api/export-templates/{template_id}/burst-pick",
                    json=payload,
                    timeout=30.0,
                )
                assert response.status_code == 400
                assert response.json()["detail"]
                assert fetch_all(library_db, "SELECT COUNT(*) FROM export_abandoned_asset")[0][0] == before
                assert fetch_all(library_db, "SELECT COUNT(*) FROM export_plan")[0][0] == plan_before
        finally:
            terminate_process(process)

    def test_api_post_rejects_while_export_is_running_without_db_changes(
        self,
        scanned_workspace,
        tmp_path: Path,
    ) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        try:
            wait_for_http_ready(f"{base_url}/")
            _name_required_people(base_url, target_person_ids)
            template_id = _create_alex_blair_template(base_url, tmp_path, target_person_ids)
            preview_response = httpx.get(
                f"{base_url}/api/export-templates/{template_id}/preview",
                timeout=30.0,
            )
            assert preview_response.status_code == 200
            assert fetch_all(library_db, "SELECT COUNT(*) FROM export_plan WHERE template_id = ?", (template_id,))[0][0] > 0
            groups = _await_burst_pick_completed(base_url, template_id)["groups"]
            assert groups
            payload = {
                "group_key": groups[0]["group_key"],
                "keep_asset_ids": [groups[0]["assets"][0]["asset_id"]],
            }

            connection = sqlite3.connect(library_db)
            try:
                with connection:
                    connection.execute(
                        """
                        INSERT INTO export_run (template_id, status, started_at)
                        VALUES (?, 'running', '2026-05-23T00:00:00Z')
                        """,
                        (template_id,),
                    )
            finally:
                connection.close()

            marker_count_before = fetch_all(library_db, "SELECT COUNT(*) FROM export_abandoned_asset")[0][0]
            plan_count_before = fetch_all(library_db, "SELECT COUNT(*) FROM export_plan")[0][0]

            response = httpx.post(
                f"{base_url}/api/export-templates/{template_id}/burst-pick",
                json=payload,
                timeout=30.0,
            )

            assert response.status_code == 423
            assert "导出进行中" in response.json()["detail"]
            assert fetch_all(library_db, "SELECT COUNT(*) FROM export_abandoned_asset")[0][0] == marker_count_before
            assert fetch_all(library_db, "SELECT COUNT(*) FROM export_plan")[0][0] == plan_count_before
        finally:
            terminate_process(process)

    def test_web_post_missing_or_invalid_template_returns_readable_400(
        self,
        scanned_workspace,
        tmp_path: Path,
    ) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        try:
            wait_for_http_ready(f"{base_url}/")
            _name_required_people(base_url, target_person_ids)
            template_id = _create_alex_blair_template(base_url, tmp_path, target_person_ids)
            connection = sqlite3.connect(library_db)
            try:
                with connection:
                    connection.execute(
                        "UPDATE export_template SET status = 'invalid' WHERE template_id = ?",
                        (template_id,),
                    )
            finally:
                connection.close()

            invalid_response = httpx.post(
                f"{base_url}/exports/{template_id}/burst-pick",
                data={"group_key": "stale-group", "keep_asset_id__stale-group": "1"},
                timeout=30.0,
            )
            missing_response = httpx.post(
                f"{base_url}/exports/missing-template/burst-pick",
                data={"group_key": "stale-group", "keep_asset_id__stale-group": "1"},
                timeout=30.0,
            )

            assert invalid_response.status_code == 400
            assert "模板已失效" in invalid_response.text
            assert missing_response.status_code == 400
            assert "模板不存在" in missing_response.text
            assert fetch_all(library_db, "SELECT COUNT(*) FROM export_abandoned_asset")[0][0] == 0
        finally:
            terminate_process(process)

    def test_missing_source_file_is_skipped_and_repeated_get_has_no_side_effects(
        self,
        scanned_workspace,
        tmp_path: Path,
    ) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        source_dir = tmp_path / "ac10-missing-source"
        original_fixture = FIXTURE_DIR / "pg_031_group_alex_blair_01.jpg"
        missing_candidate_path = source_dir / "ac10_missing_after_scan_0001.jpg"
        remaining_candidate_path = source_dir / "ac10_remaining_after_scan_0002.jpg"
        _save_reencoded_variant(original_fixture, missing_candidate_path, brightness=1.01)
        _save_reencoded_variant(original_fixture, remaining_candidate_path, brightness=0.99)
        _scan_incremental_sources(workspace, [source_dir])

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        try:
            wait_for_http_ready(f"{base_url}/")
            _name_required_people(base_url, target_person_ids)
            template_id = _create_alex_blair_template(base_url, tmp_path, target_person_ids)

            original_asset_id = _asset_id_by_file(library_db, "pg_031_group_alex_blair_01.jpg")
            missing_asset_id = _asset_id_by_file(library_db, missing_candidate_path.name)
            remaining_asset_id = _asset_id_by_file(library_db, remaining_candidate_path.name)
            missing_candidate_path.unlink()

            first = _await_burst_pick_completed(base_url, template_id)
            assert first["diagnostics"]["skipped_missing_or_unreadable_count"] == 1
            assert missing_asset_id not in _burst_asset_ids(first["groups"])
            _find_group_containing(first["groups"], {original_asset_id, remaining_asset_id})
            assert fetch_all(library_db, "SELECT COUNT(*) FROM export_abandoned_asset")[0][0] == 0
            plan_count = fetch_all(library_db, "SELECT COUNT(*) FROM export_plan")[0][0]

            second = httpx.get(
                f"{base_url}/api/export-templates/{template_id}/burst-pick",
                timeout=30.0,
            )
            third = httpx.get(
                f"{base_url}/api/export-templates/{template_id}/burst-pick",
                timeout=30.0,
            )

            assert second.status_code == 200
            assert third.status_code == 200
            assert second.json() == third.json()
            assert second.json() == first
            assert fetch_all(library_db, "SELECT COUNT(*) FROM export_abandoned_asset")[0][0] == 0
            assert fetch_all(library_db, "SELECT COUNT(*) FROM export_plan")[0][0] == plan_count
        finally:
            terminate_process(process)

    def test_visual_feature_preparation_failure_is_atomic(
        self,
        scanned_workspace,
        tmp_path: Path,
    ) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        port = find_free_port()
        process = spawn_hikbox(
            "serve",
            "--workspace",
            str(workspace),
            "--port",
            str(port),
            env_updates={"HIKBOX_TEST_BURST_PICK_FAIL_FEATURES": "1"},
        )
        base_url = f"http://127.0.0.1:{port}"
        try:
            wait_for_http_ready(f"{base_url}/")
            _name_required_people(base_url, target_person_ids)
            template_id = _create_alex_blair_template(base_url, tmp_path, target_person_ids)
            marker_count_before = fetch_all(library_db, "SELECT COUNT(*) FROM export_abandoned_asset")[0][0]
            plan_count_before = fetch_all(library_db, "SELECT COUNT(*) FROM export_plan")[0][0]

            first_response = httpx.get(
                f"{base_url}/api/export-templates/{template_id}/burst-pick",
                timeout=5.0,
            )

            assert first_response.status_code == 200
            failed = _await_burst_pick_failed(base_url, template_id)
            assert failed["status"] == "failed"
            assert "视觉特征" in str(failed["error_message"])
            run_id = int(failed["run_id"])
            assert fetch_all(
                library_db,
                "SELECT status FROM export_burst_pick_run WHERE id = ?",
                (run_id,),
            ) == [("failed",)]
            assert fetch_all(
                library_db,
                """
                SELECT COUNT(*)
                FROM export_burst_pick_group
                WHERE run_id = ?
                """,
                (run_id,),
            )[0][0] == 0
            assert fetch_all(library_db, "SELECT COUNT(*) FROM export_abandoned_asset")[0][0] == marker_count_before
            assert fetch_all(library_db, "SELECT COUNT(*) FROM export_plan")[0][0] == plan_count_before
        finally:
            terminate_process(process)
