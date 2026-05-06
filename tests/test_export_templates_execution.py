from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3

import httpx
import pytest

from tests.helpers import (
    spawn_hikbox,
    find_free_port,
    wait_for_http_ready,
    terminate_process,
    fetch_all,
    expected_target_mapping,
    load_manifest,
    find_model_root,
    prepare_workspace_models,
    init_workspace,
    add_source,
    run_hikbox,
    name_person_via_api,
    merge_people_via_api,
    create_template_via_api,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "people_gallery_scan"


def _execute_sql(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(sql, params)
        connection.commit()
    finally:
        connection.close()


def _assert_name_ok(base_url: str, person_id: str, display_name: str) -> None:
    response = name_person_via_api(base_url, person_id, display_name)
    assert response.status_code in (302, 303)


def _assert_merge_ok(base_url: str, person_ids: list[str]) -> None:
    response = merge_people_via_api(base_url, person_ids)
    assert response.status_code == 303


def _get_preview_via_api(base_url: str, template_id: str) -> dict[str, object]:
    response = httpx.get(f"{base_url}/api/export-templates/{template_id}/preview", timeout=5.0)
    response.raise_for_status()
    return response.json()


def _execute_template_via_api(base_url: str, template_id: str) -> dict[str, object]:
    response = httpx.post(f"{base_url}/api/export-templates/{template_id}/execute", timeout=30.0)
    response.raise_for_status()
    return response.json()


def _get_runs_via_api(base_url: str, template_id: str) -> list[dict[str, object]]:
    response = httpx.get(f"{base_url}/api/export-templates/{template_id}/runs", timeout=5.0)
    response.raise_for_status()
    return response.json()["runs"]


def _get_run_detail_via_api(base_url: str, run_id: int) -> dict[str, object]:
    response = httpx.get(f"{base_url}/api/export-runs/{run_id}", timeout=5.0)
    response.raise_for_status()
    return response.json()


def _collect_file_tree(root: Path) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        rel = str(Path(dirpath).relative_to(root))
        if rel == ".":
            rel = ""
        result[rel] = sorted(filenames)
    return result


def _db_capture_months(library_db: Path, file_names: list[str]) -> dict[str, str]:
    rows = fetch_all(
        library_db,
        "SELECT file_name, capture_month FROM assets WHERE file_name IN ({})".format(
            ",".join("?" for _ in file_names)
        ),
        tuple(file_names),
    )
    return {str(r[0]): str(r[1]) for r in rows}


def _expected_preview_by_month_from_manifest(
    library_db: Path,
    manifest_expected: dict[str, object],
) -> dict[str, dict[str, list[str]]]:
    """根据 manifest 的 only/group 分桶和 DB 中实际的 capture_month 构建期望的月份分桶结构。"""
    all_expected_files: list[str] = []
    for bucket in ("only", "group"):
        for files in manifest_expected[bucket].values():
            all_expected_files.extend(files)
    db_months = _db_capture_months(library_db, all_expected_files)

    result: dict[str, dict[str, list[str]]] = {}
    for bucket in ("only", "group"):
        for _manifest_month, files in manifest_expected[bucket].items():
            for f in files:
                actual_month = db_months.get(f, "unknown-date")
                if actual_month not in result:
                    result[actual_month] = {"only": [], "group": []}
                result[actual_month][bucket].append(f)

    # Sort files within each bucket for stable comparison
    for month_data in result.values():
        for bucket in ("only", "group"):
            month_data[bucket] = sorted(month_data[bucket])
    return result


def _preview_by_month_from_api(preview: dict[str, object]) -> dict[str, dict[str, list[str]]]:
    result: dict[str, dict[str, list[str]]] = {}
    for month_bucket in preview["months"]:
        month = month_bucket["month"]
        result[month] = {
            "only": sorted(a["file_name"] for a in month_bucket["only"]),
            "group": sorted(a["file_name"] for a in month_bucket["group"]),
        }
    return result


def _write_per_file_copy_hook_module(module_dir: Path, config_path: Path) -> None:
    module_dir.mkdir(parents=True, exist_ok=True)
    (module_dir / "sitecustomize.py").write_text(
        f'''
import json
import os
import sqlite3

import hikbox_pictures.product.export_templates as et

def make_hook():
    config_path = {repr(str(config_path))}
    def hook():
        if not os.path.exists(config_path):
            return
        try:
            with open(config_path, "r") as f:
                config = json.load(f)
        except Exception:
            return
        if config.get("done"):
            return
        library_db = config["library_db"]
        asset_id = config["asset_id"]
        person_id = config["person_id"]
        conn = sqlite3.connect(library_db)
        try:
            row = conn.execute(
                "SELECT id FROM face_observations WHERE asset_id = ? LIMIT 1",
                (asset_id,),
            ).fetchone()
            if row:
                face_id = row[0]
                run_row = conn.execute(
                    "SELECT id FROM assignment_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()
                assignment_run_id = run_row[0] if run_row else 1
                conn.execute(
                    "INSERT INTO person_face_assignments "
                    "(person_id, face_observation_id, assignment_run_id, assignment_source, active, evidence_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'online_v6', 1, '{{}}', datetime('now'), datetime('now'))",
                    (person_id, face_id, assignment_run_id),
                )
                conn.commit()
        finally:
            conn.close()
        config["done"] = True
        with open(config_path, "w") as f:
            json.dump(config, f)
    return hook

et.set_per_file_copy_hook(make_hook())
''',
        encoding="utf-8",
    )


class TestExportTemplatePreview:
    def test_preview_api_aligns_with_manifest(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            wait_for_http_ready(f"{base_url}/")
            _assert_name_ok(base_url, alex_id, "Alex Chen")
            _assert_name_ok(base_url, blair_id, "Blair Lin")

            result = create_template_via_api(base_url, name="Alex & Blair", person_ids=[alex_id, blair_id], output_root=output_root)
            template_id = result["template_id"]

            preview = _get_preview_via_api(base_url, template_id)
            expected = manifest["expected_exports"]["target_alex_blair"]

            # AC-1: exact month-bucket alignment using actual DB capture_months
            expected_by_month = _expected_preview_by_month_from_manifest(library_db, expected)
            preview_by_month = _preview_by_month_from_api(preview)

            assert preview_by_month == expected_by_month, (
                f"preview month buckets mismatch:\npreview={preview_by_month}\nexpected={expected_by_month}"
            )

            expected_only_files = set()
            expected_group_files = set()
            for files in expected["only"].values():
                expected_only_files.update(files)
            for files in expected["group"].values():
                expected_group_files.update(files)

            assert preview["total_count"] == len(expected_only_files) + len(expected_group_files)
            assert preview["only_count"] == len(expected_only_files)
            assert preview["group_count"] == len(expected_group_files)
        finally:
            terminate_process(process)

    def test_preview_rejects_invalid_template(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            wait_for_http_ready(f"{base_url}/")
            _assert_name_ok(base_url, alex_id, "Alex Chen")
            _assert_name_ok(base_url, blair_id, "Blair Lin")

            result = create_template_via_api(base_url, name="Alex & Blair", person_ids=[alex_id, blair_id], output_root=output_root)
            template_id = result["template_id"]

            # Merge to invalidate template
            _assert_merge_ok(base_url, [alex_id, blair_id])

            response = httpx.get(f"{base_url}/api/export-templates/{template_id}/preview", timeout=5.0)
            assert response.status_code == 400
            assert "invalid" in response.text.lower() or "失效" in response.text
        finally:
            terminate_process(process)


class TestExportTemplateExecution:
    def test_execute_copies_files_and_mov(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = tmp_path / "export-output"
        try:
            wait_for_http_ready(f"{base_url}/")
            _assert_name_ok(base_url, alex_id, "Alex Chen")
            _assert_name_ok(base_url, blair_id, "Blair Lin")

            # AC-3: Make a multi-person JPG asset appear as HEIC with a MOV pair
            # so the HEIC/HEIF branch in _copy_asset is exercised through the public API.
            mov_src = FIXTURE_DIR / ".pg_047_live_positive_01.MOV"
            mov_dst = tmp_path / ".pg_031_group_alex_blair_01.MOV"
            shutil.copy2(mov_src, mov_dst)
            _execute_sql(
                library_db,
                "UPDATE assets SET file_extension = 'heic', live_photo_mov_path = ? WHERE file_name = ?",
                (str(mov_dst), "pg_031_group_alex_blair_01.jpg"),
            )

            result = create_template_via_api(base_url, name="Alex & Blair", person_ids=[alex_id, blair_id], output_root=str(output_root))
            template_id = result["template_id"]

            preview = _get_preview_via_api(base_url, template_id)
            expected = manifest["expected_exports"]["target_alex_blair"]

            # Verify preview aligns first (exact month buckets via DB-adjusted manifest)
            expected_by_month = _expected_preview_by_month_from_manifest(library_db, expected)
            preview_by_month = _preview_by_month_from_api(preview)
            assert preview_by_month == expected_by_month, (
                f"preview month buckets mismatch:\npreview={preview_by_month}\nexpected={expected_by_month}"
            )

            execute_result = _execute_template_via_api(base_url, template_id)
            run_id = execute_result["run_id"]

            # Verify file tree (bucket correct; month derived from actual DB capture_month)
            expected_only_files = set()
            expected_group_files = set()
            for files in expected["only"].values():
                expected_only_files.update(files)
            for files in expected["group"].values():
                expected_group_files.update(files)

            db_months = _db_capture_months(library_db, list(expected_only_files | expected_group_files))
            tree = _collect_file_tree(output_root)
            for f in expected_only_files:
                month = db_months.get(f, "unknown-date")
                key = f"only/{month}"
                assert key in tree, f"Missing directory {key} in {tree}"
                assert f in tree[key], f"Missing file {f} in {key}"
            for f in expected_group_files:
                month = db_months.get(f, "unknown-date")
                key = f"group/{month}"
                assert key in tree, f"Missing directory {key} in {tree}"
                assert f in tree[key], f"Missing file {f} in {key}"

            # Verify HEIC asset's MOV was copied to the same month directory
            heic_month = db_months.get("pg_031_group_alex_blair_01.jpg", "unknown-date")
            heic_bucket = "only" if "pg_031_group_alex_blair_01.jpg" in expected_only_files else "group"
            mov_copied = output_root / heic_bucket / heic_month / mov_dst.name
            assert mov_copied.exists(), f"HEIC paired MOV should be copied: {mov_copied}"

            # Verify JPG/PNG assets do NOT have MOV copied alongside them
            for dir_name, files in tree.items():
                for f in files:
                    if f.lower().endswith(".jpg") or f.lower().endswith(".jpeg") or f.lower().endswith(".png"):
                        mov_name = f.rsplit(".", 1)[0] + ".mov"
                        assert mov_name.lower() not in [x.lower() for x in files], f"Unexpected MOV for {f}"

            # Verify run record
            run_detail = _get_run_detail_via_api(base_url, run_id)
            assert run_detail["status"] == "completed"
            assert run_detail["template_name"] == "Alex & Blair"

            # Verify deliveries
            deliveries = run_detail["deliveries"]
            copied = [d for d in deliveries if d["result"] == "copied"]
            skipped = [d for d in deliveries if d["result"] == "skipped_exists"]
            assert len(copied) == run_detail["copied_count"]
            assert len(skipped) == run_detail["skipped_count"]

            # The modified HEIC asset should have mov_result='copied'
            heic_delivery = next(
                (d for d in deliveries if d["target_path"].endswith("pg_031_group_alex_blair_01.jpg")),
                None,
            )
            assert heic_delivery is not None
            assert heic_delivery["mov_result"] == "copied", f"HEIC asset should have mov_result=copied: {heic_delivery}"

            # All other assets are JPG/PNG, so mov_result should be not_applicable
            for d in deliveries:
                if not d["target_path"].lower().endswith("pg_031_group_alex_blair_01.jpg"):
                    assert d["mov_result"] == "not_applicable", f"Non-HEIC should have not_applicable mov_result: {d}"
        finally:
            terminate_process(process)

    def test_execute_skips_existing_files(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = tmp_path / "export-output"
        try:
            wait_for_http_ready(f"{base_url}/")
            _assert_name_ok(base_url, alex_id, "Alex Chen")
            _assert_name_ok(base_url, blair_id, "Blair Lin")

            result = create_template_via_api(base_url, name="Alex & Blair", person_ids=[alex_id, blair_id], output_root=str(output_root))
            template_id = result["template_id"]

            expected = manifest["expected_exports"]["target_alex_blair"]
            # Determine actual months from DB
            db_months = _db_capture_months(library_db, ["pg_031_group_alex_blair_01.jpg", "pg_032_group_alex_blair_02.jpg"])
            actual_month = db_months.get("pg_031_group_alex_blair_01.jpg", "unknown-date")
            # Pre-place a file in only/<actual_month> with different content
            placeholder_dir = output_root / "only" / actual_month
            placeholder_dir.mkdir(parents=True)
            placeholder_path = placeholder_dir / "pg_031_group_alex_blair_01.jpg"
            placeholder_content = b"placeholder"
            placeholder_path.write_bytes(placeholder_content)
            placeholder_mtime_before = placeholder_path.stat().st_mtime

            # Preview first to populate export_plan
            _get_preview_via_api(base_url, template_id)
            execute_result = _execute_template_via_api(base_url, template_id)
            run_id = execute_result["run_id"]

            # Verify placeholder unchanged
            assert placeholder_path.read_bytes() == placeholder_content
            assert placeholder_path.stat().st_mtime == placeholder_mtime_before

            # Verify delivery record
            deliveries = _get_run_detail_via_api(base_url, run_id)["deliveries"]
            skipped_delivery = next((d for d in deliveries if d["target_path"].endswith("pg_031_group_alex_blair_01.jpg")), None)
            assert skipped_delivery is not None
            assert skipped_delivery["result"] == "skipped_exists"

            # Verify other files were copied
            other_month = db_months.get("pg_032_group_alex_blair_02.jpg", "unknown-date")
            other_file = output_root / "only" / other_month / "pg_032_group_alex_blair_02.jpg"
            assert other_file.exists()
            assert other_file.read_bytes() != b"placeholder"
        finally:
            terminate_process(process)

    def test_invalid_template_execute_rejected(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            wait_for_http_ready(f"{base_url}/")
            _assert_name_ok(base_url, alex_id, "Alex Chen")
            _assert_name_ok(base_url, blair_id, "Blair Lin")

            result = create_template_via_api(base_url, name="Alex & Blair", person_ids=[alex_id, blair_id], output_root=output_root)
            template_id = result["template_id"]

            _assert_merge_ok(base_url, [alex_id, blair_id])

            run_count_before = fetch_all(library_db, "SELECT COUNT(*) FROM export_run")[0][0]
            response = httpx.post(f"{base_url}/api/export-templates/{template_id}/execute", timeout=5.0)
            assert response.status_code == 400
            run_count_after = fetch_all(library_db, "SELECT COUNT(*) FROM export_run")[0][0]
            assert run_count_after == run_count_before
        finally:
            terminate_process(process)

    def test_history_shows_run_and_deliveries(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            wait_for_http_ready(f"{base_url}/")
            _assert_name_ok(base_url, alex_id, "Alex Chen")
            _assert_name_ok(base_url, blair_id, "Blair Lin")

            result = create_template_via_api(base_url, name="Alex & Blair", person_ids=[alex_id, blair_id], output_root=output_root)
            template_id = result["template_id"]

            # Preview first to populate export_plan
            _get_preview_via_api(base_url, template_id)
            execute_result = _execute_template_via_api(base_url, template_id)
            run_id = execute_result["run_id"]

            runs = _get_runs_via_api(base_url, template_id)
            assert len(runs) == 1
            assert runs[0]["run_id"] == run_id
            assert runs[0]["status"] == "completed"
            assert runs[0]["copied_count"] > 0

            run_detail = _get_run_detail_via_api(base_url, run_id)
            assert run_detail["template_name"] == "Alex & Blair"
            assert run_detail["status"] == "completed"
            assert run_detail["copied_count"] > 0
            assert len(run_detail["deliveries"]) > 0
            for d in run_detail["deliveries"]:
                assert d["target_path"]
                assert d["result"] in ("copied", "skipped_exists")
                assert d["mov_result"] in ("copied", "skipped_missing", "not_applicable")
        finally:
            terminate_process(process)

    def test_unwritable_output_root_marks_failed(self, scanned_workspace, tmp_path: Path) -> None:
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = tmp_path / "export-output"
        output_root.mkdir()
        # Make directory read-only
        os.chmod(output_root, 0o555)
        try:
            wait_for_http_ready(f"{base_url}/")
            _assert_name_ok(base_url, alex_id, "Alex Chen")
            _assert_name_ok(base_url, blair_id, "Blair Lin")

            result = create_template_via_api(base_url, name="Alex & Blair", person_ids=[alex_id, blair_id], output_root=str(output_root))
            template_id = result["template_id"]

            # Preview first to populate export_plan (preview only writes to DB, not filesystem)
            _get_preview_via_api(base_url, template_id)
            response = httpx.post(f"{base_url}/api/export-templates/{template_id}/execute", timeout=5.0)
            assert response.status_code == 500

            run_rows = fetch_all(library_db, "SELECT run_id, status FROM export_run WHERE template_id = ?", (template_id,))
            assert len(run_rows) == 1
            assert run_rows[0][1] == "failed"
        finally:
            os.chmod(output_root, 0o755)
            terminate_process(process)

    def test_export_preserves_exif_and_timestamps(self, scanned_workspace, tmp_path: Path) -> None:
        from PIL import Image

        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = tmp_path / "export-output"
        try:
            wait_for_http_ready(f"{base_url}/")
            _assert_name_ok(base_url, alex_id, "Alex Chen")
            _assert_name_ok(base_url, blair_id, "Blair Lin")

            result = create_template_via_api(base_url, name="Alex & Blair", person_ids=[alex_id, blair_id], output_root=str(output_root))
            template_id = result["template_id"]

            # Preview first to populate export_plan
            _get_preview_via_api(base_url, template_id)
            _execute_template_via_api(base_url, template_id)

            # Find one exported JPG and compare with source
            src_path = FIXTURE_DIR / "pg_031_group_alex_blair_01.jpg"
            db_months = _db_capture_months(library_db, ["pg_031_group_alex_blair_01.jpg"])
            actual_month = db_months.get("pg_031_group_alex_blair_01.jpg", "unknown-date")
            dst_path = output_root / "only" / actual_month / "pg_031_group_alex_blair_01.jpg"
            assert dst_path.exists()

            src_exif = Image.open(src_path).info.get("exif")
            dst_exif = Image.open(dst_path).info.get("exif")
            assert src_exif == dst_exif, "EXIF data mismatch"

            src_stat = src_path.stat()
            dst_stat = dst_path.stat()
            assert abs(src_stat.st_mtime - dst_stat.st_mtime) < 2, "mtime mismatch"
            if hasattr(src_stat, "st_birthtime"):
                assert abs(src_stat.st_birthtime - dst_stat.st_birthtime) < 2, "birthtime mismatch"
        finally:
            terminate_process(process)

    def test_execute_ignores_later_asset_changes(self, scanned_workspace, tmp_path: Path) -> None:
        # AC-7: export should use snapshot from start time.
        # We use the test-specific per-file copy hook to inject a new assignment
        # for an asset that is NOT in the preview snapshot, proving the export
        # continues with its snapshot and does not pick up the injected change.
        workspace, external_root, library_db, manifest, target_person_ids = scanned_workspace
        alex_id = target_person_ids["target_alex"]
        blair_id = target_person_ids["target_blair"]

        port = find_free_port()
        hook_module_dir = tmp_path / "hook_module"
        hook_config_path = tmp_path / "hook_config.json"
        _write_per_file_copy_hook_module(hook_module_dir, hook_config_path)

        process = spawn_hikbox(
            "serve",
            "--workspace", str(workspace),
            "--port", str(port),
            pythonpath_prepend=[hook_module_dir],
            env_updates={"HIKBOX_TEST_HOOK_CONFIG": str(hook_config_path)},
        )
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            wait_for_http_ready(f"{base_url}/")
            _assert_name_ok(base_url, alex_id, "Alex Chen")
            _assert_name_ok(base_url, blair_id, "Blair Lin")

            result = create_template_via_api(base_url, name="Alex & Blair", person_ids=[alex_id, blair_id], output_root=output_root)
            template_id = result["template_id"]

            preview = _get_preview_via_api(base_url, template_id)
            snapshot_total = preview["total_count"]
            snapshot_asset_ids = {
                a["asset_id"]
                for mb in preview["months"]
                for bucket in ("only", "group")
                for a in mb[bucket]
            }

            # Pick a future asset not in the snapshot that has an unassigned face
            # (so we can inject a new assignment without violating the unique active
            # face constraint). pg_039_non_target_01.jpg has an unassigned face.
            future_asset_rows = fetch_all(
                library_db,
                """
                SELECT a.id
                FROM assets a
                INNER JOIN face_observations fo ON fo.asset_id = a.id
                WHERE a.file_name = ?
                  AND fo.id NOT IN (
                    SELECT face_observation_id FROM person_face_assignments WHERE active = 1
                  )
                LIMIT 1
                """,
                ("pg_039_non_target_01.jpg",),
            )
            assert len(future_asset_rows) == 1
            future_asset_id = future_asset_rows[0][0]
            assert future_asset_id not in snapshot_asset_ids

            # Write hook config so that during file-copy loop a new assignment is injected
            hook_config = {
                "library_db": str(library_db),
                "asset_id": future_asset_id,
                "person_id": blair_id,
                "done": False,
            }
            hook_config_path.write_text(json.dumps(hook_config), encoding="utf-8")

            execute_result = _execute_template_via_api(base_url, template_id)
            run_id = execute_result["run_id"]

            run_detail = _get_run_detail_via_api(base_url, run_id)
            assert run_detail["status"] == "completed"
            assert len(run_detail["deliveries"]) == snapshot_total, (
                f"Delivery count {len(run_detail['deliveries'])} should match preview snapshot {snapshot_total}"
            )

            # The injected asset must NOT appear in deliveries
            delivery_asset_ids = {d["asset_id"] for d in run_detail["deliveries"]}
            assert future_asset_id not in delivery_asset_ids, (
                f"Injected future asset {future_asset_id} should not be in deliveries"
            )

            # File tree must contain exactly snapshot assets (no injected asset)
            tree = _collect_file_tree(Path(output_root))
            all_files = []
            for files in tree.values():
                all_files.extend(files)
            assert len(all_files) == snapshot_total, (
                f"File tree count {len(all_files)} should match snapshot {snapshot_total}"
            )
        finally:
            terminate_process(process)
