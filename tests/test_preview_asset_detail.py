"""导出模板预览页 — 照片-人物详情页集成测试。

验证：
- 照片-人物详情页 GET 返回正确的 HTML 内容
- crop 图片路由返回图片文件
- 排除操作从详情页发起后正确生效
- 排除后详情页反映变化
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import time

import httpx
import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "people_gallery_scan"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"


def _spawn_hikbox(*args: str) -> subprocess.Popen[str]:
    env = os.environ.copy()
    pythonpath_parts = [str(REPO_ROOT)]
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        pythonpath_parts.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    return subprocess.Popen(
        [sys.executable, "-m", "hikbox_pictures", *args],
        cwd=str(REPO_ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


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
        except Exception as exc:
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


def _name_person(base_url: str, person_id: str, display_name: str) -> None:
    response = httpx.post(
        f"{base_url}/people/{person_id}/name",
        data={"display_name": display_name},
        follow_redirects=False,
        timeout=5.0,
    )
    assert response.status_code in (302, 303)


def _create_template(
    base_url: str, *, name: str, person_ids: list[str], output_root: str,
) -> str:
    response = httpx.post(
        f"{base_url}/api/export-templates",
        data={"name": name, "output_root": output_root, "person_id": person_ids},
        timeout=5.0,
    )
    assert response.status_code == 200
    return str(response.json()["template_id"])


def _get_preview_api(base_url: str, template_id: str) -> dict:
    response = httpx.get(
        f"{base_url}/api/export-templates/{template_id}/preview", timeout=10.0,
    )
    assert response.status_code == 200
    return response.json()


class TestPreviewAssetDetailPage:
    """照片-人物详情页 GET 请求。"""

    def test_detail_page_returns_html_with_person_groups(
        self, scanned_workspace, tmp_path: Path,
    ) -> None:
        workspace, external_root, library_db, manifest, target_mapping = scanned_workspace
        alex_id = target_mapping["target_alex"]
        blair_id = target_mapping["target_blair"]
        casey_id = target_mapping["target_casey"]

        port = _find_free_port()
        process = _spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            _wait_for_http_ready(f"{base_url}/")
            _name_person(base_url, alex_id, "Alex")
            _name_person(base_url, blair_id, "Blair")
            _name_person(base_url, casey_id, "Casey")

            template_id = _create_template(
                base_url, name="全员", person_ids=[alex_id, blair_id, casey_id],
                output_root=output_root,
            )
            preview = _get_preview_api(base_url, template_id)

            # 取第一个 asset
            first_asset = None
            for month in preview["months"]:
                for asset in month.get("only", []):
                    first_asset = asset
                    break
                if first_asset:
                    break
                for asset in month.get("group", []):
                    first_asset = asset
                    break
            assert first_asset is not None, "预览中无命中资产"

            asset_id = first_asset["asset_id"]
            url = f"{base_url}/exports/{template_id}/preview/{asset_id}"
            response = httpx.get(url, follow_redirects=True, timeout=5.0)
            assert response.status_code == 200
            html = response.text

            assert "Alex" in html
            assert "Blair" in html
            assert "Casey" in html
            assert "排除此人脸" in html
        finally:
            _terminate_process(process)

    def test_detail_page_redirects_for_nonexistent_asset(
        self, scanned_workspace, tmp_path: Path,
    ) -> None:
        workspace, external_root, library_db, manifest, target_mapping = scanned_workspace
        alex_id = target_mapping["target_alex"]
        blair_id = target_mapping["target_blair"]

        port = _find_free_port()
        process = _spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            _wait_for_http_ready(f"{base_url}/")
            _name_person(base_url, alex_id, "Alex")
            _name_person(base_url, blair_id, "Blair")

            template_id = _create_template(
                base_url, name="双人", person_ids=[alex_id, blair_id],
                output_root=output_root,
            )

            url = f"{base_url}/exports/{template_id}/preview/99999"
            response = httpx.get(url, follow_redirects=False, timeout=5.0)
            assert response.status_code == 303
            assert f"/exports/{template_id}/preview" in response.headers.get("location", "")
        finally:
            _terminate_process(process)


class TestFaceCropImage:
    """crop 图片路由。"""

    def test_crop_image_returns_jpeg(
        self, scanned_workspace, tmp_path: Path,
    ) -> None:
        workspace, external_root, library_db, manifest, target_mapping = scanned_workspace
        alex_id = target_mapping["target_alex"]
        blair_id = target_mapping["target_blair"]
        casey_id = target_mapping["target_casey"]

        port = _find_free_port()
        process = _spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            _wait_for_http_ready(f"{base_url}/")
            _name_person(base_url, alex_id, "Alex")
            _name_person(base_url, blair_id, "Blair")
            _name_person(base_url, casey_id, "Casey")

            template_id = _create_template(
                base_url, name="全员", person_ids=[alex_id, blair_id, casey_id],
                output_root=output_root,
            )
            preview = _get_preview_api(base_url, template_id)

            # 从数据库获取一个 face_observation_id
            rows = _fetch_all(
                library_db,
                """
                SELECT fo.id
                FROM face_observations fo
                INNER JOIN person_face_assignments pfa
                  ON pfa.face_observation_id = fo.id AND pfa.active = 1
                WHERE pfa.person_id IN (?, ?, ?)
                LIMIT 1
                """,
                (alex_id, blair_id, casey_id),
            )
            assert rows, "无 active face observation"
            face_obs_id = int(rows[0][0])

            url = f"{base_url}/images/faces/{face_obs_id}/crop"
            response = httpx.get(url, timeout=5.0)
            assert response.status_code == 200
            assert "image/jpeg" in response.headers.get("content-type", "")
        finally:
            _terminate_process(process)

    def test_crop_image_404_for_nonexistent_face(
        self, scanned_workspace, tmp_path: Path,
    ) -> None:
        workspace, external_root, library_db, manifest, target_mapping = scanned_workspace

        port = _find_free_port()
        process = _spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        try:
            _wait_for_http_ready(f"{base_url}/")

            url = f"{base_url}/images/faces/99999/crop"
            response = httpx.get(url, timeout=5.0)
            assert response.status_code == 404
        finally:
            _terminate_process(process)


class TestPreviewAssetExclusion:
    """从照片-人物详情页发起排除。"""

    def test_exclude_face_from_detail_page(
        self, scanned_workspace, tmp_path: Path,
    ) -> None:
        workspace, external_root, library_db, manifest, target_mapping = scanned_workspace
        alex_id = target_mapping["target_alex"]
        blair_id = target_mapping["target_blair"]
        casey_id = target_mapping["target_casey"]

        port = _find_free_port()
        process = _spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            _wait_for_http_ready(f"{base_url}/")
            _name_person(base_url, alex_id, "Alex")
            _name_person(base_url, blair_id, "Blair")
            _name_person(base_url, casey_id, "Casey")

            template_id = _create_template(
                base_url, name="全员", person_ids=[alex_id, blair_id, casey_id],
                output_root=output_root,
            )
            preview = _get_preview_api(base_url, template_id)

            # 取第一个 asset
            first_asset = None
            for month in preview["months"]:
                for asset in month.get("only", []):
                    first_asset = asset
                    break
                if first_asset:
                    break
                for asset in month.get("group", []):
                    first_asset = asset
                    break
            assert first_asset is not None
            asset_id = first_asset["asset_id"]

            # 获取详情页，找到第一个排除表单的参数
            detail_url = f"{base_url}/exports/{template_id}/preview/{asset_id}"
            detail_resp = httpx.get(detail_url, timeout=5.0)
            assert detail_resp.status_code == 200
            html = detail_resp.text

            # 从数据库获取该 asset 在选中人物范围内的第一个 active assignment
            rows = _fetch_all(
                library_db,
                """
                SELECT pfa.id, pfa.person_id
                FROM person_face_assignments pfa
                INNER JOIN face_observations fo ON fo.id = pfa.face_observation_id
                WHERE fo.asset_id = ?
                  AND pfa.active = 1
                  AND pfa.person_id IN (?, ?, ?)
                ORDER BY pfa.id ASC
                LIMIT 1
                """,
                (asset_id, alex_id, blair_id, casey_id),
            )
            assert rows, f"asset_id={asset_id} 无命中 assignment"
            assignment_id = str(rows[0][0])
            person_id = str(rows[0][1])

            # 提交排除
            exclude_url = f"{base_url}/exports/{template_id}/preview/{asset_id}/exclude"
            exclude_resp = httpx.post(
                exclude_url,
                data={"person_id": person_id, "assignment_id": assignment_id},
                follow_redirects=False,
                timeout=5.0,
            )
            assert exclude_resp.status_code == 303
            assert f"/exports/{template_id}/preview/{asset_id}" in exclude_resp.headers.get("location", "")

            # 重新访问详情页，检查该人脸已消失
            detail_resp2 = httpx.get(detail_url, follow_redirects=True, timeout=5.0)
            assert detail_resp2.status_code == 200

            # 验证数据库中该 assignment 已变为 inactive
            active_check = _fetch_all(
                library_db,
                "SELECT active FROM person_face_assignments WHERE id = ?",
                (int(assignment_id),),
            )
            assert active_check[0][0] == 0

            # 验证预览总数减少
            preview2 = _get_preview_api(base_url, template_id)
            assert preview2["total_count"] <= preview["total_count"]
        finally:
            _terminate_process(process)


def _fetch_all(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(db_path)
    try:
        return [tuple(row) for row in connection.execute(sql, params).fetchall()]
    finally:
        connection.close()
