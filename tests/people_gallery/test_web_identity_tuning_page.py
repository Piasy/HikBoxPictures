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

        payload = _extract_embedded_json(html_text)
        assert "active_profile" in payload
        assert "bootstrap_batch" in payload
        assert "anonymous_people" in payload
        assert "pending_clusters" in payload

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
