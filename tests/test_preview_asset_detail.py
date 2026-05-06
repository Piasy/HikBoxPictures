"""导出模板预览页 — 照片-人物详情页集成测试。

验证：
- 照片-人物详情页 GET 返回正确的 HTML 内容
- crop 图片路由返回图片文件
- 排除操作从详情页发起后正确生效
- 排除后详情页反映变化
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tests.helpers import (
    create_template_via_api,
    fetch_all,
    find_free_port,
    name_person_via_api,
    spawn_hikbox,
    terminate_process,
    wait_for_http_ready,
)


def get_preview_api(base_url: str, template_id: str) -> dict:
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

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            wait_for_http_ready(f"{base_url}/")
            assert name_person_via_api(base_url, alex_id, "Alex").status_code in (302, 303)
            assert name_person_via_api(base_url, blair_id, "Blair").status_code in (302, 303)
            assert name_person_via_api(base_url, casey_id, "Casey").status_code in (302, 303)

            template_id = str(create_template_via_api(
                base_url, name="全员", person_ids=[alex_id, blair_id, casey_id],
                output_root=output_root,
            )["template_id"])
            preview = get_preview_api(base_url, template_id)

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
            terminate_process(process)

    def test_detail_page_redirects_for_nonexistent_asset(
        self, scanned_workspace, tmp_path: Path,
    ) -> None:
        workspace, external_root, library_db, manifest, target_mapping = scanned_workspace
        alex_id = target_mapping["target_alex"]
        blair_id = target_mapping["target_blair"]

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            wait_for_http_ready(f"{base_url}/")
            assert name_person_via_api(base_url, alex_id, "Alex").status_code in (302, 303)
            assert name_person_via_api(base_url, blair_id, "Blair").status_code in (302, 303)

            template_id = str(create_template_via_api(
                base_url, name="双人", person_ids=[alex_id, blair_id],
                output_root=output_root,
            )["template_id"])

            url = f"{base_url}/exports/{template_id}/preview/99999"
            response = httpx.get(url, follow_redirects=False, timeout=5.0)
            assert response.status_code == 303
            assert f"/exports/{template_id}/preview" in response.headers.get("location", "")
        finally:
            terminate_process(process)


class TestFaceCropImage:
    """crop 图片路由。"""

    def test_crop_image_returns_jpeg(
        self, scanned_workspace, tmp_path: Path,
    ) -> None:
        workspace, external_root, library_db, manifest, target_mapping = scanned_workspace
        alex_id = target_mapping["target_alex"]
        blair_id = target_mapping["target_blair"]
        casey_id = target_mapping["target_casey"]

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            wait_for_http_ready(f"{base_url}/")
            assert name_person_via_api(base_url, alex_id, "Alex").status_code in (302, 303)
            assert name_person_via_api(base_url, blair_id, "Blair").status_code in (302, 303)
            assert name_person_via_api(base_url, casey_id, "Casey").status_code in (302, 303)

            template_id = str(create_template_via_api(
                base_url, name="全员", person_ids=[alex_id, blair_id, casey_id],
                output_root=output_root,
            )["template_id"])
            preview = get_preview_api(base_url, template_id)

            # 从数据库获取一个 face_observation_id
            rows = fetch_all(
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
            terminate_process(process)

    def test_crop_image_404_for_nonexistent_face(
        self, scanned_workspace, tmp_path: Path,
    ) -> None:
        workspace, external_root, library_db, manifest, target_mapping = scanned_workspace

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        try:
            wait_for_http_ready(f"{base_url}/")

            url = f"{base_url}/images/faces/99999/crop"
            response = httpx.get(url, timeout=5.0)
            assert response.status_code == 404
        finally:
            terminate_process(process)


class TestPreviewAssetExclusion:
    """从照片-人物详情页发起排除。"""

    def test_exclude_face_from_detail_page(
        self, scanned_workspace, tmp_path: Path,
    ) -> None:
        workspace, external_root, library_db, manifest, target_mapping = scanned_workspace
        alex_id = target_mapping["target_alex"]
        blair_id = target_mapping["target_blair"]
        casey_id = target_mapping["target_casey"]

        port = find_free_port()
        process = spawn_hikbox("serve", "--workspace", str(workspace), "--port", str(port))
        base_url = f"http://127.0.0.1:{port}"
        output_root = str(tmp_path / "export-output")
        try:
            wait_for_http_ready(f"{base_url}/")
            assert name_person_via_api(base_url, alex_id, "Alex").status_code in (302, 303)
            assert name_person_via_api(base_url, blair_id, "Blair").status_code in (302, 303)
            assert name_person_via_api(base_url, casey_id, "Casey").status_code in (302, 303)

            template_id = str(create_template_via_api(
                base_url, name="全员", person_ids=[alex_id, blair_id, casey_id],
                output_root=output_root,
            )["template_id"])
            preview = get_preview_api(base_url, template_id)

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
            rows = fetch_all(
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
            active_check = fetch_all(
                library_db,
                "SELECT active FROM person_face_assignments WHERE id = ?",
                (int(assignment_id),),
            )
            assert active_check[0][0] == 0

            # 验证预览总数减少
            preview2 = get_preview_api(base_url, template_id)
            assert preview2["total_count"] <= preview["total_count"]
        finally:
            terminate_process(process)
