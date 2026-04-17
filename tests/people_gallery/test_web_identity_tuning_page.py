from __future__ import annotations

import html
import json
import re
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from fastapi.testclient import TestClient

from hikbox_pictures.api.app import create_app

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace_identity_tuning_page", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_identity_seed_workspace = _MODULE.build_identity_seed_workspace


def _extract_embedded_json(html_text: str) -> dict[str, object]:
    match = re.search(
        r'<script id="identity-tuning-data" type="application/json">\s*(.*?)\s*</script>',
        html_text,
        re.DOTALL,
    )
    assert match is not None, "页面缺少 identity-tuning-data JSON 脚本"
    payload = html.unescape(match.group(1)).strip()
    assert payload, "identity-tuning-data JSON 脚本为空"
    parsed = json.loads(payload)
    assert isinstance(parsed, dict)
    return parsed


def _strip_embedded_json(html_text: str) -> str:
    return re.sub(
        r'<script id="identity-tuning-data" type="application/json">\s*.*?\s*</script>',
        "",
        html_text,
        flags=re.DOTALL,
    )


def _insert_legacy_bootstrap_materialized_person(ws) -> tuple[int, int, int]:
    batch_id = int(
        ws.conn.execute(
            """
            INSERT INTO auto_cluster_batch(model_key, algorithm_version, batch_type, threshold_profile_id, scan_session_id)
            VALUES (?, ?, 'bootstrap', ?, NULL)
            RETURNING id
            """,
            (
                ws.model_key,
                "identity.bootstrap.v1",
                ws.profile_id,
            ),
        ).fetchone()["id"]
    )
    cluster_id = int(
        ws.conn.execute(
            """
            INSERT INTO auto_cluster(
                batch_id,
                representative_observation_id,
                cluster_status,
                resolved_person_id,
                diagnostic_json
            )
            VALUES (?, NULL, 'materialized', NULL, '{}')
            RETURNING id
            """,
            (batch_id,),
        ).fetchone()["id"]
    )
    person_id = int(
        ws.conn.execute(
            """
            INSERT INTO person(
                display_name,
                status,
                confirmed,
                ignored,
                notes,
                cover_observation_id,
                origin_cluster_id
            )
            VALUES ('未命名人物-旧批次', 'active', 0, 0, NULL, NULL, ?)
            RETURNING id
            """,
            (cluster_id,),
        ).fetchone()["id"]
    )
    ws.conn.execute(
        """
        UPDATE auto_cluster
        SET resolved_person_id = ?
        WHERE id = ?
        """,
        (person_id, cluster_id),
    )
    ws.conn.commit()
    return batch_id, cluster_id, person_id


def test_identity_tuning_page_shows_required_blocks_and_embedded_json(tmp_path: Path) -> None:
    ws = build_identity_seed_workspace(tmp_path / "identity-tuning-page")
    try:
        ws.seed_edge_rule_challenge_case()
        ws.new_bootstrap_service().run_bootstrap(profile_id=ws.profile_id)

        client = TestClient(create_app(workspace=ws.root))
        response = client.get("/identity-tuning")

        assert response.status_code == 200
        html_text = response.text
        assert "阈值调参与 Bootstrap 验收" in html_text
        assert "active profile" in html_text
        assert "bootstrap batch" in html_text
        assert "匿名人物" in html_text
        assert "cover observation" in html_text
        assert "seed 组成" in html_text
        assert "pending cluster 诊断" in html_text
        assert "distinct_photo_count" in html_text
        assert "quality_distribution" in html_text
        assert "external_margin" in html_text
        assert "reject_reason" in html_text
        assert "全图库高质量 observation" in html_text
        assert html_text.index("pending cluster 诊断") < html_text.index("全图库高质量 observation")

        payload = _extract_embedded_json(html_text)
        assert "active_profile" in payload
        assert "bootstrap_batch" in payload
        assert "anonymous_people" in payload
        assert "pending_clusters" in payload
        assert "high_quality_observations" in payload

        active_profile = payload["active_profile"]
        assert isinstance(active_profile, dict)
        assert int(active_profile["id"]) == ws.profile_id

        bootstrap_batch = payload["bootstrap_batch"]
        assert isinstance(bootstrap_batch, dict)
        assert bootstrap_batch["batch_type"] == "bootstrap"
        assert int(bootstrap_batch["threshold_profile_id"]) == ws.profile_id

        anonymous_people = payload["anonymous_people"]
        assert isinstance(anonymous_people, list)
        assert anonymous_people
        first_person = anonymous_people[0]
        assert isinstance(first_person, dict)
        assert int(first_person["cover_observation_id"]) > 0
        seed_observations = first_person["seed_observations"]
        assert isinstance(seed_observations, list)
        assert seed_observations

        pending_clusters = payload["pending_clusters"]
        assert isinstance(pending_clusters, list)
        assert pending_clusters
        first_cluster = pending_clusters[0]
        assert isinstance(first_cluster, dict)
        diagnostics = first_cluster["diagnostics"]
        assert isinstance(diagnostics, dict)
        assert "distinct_photo_count" in diagnostics
        assert "quality_distribution" in diagnostics
        assert "external_margin" in diagnostics
        assert "reject_reason" in diagnostics
    finally:
        ws.close()


def test_identity_tuning_page_is_read_only_without_write_entries(tmp_path: Path) -> None:
    ws = build_identity_seed_workspace(tmp_path / "identity-tuning-readonly")
    try:
        ws.seed_edge_rule_challenge_case()
        ws.new_bootstrap_service().run_bootstrap(profile_id=ws.profile_id)

        client = TestClient(create_app(workspace=ws.root))
        response = client.get("/identity-tuning")
        assert response.status_code == 200
        html_text = response.text

        assert "<form" not in html_text.lower()
        assert "method=\"post\"" not in html_text.lower()
        assert "resolve-review" not in html_text
        assert "data-action=\"review-" not in html_text
        assert "/api/actions" not in html_text
    finally:
        ws.close()


def test_identity_tuning_page_renders_preview_images_for_results(tmp_path: Path) -> None:
    ws = build_identity_seed_workspace(tmp_path / "identity-tuning-preview-images")
    try:
        ws.seed_edge_rule_challenge_case()
        ws.new_bootstrap_service().run_bootstrap(profile_id=ws.profile_id)

        client = TestClient(create_app(workspace=ws.root))
        response = client.get("/identity-tuning")

        assert response.status_code == 200
        html_text = response.text
        payload = _extract_embedded_json(html_text)

        anonymous_people = payload["anonymous_people"]
        assert isinstance(anonymous_people, list)
        assert anonymous_people
        first_person = anonymous_people[0]
        assert isinstance(first_person, dict)
        cover_crop_url = first_person["cover_crop_url"]
        assert isinstance(cover_crop_url, str)
        assert cover_crop_url
        assert f'src="{cover_crop_url}"' in html_text

        seed_observations = first_person["seed_observations"]
        assert isinstance(seed_observations, list)
        assert seed_observations
        first_seed = seed_observations[0]
        assert isinstance(first_seed, dict)
        seed_crop_url = first_seed["crop_url"]
        assert isinstance(seed_crop_url, str)
        assert seed_crop_url
        assert f'src="{seed_crop_url}"' in html_text

        pending_clusters = payload["pending_clusters"]
        assert isinstance(pending_clusters, list)
        assert pending_clusters
        first_cluster = pending_clusters[0]
        assert isinstance(first_cluster, dict)
        representative_crop_url = first_cluster["representative_crop_url"]
        assert isinstance(representative_crop_url, str)
        assert representative_crop_url
        assert f'src="{representative_crop_url}"' in html_text

        distinct_photo_previews = first_cluster["distinct_photo_previews"]
        assert isinstance(distinct_photo_previews, list)
        expected_photo_rows = ws.conn.execute(
            """
            SELECT fo.photo_asset_id,
                   MIN(acm.face_observation_id) AS observation_id
            FROM auto_cluster_member AS acm
            JOIN face_observation AS fo
              ON fo.id = acm.face_observation_id
            WHERE acm.cluster_id = ?
            GROUP BY fo.photo_asset_id
            ORDER BY MIN(acm.face_observation_id) ASC, fo.photo_asset_id ASC
            """,
            (int(first_cluster["cluster_id"]),),
        ).fetchall()
        assert len(expected_photo_rows) == int(first_cluster["diagnostics"]["distinct_photo_count"])
        assert len(distinct_photo_previews) == len(expected_photo_rows)
        expected_photo_ids = [int(row["photo_asset_id"]) for row in expected_photo_rows]
        actual_photo_ids = [int(item["photo_asset_id"]) for item in distinct_photo_previews]
        assert actual_photo_ids == expected_photo_ids
        expected_preview_urls = [f"/api/photos/{photo_id}/preview" for photo_id in expected_photo_ids]
        actual_preview_urls = [str(item["preview_url"]) for item in distinct_photo_previews]
        assert actual_preview_urls == expected_preview_urls
        for preview_url in actual_preview_urls:
            assert f'src="{preview_url}"' in html_text
    finally:
        ws.close()


def test_identity_tuning_page_renders_library_wide_high_quality_observation_media(tmp_path: Path) -> None:
    ws = build_identity_seed_workspace(tmp_path / "identity-tuning-library-high-quality-observations")
    try:
        ws.seed_edge_rule_challenge_case()
        ws.new_bootstrap_service().run_bootstrap(profile_id=ws.profile_id)

        client = TestClient(create_app(workspace=ws.root))
        response = client.get("/identity-tuning")

        assert response.status_code == 200
        html_text = response.text
        payload = _extract_embedded_json(html_text)

        active_profile = payload["active_profile"]
        assert isinstance(active_profile, dict)
        high_quality_threshold = float(active_profile["high_quality_threshold"])

        high_quality_observations = payload["high_quality_observations"]
        assert isinstance(high_quality_observations, list)

        expected_rows = ws.conn.execute(
            """
            SELECT fo.id AS observation_id,
                   fo.photo_asset_id,
                   fo.quality_score
            FROM face_observation AS fo
            WHERE fo.active = 1
              AND fo.quality_score >= ?
            ORDER BY fo.quality_score DESC, fo.id ASC
            """,
            (high_quality_threshold,),
        ).fetchall()
        assert expected_rows
        assert len(high_quality_observations) == len(expected_rows)

        expected_observation_ids = [int(row["observation_id"]) for row in expected_rows]
        actual_observation_ids = [int(item["observation_id"]) for item in high_quality_observations]
        assert actual_observation_ids == expected_observation_ids

        for item, row in zip(high_quality_observations, expected_rows, strict=True):
            crop_url = f"/api/observations/{int(row['observation_id'])}/crop"
            preview_url = f"/api/photos/{int(row['photo_asset_id'])}/preview"
            assert str(item["crop_url"]) == crop_url
            assert str(item["preview_url"]) == preview_url
            assert float(item["quality_score"]) == float(row["quality_score"])
            assert f'src="{crop_url}"' in html_text
            assert f'src="{preview_url}"' in html_text
    finally:
        ws.close()


def test_identity_tuning_page_formats_quality_values_with_two_decimals(tmp_path: Path) -> None:
    ws = build_identity_seed_workspace(tmp_path / "identity-tuning-quality-format")
    try:
        ws.seed_edge_rule_challenge_case()
        ws.new_bootstrap_service().run_bootstrap(profile_id=ws.profile_id)
        ws.conn.execute(
            """
            UPDATE identity_threshold_profile
            SET high_quality_threshold = ?,
                trusted_seed_quality_threshold = ?
            WHERE id = ?
            """,
            (0.85555, 0.90555, ws.profile_id),
        )
        observation_row = ws.conn.execute(
            """
            SELECT id
            FROM face_observation
            WHERE active = 1
            ORDER BY id ASC
            LIMIT 1
            """
        ).fetchone()
        assert observation_row is not None
        ws.conn.execute(
            "UPDATE face_observation SET quality_score = ? WHERE id = ?",
            (0.87654, int(observation_row["id"])),
        )
        ws.conn.commit()

        client = TestClient(create_app(workspace=ws.root))
        response = client.get("/identity-tuning")

        assert response.status_code == 200
        payload = _extract_embedded_json(response.text)
        visible_html = _strip_embedded_json(response.text)
        pending_clusters = payload["pending_clusters"]
        assert isinstance(pending_clusters, list)
        assert pending_clusters
        first_cluster = pending_clusters[0]
        assert isinstance(first_cluster, dict)
        diagnostics = first_cluster["diagnostics"]
        assert isinstance(diagnostics, dict)
        quality_distribution = diagnostics["quality_distribution"]
        assert isinstance(quality_distribution, dict)
        assert "<dt>high_quality_threshold</dt><dd>0.86</dd>" in visible_html
        assert "<dt>trusted_seed_quality_threshold</dt><dd>0.91</dd>" in visible_html
        assert (
            "quality_distribution</dt><dd>"
            f"min {float(quality_distribution['min']):.2f} · "
            f"avg {float(quality_distribution['avg']):.2f} · "
            f"max {float(quality_distribution['max']):.2f}</dd>"
        ) in visible_html
        assert "quality 0.88" in visible_html
        assert "0.85555" not in visible_html
        assert "0.90555" not in visible_html
        assert "0.87654" not in visible_html
        assert "0.975" not in visible_html
    finally:
        ws.close()


def test_identity_tuning_page_batch_aggregate_matches_sql_baseline(tmp_path: Path) -> None:
    ws = build_identity_seed_workspace(tmp_path / "identity-tuning-aggregate")
    try:
        ws.seed_edge_rule_challenge_case()
        ws.new_bootstrap_service().run_bootstrap(profile_id=ws.profile_id)

        latest_batch_row = ws.conn.execute(
            """
            SELECT id
            FROM auto_cluster_batch
            WHERE batch_type = 'bootstrap'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        assert latest_batch_row is not None
        latest_batch_id = int(latest_batch_row["id"])
        baseline = ws.conn.execute(
            """
            SELECT COUNT(*) AS cluster_count,
                   SUM(CASE WHEN cluster_status = 'materialized' THEN 1 ELSE 0 END) AS materialized_count,
                   SUM(CASE WHEN cluster_status = 'review_pending' THEN 1 ELSE 0 END) AS review_pending_count,
                   SUM(CASE WHEN cluster_status = 'discarded' THEN 1 ELSE 0 END) AS discarded_count
            FROM auto_cluster
            WHERE batch_id = ?
            """,
            (latest_batch_id,),
        ).fetchone()
        assert baseline is not None

        client = TestClient(create_app(workspace=ws.root))
        response = client.get("/identity-tuning")
        assert response.status_code == 200
        payload = _extract_embedded_json(response.text)

        bootstrap_batch = payload["bootstrap_batch"]
        assert isinstance(bootstrap_batch, dict)
        assert int(bootstrap_batch["id"]) == latest_batch_id
        assert int(bootstrap_batch["cluster_count"]) == int(baseline["cluster_count"] or 0)
        assert int(bootstrap_batch["materialized_count"]) == int(baseline["materialized_count"] or 0)
        assert int(bootstrap_batch["review_pending_count"]) == int(baseline["review_pending_count"] or 0)
        assert int(bootstrap_batch["discarded_count"]) == int(baseline["discarded_count"] or 0)

        pending_clusters = payload["pending_clusters"]
        assert isinstance(pending_clusters, list)
        assert len(pending_clusters) == int(bootstrap_batch["review_pending_count"])
    finally:
        ws.close()


def test_identity_tuning_page_only_lists_latest_batch_anonymous_people(tmp_path: Path) -> None:
    ws = build_identity_seed_workspace(tmp_path / "identity-tuning-latest-anonymous")
    try:
        _, _, legacy_person_id = _insert_legacy_bootstrap_materialized_person(ws)
        ws.seed_materialize_happy_case()
        ws.new_bootstrap_service().run_bootstrap(profile_id=ws.profile_id)

        client = TestClient(create_app(workspace=ws.root))
        response = client.get("/identity-tuning")
        assert response.status_code == 200
        payload = _extract_embedded_json(response.text)

        anonymous_people = payload["anonymous_people"]
        assert isinstance(anonymous_people, list)
        person_ids = {int(item["person_id"]) for item in anonymous_people}
        assert legacy_person_id not in person_ids
    finally:
        ws.close()
