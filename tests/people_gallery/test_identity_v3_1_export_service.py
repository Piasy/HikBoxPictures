from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import html
import json
from pathlib import Path
import re
import sqlite3

from PIL import Image
import pytest

from hikbox_experiments.identity_v3_1.models import AssignParameters
from hikbox_experiments.identity_v3_1.export_service import IdentityV31ReportExportService
from hikbox_pictures.services.preview_artifact_service import PreviewArtifactService

from .fixtures_identity_v3_1_export import build_identity_v3_1_export_workspace


DETAIL_IDS = (
    "summary",
    "seed-identities",
    "overrides",
    "review-pending-clusters",
    "bucket-auto-assign",
    "bucket-review",
    "bucket-reject",
)


@dataclass(slots=True, frozen=True)
class _PreviewTrackRecord:
    kind: str
    observation_id: int | None
    photo_id: int | None
    artifact_path: Path
    source_path: Path | None


class TrackingPreviewArtifactService(PreviewArtifactService):
    def __init__(
        self,
        *,
        db_path: Path,
        workspace: Path,
        fail_context_observation_ids: set[int] | None = None,
    ) -> None:
        super().__init__(db_path=db_path, workspace=workspace)
        self.fail_context_observation_ids = set(fail_context_observation_ids or set())
        self.records: list[_PreviewTrackRecord] = []

    def ensure_crop(self, observation_id: int) -> str:
        artifact_path = Path(super().ensure_crop(int(observation_id)))
        self.records.append(
            _PreviewTrackRecord(
                kind="crop",
                observation_id=int(observation_id),
                photo_id=None,
                artifact_path=artifact_path,
                source_path=None,
            )
        )
        return str(artifact_path)

    def ensure_context(self, observation_id: int) -> str:
        if int(observation_id) in self.fail_context_observation_ids:
            raise RuntimeError(f"context asset forced failure for {int(observation_id)}")
        artifact_path = Path(super().ensure_context(int(observation_id)))
        self.records.append(
            _PreviewTrackRecord(
                kind="context",
                observation_id=int(observation_id),
                photo_id=None,
                artifact_path=artifact_path,
                source_path=None,
            )
        )
        return str(artifact_path)

    def ensure_photo_preview(self, *, photo_id: int, source_path: Path) -> str:
        artifact_path = Path(super().ensure_photo_preview(photo_id=int(photo_id), source_path=source_path))
        self.records.append(
            _PreviewTrackRecord(
                kind="preview",
                observation_id=None,
                photo_id=int(photo_id),
                artifact_path=artifact_path,
                source_path=Path(source_path),
            )
        )
        return str(artifact_path)


def _materialize_valid_source_images(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT p.id, p.library_source_id, p.primary_path, s.root_path
        FROM photo_asset AS p
        JOIN library_source AS s ON s.id = p.library_source_id
        ORDER BY p.id ASC
        """
    ).fetchall()
    for index, row in enumerate(rows, start=1):
        source_root = Path(str(row["root_path"])).expanduser().resolve()
        image_path = source_root / "identity-v3-1-export-input" / f"photo-{int(row['id'])}.jpg"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (640, 480), color=((index * 17) % 255, (index * 29) % 255, (index * 41) % 255))
        image.save(image_path, format="JPEG")
        conn.execute(
            "UPDATE photo_asset SET primary_path = ? WHERE id = ?",
            (str(image_path), int(row["id"])),
        )
    conn.commit()


def _normalize_text(raw: str) -> str:
    text = html.unescape(raw)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _details_visible_text_map(page_html: str) -> dict[str, str]:
    output: dict[str, str] = {}
    for detail_id in DETAIL_IDS:
        pattern = re.compile(
            rf'<details[^>]*id="{re.escape(detail_id)}"[^>]*>(.*?)</details>',
            re.IGNORECASE | re.DOTALL,
        )
        match = pattern.search(page_html)
        assert match is not None, f"缺少 details: {detail_id}"
        output[detail_id] = _normalize_text(match.group(1))
    return output


def _load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_html(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _get_record_map(records: list[_PreviewTrackRecord]) -> tuple[dict[int, _PreviewTrackRecord], dict[int, _PreviewTrackRecord], dict[int, _PreviewTrackRecord]]:
    crop_map: dict[int, _PreviewTrackRecord] = {}
    context_map: dict[int, _PreviewTrackRecord] = {}
    preview_map: dict[int, _PreviewTrackRecord] = {}
    for record in records:
        if record.kind == "crop":
            assert record.observation_id is not None
            crop_map[record.observation_id] = record
            continue
        if record.kind == "context":
            assert record.observation_id is not None
            context_map[record.observation_id] = record
            continue
        assert record.kind == "preview"
        assert record.photo_id is not None
        preview_map[record.photo_id] = record
    return crop_map, context_map, preview_map


def test_export_service_outputs_bundle_structure_real_assets_and_rich_html(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "identity-v3-1-export-bundle")
    try:
        _materialize_valid_source_images(ws.conn)
        preview_service = TrackingPreviewArtifactService(
            db_path=ws.root / ".hikbox" / "library.db",
            workspace=ws.root,
        )
        service = IdentityV31ReportExportService(
            workspace=ws.root,
            preview_artifact_service=preview_service,
        )
        output_root = tmp_path / "bundle-root"

        result = service.export(output_root=output_root)

        output_dir = result["output_dir"]
        index_path = result["index_path"]
        manifest_path = result["manifest_path"]
        assert output_dir.parent == output_root
        assert output_dir != output_root
        assert index_path == output_dir / "index.html"
        assert manifest_path == output_dir / "manifest.json"
        assert (output_dir / "assets").is_dir()
        assert (output_dir / "assets" / "observations").is_dir()

        manifest = _load_json(manifest_path)
        page_html = _load_html(index_path)
        details_text = _details_visible_text_map(page_html)

        assert set(re.findall(r'<details\s+id="([^"]+)"', page_html)) == set(DETAIL_IDS)
        assert "data-top-candidate-rank" in page_html

        required_top_keys = {
            "workspace",
            "db_path",
            "generated_at",
            "base_run",
            "snapshot",
            "parameters",
            "seed_identities",
            "pending_clusters",
            "assignment_summary",
            "warnings",
            "errors",
            "assignments",
        }
        assert required_top_keys.issubset(set(manifest))

        expected_parameter_keys = {
            "base_run_id",
            "assign_source",
            "top_k",
            "auto_max_distance",
            "review_max_distance",
            "min_margin",
            "promote_cluster_ids",
            "disable_seed_cluster_ids",
        }
        assert expected_parameter_keys.issubset(set(manifest["parameters"]))
        assert int(manifest["parameters"]["base_run_id"]) == int(manifest["base_run"]["id"])

        summary = manifest["assignment_summary"]
        assert int(summary["candidate_count"]) == int(summary["auto_assign_count"]) + int(summary["review_count"]) + int(
            summary["reject_count"]
        )

        invalid_seed_rows = [item for item in manifest["seed_identities"] if not bool(item["valid"])]
        assert invalid_seed_rows
        invalid_cluster_ids = {int(item["source_cluster_id"]) for item in invalid_seed_rows}
        error_cluster_ids = {
            int(item["cluster_id"]) for item in manifest["errors"] if str(item.get("code", "")) == "invalid_seed_prototype"
        }
        assert invalid_cluster_ids.issubset(error_cluster_ids)
        for item in invalid_seed_rows:
            assert item["error_code"] == "invalid_seed_prototype"
            assert item["error_message"]

        assert manifest["pending_clusters"]
        for cluster in manifest["pending_clusters"]:
            assert {
                "cluster_id",
                "retained_member_count",
                "distinct_photo_count",
                "representative_count",
                "retained_count",
                "excluded_count",
                "promoted_to_seed",
            }.issubset(set(cluster))

        assignments = manifest["assignments"]
        assert assignments
        first_top_k_assignment = next(item for item in assignments if item["top_candidates"])
        top_candidates = first_top_k_assignment["top_candidates"]
        assert [int(item["rank"]) for item in top_candidates] == list(range(1, len(top_candidates) + 1))
        for item in top_candidates:
            assert f'data-top-candidate-rank="{int(item["rank"])}"' in page_html

        assert "workspace" in details_text["summary"]
        assert "seed" in details_text["seed-identities"]
        assert "promoted" in details_text["overrides"]
        assert "retained" in details_text["review-pending-clusters"]
        assert "auto_assign" in details_text["bucket-auto-assign"]
        assert "review" in details_text["bucket-review"]
        assert "reject" in details_text["bucket-reject"]
        assert "none" in details_text["overrides"]
        assert str(ws.cluster_ids["seed_invalid"]) in details_text["overrides"]
        assert str(ws.cluster_ids["seed_invalid"]) in details_text["summary"]
        assert "invalid seed" in details_text["summary"]

        per_observation_kind_counts = Counter(
            (record.kind, int(record.observation_id))
            for record in preview_service.records
            if record.observation_id is not None
        )
        assert per_observation_kind_counts
        assert max(per_observation_kind_counts.values()) == 1

        crop_map, context_map, preview_map = _get_record_map(preview_service.records)
        for assignment in assignments:
            observation_id = int(assignment["observation_id"])
            photo_id = int(assignment["photo_id"])
            obs_assets_dir = output_dir / "assets" / "observations" / f"obs-{observation_id}"
            assert obs_assets_dir.is_dir()

            assets = assignment["assets"]
            for asset_key, relative_path in assets.items():
                if relative_path is None:
                    continue
                bundle_asset_path = output_dir / str(relative_path)
                assert bundle_asset_path.is_file()
                if asset_key == "crop":
                    assert observation_id in crop_map
                    assert bundle_asset_path.read_bytes() == crop_map[observation_id].artifact_path.read_bytes()
                elif asset_key == "context":
                    assert observation_id in context_map
                    assert bundle_asset_path.read_bytes() == context_map[observation_id].artifact_path.read_bytes()
                elif asset_key == "preview":
                    assert photo_id in preview_map
                    assert bundle_asset_path.read_bytes() == preview_map[photo_id].artifact_path.read_bytes()
                else:
                    pytest.fail(f"未知资产 key: {asset_key}")
    finally:
        ws.close()


def test_export_service_result_changes_with_base_run_and_overrides(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "identity-v3-1-export-diff")
    try:
        _materialize_valid_source_images(ws.conn)
        service = IdentityV31ReportExportService(workspace=ws.root)
        output_root = tmp_path / "bundle-root"

        default_result = service.export(output_root=output_root)
        latest_run_result = service.export(base_run_id=ws.latest_non_target_run_id, output_root=output_root)
        override_result = service.export(
            base_run_id=ws.base_run_id,
            promote_cluster_ids={ws.cluster_ids["pending_promotable"]},
            disable_seed_cluster_ids={ws.cluster_ids["seed_primary"]},
            output_root=output_root,
        )

        default_manifest = _load_json(default_result["manifest_path"])
        latest_manifest = _load_json(latest_run_result["manifest_path"])
        override_manifest = _load_json(override_result["manifest_path"])

        assert int(default_manifest["base_run"]["id"]) != int(latest_manifest["base_run"]["id"])
        assert int(latest_manifest["base_run"]["id"]) == int(ws.latest_non_target_run_id)
        assert default_manifest["assignments"] != latest_manifest["assignments"]

        default_seed_clusters = {int(item["source_cluster_id"]) for item in default_manifest["seed_identities"]}
        override_seed_clusters = {int(item["source_cluster_id"]) for item in override_manifest["seed_identities"]}
        assert ws.cluster_ids["pending_promotable"] in override_seed_clusters
        assert default_seed_clusters != override_seed_clusters
        assert default_manifest["assignments"] != override_manifest["assignments"]
    finally:
        ws.close()


def test_export_service_asset_failure_only_warns_and_still_exports(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "identity-v3-1-export-warning")
    try:
        _materialize_valid_source_images(ws.conn)
        forced_observation_id = int(ws.observation_ids["attachment_auto"])
        preview_service = TrackingPreviewArtifactService(
            db_path=ws.root / ".hikbox" / "library.db",
            workspace=ws.root,
            fail_context_observation_ids={forced_observation_id},
        )
        service = IdentityV31ReportExportService(
            workspace=ws.root,
            preview_artifact_service=preview_service,
        )

        result = service.export(output_root=tmp_path / "bundle-root")
        manifest = _load_json(result["manifest_path"])

        assert result["index_path"].is_file()
        assert result["manifest_path"].is_file()
        assert manifest["warnings"]
        assert any("context" in str(item).lower() for item in manifest["warnings"])
    finally:
        ws.close()


def test_export_service_assign_parameters_really_change_export_result(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "identity-v3-1-export-assign-params")
    try:
        _materialize_valid_source_images(ws.conn)
        service = IdentityV31ReportExportService(workspace=ws.root)
        output_root = tmp_path / "bundle-root"

        top_k_1_result = service.export(
            output_root=output_root,
            assign_parameters=AssignParameters(top_k=1),
        )
        top_k_3_result = service.export(
            output_root=output_root,
            assign_parameters=AssignParameters(top_k=3),
        )

        manifest_k1 = _load_json(top_k_1_result["manifest_path"])
        manifest_k3 = _load_json(top_k_3_result["manifest_path"])
        assert int(manifest_k1["parameters"]["top_k"]) == 1
        assert int(manifest_k3["parameters"]["top_k"]) == 3

        by_observation_k1 = {
            int(item["observation_id"]): item
            for item in manifest_k1["assignments"]
            if item["top_candidates"]
        }
        by_observation_k3 = {
            int(item["observation_id"]): item
            for item in manifest_k3["assignments"]
            if item["top_candidates"]
        }
        shared_observation_ids = sorted(set(by_observation_k1) & set(by_observation_k3))
        assert shared_observation_ids

        changed_count = 0
        for observation_id in shared_observation_ids:
            top1 = by_observation_k1[observation_id]["top_candidates"]
            top3 = by_observation_k3[observation_id]["top_candidates"]
            assert len(top1) <= 1
            assert len(top3) <= 3
            if len(top3) > len(top1):
                changed_count += 1
        assert changed_count > 0
    finally:
        ws.close()


def test_export_service_manifest_is_strict_json_when_single_seed(tmp_path: Path) -> None:
    ws = build_identity_v3_1_export_workspace(tmp_path / "identity-v3-1-export-single-seed")
    try:
        _materialize_valid_source_images(ws.conn)
        service = IdentityV31ReportExportService(workspace=ws.root)
        result = service.export(
            output_root=tmp_path / "bundle-root",
            disable_seed_cluster_ids={ws.cluster_ids["seed_fallback"]},
        )

        manifest_text = result["manifest_path"].read_text(encoding="utf-8")
        assert "Infinity" not in manifest_text
        assert "-Infinity" not in manifest_text
        assert "NaN" not in manifest_text

        def _strict_constant(value: str) -> None:
            pytest.fail(f"manifest.json 出现非标准 JSON 常量: {value}")

        manifest = json.loads(manifest_text, parse_constant=_strict_constant)
        assert manifest["assignments"]
        assert any(item["second_best_distance"] is None for item in manifest["assignments"])
    finally:
        ws.close()
