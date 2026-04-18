from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import html
import json
import math
from pathlib import Path
from shutil import copy2
from typing import Any

from hikbox_pictures.services.preview_artifact_service import PreviewArtifactService
from hikbox_pictures.workspace import load_workspace_paths

from .assignment_service import IdentityV31AssignmentService
from .models import AssignParameters, AssignmentRecord, ClusterRecord, SeedIdentityRecord
from .query_service import IdentityV31QueryService


class IdentityV31ReportExportService:
    def __init__(
        self,
        workspace: Path,
        *,
        query_service: IdentityV31QueryService | None = None,
        assignment_service: IdentityV31AssignmentService | None = None,
        preview_artifact_service: PreviewArtifactService | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.workspace_paths = load_workspace_paths(self.workspace)
        self.query_service = query_service or IdentityV31QueryService(self.workspace)
        self.assignment_service = assignment_service or IdentityV31AssignmentService()
        self.preview_artifact_service = preview_artifact_service or PreviewArtifactService(
            db_path=self.workspace_paths.db_path,
            workspace=self.workspace,
        )

    def export(
        self,
        *,
        base_run_id: int | None = None,
        promote_cluster_ids: set[int] | None = None,
        disable_seed_cluster_ids: set[int] | None = None,
        assign_parameters: AssignParameters | None = None,
        output_root: Path,
    ) -> dict[str, Path]:
        now = datetime.now()
        output_dir = Path(output_root).expanduser().resolve() / now.strftime("%Y%m%d-%H%M%S-%f")
        output_dir.mkdir(parents=True, exist_ok=True)
        index_path = output_dir / "index.html"
        manifest_path = output_dir / "manifest.json"

        base_params = assign_parameters or AssignParameters()
        effective_base_run_id = int(base_run_id) if base_run_id is not None else base_params.base_run_id
        effective_promote = (
            {int(item) for item in promote_cluster_ids}
            if promote_cluster_ids is not None
            else {int(item) for item in base_params.promote_cluster_ids}
        )
        effective_disable = (
            {int(item) for item in disable_seed_cluster_ids}
            if disable_seed_cluster_ids is not None
            else {int(item) for item in base_params.disable_seed_cluster_ids}
        )
        effective_params = AssignParameters(
            base_run_id=effective_base_run_id,
            assign_source=base_params.assign_source,
            top_k=base_params.top_k,
            auto_max_distance=base_params.auto_max_distance,
            review_max_distance=base_params.review_max_distance,
            min_margin=base_params.min_margin,
            promote_cluster_ids=tuple(sorted(effective_promote)),
            disable_seed_cluster_ids=tuple(sorted(effective_disable)),
        ).validate()

        query_context = self.query_service.load_report_context(
            base_run_id=effective_base_run_id,
            assign_parameters=effective_params,
        )
        seed_result = self.assignment_service.build_seed_identities(
            clusters=query_context.clusters,
            promote_cluster_ids=effective_promote,
            disable_seed_cluster_ids=effective_disable,
        )
        assignment_evaluation = self.assignment_service.evaluate_assignments(
            query_context=query_context,
            seed_result=seed_result,
            assign_parameters=effective_params,
        )

        warnings: list[str] = list(query_context.warnings)
        observation_meta = self._collect_observation_meta(query_context.clusters, query_context.candidate_observations)
        observation_ids = self._collect_export_observation_ids(
            clusters=query_context.clusters,
            seeds=[
                *seed_result.valid_seeds_by_cluster.values(),
                *seed_result.invalid_seeds,
            ],
            assignments=assignment_evaluation.assignments,
        )
        assets_by_observation = self._export_assets(
            observation_ids=observation_ids,
            observation_meta=observation_meta,
            output_dir=output_dir,
            warnings=warnings,
        )

        seed_identities = self._serialize_seed_identities(
            valid_seeds=seed_result.valid_seeds_by_cluster,
            invalid_seeds=seed_result.invalid_seeds,
        )
        pending_clusters = self._serialize_pending_clusters(
            clusters=query_context.clusters,
            promote_cluster_ids=effective_promote,
        )
        assignments = self._serialize_assignments(
            assignments=assignment_evaluation.assignments,
            assets_by_observation=assets_by_observation,
        )
        summary = asdict(assignment_evaluation.summary)
        resolved_base_run_id = int(query_context.base_run.id)
        manifest: dict[str, Any] = {
            "workspace": str(self.workspace),
            "db_path": str(self.workspace_paths.db_path),
            "generated_at": now.isoformat(),
            "base_run": asdict(query_context.base_run),
            "snapshot": asdict(query_context.snapshot),
            "parameters": {
                "base_run_id": resolved_base_run_id,
                "assign_source": effective_params.assign_source,
                "top_k": effective_params.top_k,
                "auto_max_distance": effective_params.auto_max_distance,
                "review_max_distance": effective_params.review_max_distance,
                "min_margin": effective_params.min_margin,
                "promote_cluster_ids": sorted(effective_promote),
                "disable_seed_cluster_ids": sorted(effective_disable),
            },
            "seed_identities": seed_identities,
            "pending_clusters": pending_clusters,
            "assignment_summary": summary,
            "warnings": warnings,
            "errors": list(seed_result.errors),
            "assignments": assignments,
        }
        json_safe_manifest = self._json_safe(manifest)
        index_path.write_text(
            self._build_html(manifest=json_safe_manifest, assets_by_observation=assets_by_observation),
            encoding="utf-8",
        )
        manifest_path.write_text(
            json.dumps(json_safe_manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        return {
            "output_dir": output_dir,
            "index_path": index_path,
            "manifest_path": manifest_path,
        }

    def _json_safe(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self._json_safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._json_safe(item) for item in value]
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value

    def _collect_observation_meta(
        self,
        clusters: list[ClusterRecord],
        candidates: list[Any],
    ) -> dict[int, dict[str, Any]]:
        observation_meta: dict[int, dict[str, Any]] = {}
        for cluster in clusters:
            for member in cluster.members:
                observation_meta[int(member.observation_id)] = {
                    "photo_id": int(member.photo_id),
                    "primary_path": member.primary_path,
                }
        for candidate in candidates:
            observation_meta[int(candidate.observation_id)] = {
                "photo_id": int(candidate.photo_id),
                "primary_path": candidate.primary_path,
            }
        return observation_meta

    def _collect_export_observation_ids(
        self,
        *,
        clusters: list[ClusterRecord],
        seeds: list[SeedIdentityRecord],
        assignments: list[AssignmentRecord],
    ) -> list[int]:
        observation_ids: set[int] = set()
        for seed in seeds:
            observation_ids.update(int(item) for item in seed.member_observation_ids)
            if seed.representative_observation_id is not None:
                observation_ids.add(int(seed.representative_observation_id))
        for cluster in clusters:
            if cluster.resolution_state != "review_pending":
                continue
            observation_ids.update(int(member.observation_id) for member in cluster.members)
            if cluster.representative_observation_id is not None:
                observation_ids.add(int(cluster.representative_observation_id))
        for assignment in assignments:
            observation_ids.add(int(assignment.observation_id))
        return sorted(observation_ids)

    def _export_assets(
        self,
        *,
        observation_ids: list[int],
        observation_meta: dict[int, dict[str, Any]],
        output_dir: Path,
        warnings: list[str],
    ) -> dict[int, dict[str, str | None]]:
        assets_root = output_dir / "assets" / "observations"
        assets_root.mkdir(parents=True, exist_ok=True)
        output: dict[int, dict[str, str | None]] = {}
        for observation_id in observation_ids:
            output[observation_id] = {"crop": None, "context": None, "preview": None}
            meta = observation_meta.get(int(observation_id))
            if meta is None:
                warnings.append(f"asset export skipped: observation={observation_id}, reason=missing_meta")
                continue
            obs_dir = assets_root / f"obs-{observation_id}"
            obs_dir.mkdir(parents=True, exist_ok=True)
            photo_id = meta.get("photo_id")
            primary_path = meta.get("primary_path")
            output[observation_id]["crop"] = self._copy_artifact(
                ensure_call=lambda: self.preview_artifact_service.ensure_crop(int(observation_id)),
                output_path=obs_dir / "crop.jpg",
                relative_path=Path("assets") / "observations" / f"obs-{observation_id}" / "crop.jpg",
                warnings=warnings,
                error_prefix=f"asset crop failed: observation={observation_id}",
            )
            output[observation_id]["context"] = self._copy_artifact(
                ensure_call=lambda: self.preview_artifact_service.ensure_context(int(observation_id)),
                output_path=obs_dir / "context.jpg",
                relative_path=Path("assets") / "observations" / f"obs-{observation_id}" / "context.jpg",
                warnings=warnings,
                error_prefix=f"asset context failed: observation={observation_id}",
            )
            if photo_id is None or primary_path in (None, ""):
                warnings.append(f"asset preview skipped: observation={observation_id}, reason=missing_photo_or_path")
                continue
            output[observation_id]["preview"] = self._copy_artifact(
                ensure_call=lambda: self.preview_artifact_service.ensure_photo_preview(
                    photo_id=int(photo_id),
                    source_path=Path(str(primary_path)),
                ),
                output_path=obs_dir / "preview.jpg",
                relative_path=Path("assets") / "observations" / f"obs-{observation_id}" / "preview.jpg",
                warnings=warnings,
                error_prefix=f"asset preview failed: observation={observation_id}, photo_id={photo_id}",
            )
        return output

    @staticmethod
    def _copy_artifact(
        *,
        ensure_call,
        output_path: Path,
        relative_path: Path,
        warnings: list[str],
        error_prefix: str,
    ) -> str | None:
        try:
            source = Path(str(ensure_call())).expanduser().resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            copy2(source, output_path)
            return str(relative_path)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{error_prefix}, error={exc.__class__.__name__}: {exc}")
            return None

    def _serialize_seed_identities(
        self,
        *,
        valid_seeds: dict[int, SeedIdentityRecord],
        invalid_seeds: list[SeedIdentityRecord],
    ) -> list[dict[str, Any]]:
        rows = [*valid_seeds.values(), *invalid_seeds]
        rows.sort(key=lambda item: int(item.source_cluster_id))
        return [asdict(item) for item in rows]

    def _serialize_pending_clusters(
        self,
        *,
        clusters: list[ClusterRecord],
        promote_cluster_ids: set[int],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for cluster in sorted(clusters, key=lambda item: int(item.cluster_id)):
            if cluster.resolution_state != "review_pending":
                continue
            rows.append(
                {
                    "cluster_id": int(cluster.cluster_id),
                    "retained_member_count": int(cluster.retained_member_count),
                    "distinct_photo_count": int(cluster.distinct_photo_count),
                    "representative_count": int(cluster.representative_count),
                    "retained_count": int(cluster.retained_count),
                    "excluded_count": int(cluster.excluded_count),
                    "promoted_to_seed": int(cluster.cluster_id) in promote_cluster_ids,
                }
            )
        return rows

    def _serialize_assignments(
        self,
        *,
        assignments: list[AssignmentRecord],
        assets_by_observation: dict[int, dict[str, str | None]],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in assignments:
            assets = assets_by_observation.get(int(item.observation_id), item.assets)
            missing_assets = [key for key, value in assets.items() if value is None]
            rows.append(
                {
                    "observation_id": int(item.observation_id),
                    "photo_id": int(item.photo_id),
                    "source_kind": item.source_kind,
                    "source_cluster_id": item.source_cluster_id,
                    "best_identity_id": item.best_identity_id,
                    "best_cluster_id": item.best_cluster_id,
                    "best_distance": item.best_distance,
                    "second_best_distance": item.second_best_distance,
                    "distance_margin": item.distance_margin,
                    "same_photo_conflict": bool(item.same_photo_conflict),
                    "decision": item.decision,
                    "reason_code": item.reason_code,
                    "top_candidates": [asdict(candidate) for candidate in item.top_candidates],
                    "assets": assets,
                    "missing_assets": missing_assets,
                }
            )
        return rows

    def _build_html(
        self,
        *,
        manifest: dict[str, Any],
        assets_by_observation: dict[int, dict[str, str | None]],
    ) -> str:
        summary = manifest["assignment_summary"]
        parameters = manifest["parameters"]
        promoted = ",".join(str(item) for item in parameters["promote_cluster_ids"]) or "none"
        disabled = ",".join(str(item) for item in parameters["disable_seed_cluster_ids"]) or "none"
        invalid_ids = ",".join(
            str(item["source_cluster_id"])
            for item in manifest["seed_identities"]
            if not bool(item.get("valid", False))
        ) or "none"
        buckets: dict[str, list[dict[str, Any]]] = {
            "auto_assign": [],
            "review": [],
            "reject": [],
        }
        for item in manifest["assignments"]:
            decision = str(item["decision"])
            buckets.setdefault(decision, []).append(item)

        return (
            "<!DOCTYPE html>\n"
            "<html lang=\"zh-CN\">\n"
            "<head>\n"
            "  <meta charset=\"utf-8\">\n"
            "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "  <title>Identity v3.1 Offline Export</title>\n"
            "  <style>"
            "body{font-family:system-ui,-apple-system,BlinkMacSystemFont,sans-serif;margin:16px;line-height:1.5;}"
            "details{margin:12px 0;padding:10px;border:1px solid #ddd;border-radius:8px;background:#fff;}"
            "summary{cursor:pointer;font-weight:600;}"
            ".card{margin:8px 0;padding:8px;border:1px solid #eee;border-radius:6px;background:#fafafa;}"
            ".meta{color:#444;font-size:13px;}"
            ".grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px;}"
            "img{max-width:100%;height:auto;display:block;border-radius:4px;border:1px solid #ddd;}"
            "ol{margin:6px 0 0 18px;padding:0;}"
            "code{background:#f1f1f1;padding:0 4px;border-radius:4px;}"
            "</style>\n"
            "</head>\n"
            "<body>\n"
            f"{self._render_summary(manifest)}\n"
            f"{self._render_seed_identities(manifest, assets_by_observation=assets_by_observation)}\n"
            f"{self._render_overrides(promoted=promoted, disabled=disabled, invalid_ids=invalid_ids)}\n"
            f"{self._render_pending_clusters(manifest)}\n"
            f"{self._render_bucket('auto_assign', buckets.get('auto_assign', []), summary)}\n"
            f"{self._render_bucket('review', buckets.get('review', []), summary)}\n"
            f"{self._render_bucket('reject', buckets.get('reject', []), summary)}\n"
            "</body>\n"
            "</html>\n"
        )

    def _render_summary(self, manifest: dict[str, Any]) -> str:
        parameters = manifest["parameters"]
        summary = manifest["assignment_summary"]
        invalid_seed_cluster_ids = [
            int(item["source_cluster_id"])
            for item in manifest["seed_identities"]
            if not bool(item.get("valid", False))
        ]
        invalid_seed_summary = (
            ",".join(str(item) for item in invalid_seed_cluster_ids)
            if invalid_seed_cluster_ids
            else "none"
        )
        invalid_seed_error_summaries = [
            f"cluster={int(item['cluster_id'])}:{str(item['code'])}"
            for item in manifest["errors"]
            if str(item.get("code", "")) == "invalid_seed_prototype" and item.get("cluster_id") is not None
        ]
        invalid_seed_error_text = "; ".join(invalid_seed_error_summaries) if invalid_seed_error_summaries else "none"
        return (
            "<details id=\"summary\" open "
            f"data-summary-workspace=\"{html.escape(str(manifest['workspace']))}\" "
            f"data-summary-base-run-id=\"{int(manifest['base_run']['id'])}\" "
            f"data-summary-snapshot-id=\"{int(manifest['snapshot']['id'])}\" "
            f"data-summary-generated-at=\"{html.escape(str(manifest['generated_at']))}\" "
            f"data-summary-assign-source=\"{html.escape(str(parameters['assign_source']))}\" "
            f"data-summary-auto-assign-count=\"{int(summary['auto_assign_count'])}\" "
            f"data-summary-review-count=\"{int(summary['review_count'])}\" "
            f"data-summary-reject-count=\"{int(summary['reject_count'])}\" "
            f"data-summary-warning-count=\"{len(manifest['warnings'])}\" "
            f"data-summary-error-count=\"{len(manifest['errors'])}\" "
            f"data-summary-invalid-seed-cluster-ids=\"{html.escape(invalid_seed_summary)}\">"
            "<summary>summary</summary>"
            f"<p>workspace: <code>{html.escape(str(manifest['workspace']))}</code></p>"
            f"<p>base run: {int(manifest['base_run']['id'])}, snapshot: {int(manifest['snapshot']['id'])}, generated at: {html.escape(str(manifest['generated_at']))}</p>"
            f"<p>参数摘要: source={html.escape(str(parameters['assign_source']))}, top_k={int(parameters['top_k'])}, auto_max_distance={float(parameters['auto_max_distance']):.3f}, review_max_distance={float(parameters['review_max_distance']):.3f}, min_margin={float(parameters['min_margin']):.3f}</p>"
            f"<p>seed identities: {len(manifest['seed_identities'])}, auto_assign/review/reject: {int(summary['auto_assign_count'])}/{int(summary['review_count'])}/{int(summary['reject_count'])}</p>"
            f"<p>invalid seed clusters: {html.escape(invalid_seed_summary)} | invalid seed errors: {html.escape(invalid_seed_error_text)}</p>"
            f"<p>warnings/errors: {len(manifest['warnings'])}/{len(manifest['errors'])}</p>"
            "</details>"
        )

    def _render_seed_identities(
        self,
        manifest: dict[str, Any],
        *,
        assets_by_observation: dict[int, dict[str, str | None]],
    ) -> str:
        cards: list[str] = []
        for seed in manifest["seed_identities"]:
            representative_obs = seed.get("representative_observation_id")
            representative_assets = (
                assets_by_observation.get(int(representative_obs))
                if representative_obs not in (None, "")
                else None
            )
            crop_src = representative_assets.get("crop") if representative_assets else None
            context_src = representative_assets.get("context") if representative_assets else None
            member_ids = [int(item) for item in seed.get("member_observation_ids", [])]
            cards.append(
                "<article class=\"card\" "
                f"data-seed-cluster-id=\"{int(seed['source_cluster_id'])}\" "
                f"data-seed-resolution-state=\"{html.escape(str(seed['resolution_state']))}\" "
                f"data-seed-member-count=\"{int(seed['seed_member_count'])}\" "
                f"data-seed-fallback-used=\"{str(bool(seed['fallback_used'])).lower()}\" "
                f"data-seed-representative-crop-src=\"{html.escape(str(crop_src or 'none'))}\" "
                f"data-seed-representative-context-src=\"{html.escape(str(context_src or 'none'))}\">"
                f"<p>seed cluster #{int(seed['source_cluster_id'])} | resolution={html.escape(str(seed['resolution_state']))} | prototype members={int(seed['seed_member_count'])} | fallback={str(bool(seed['fallback_used'])).lower()}</p>"
                f"<p>valid={str(bool(seed['valid'])).lower()}, error_code={html.escape(str(seed['error_code'] or 'none'))}, error_message={html.escape(str(seed['error_message'] or 'none'))}</p>"
                "<p class=\"meta\">members: "
                + ", ".join(
                    f"<span data-seed-member-observation-id=\"{obs_id}\">{obs_id}</span>"
                    for obs_id in member_ids
                )
                + "</p>"
                + (f"<img src=\"{html.escape(str(crop_src))}\" alt=\"crop\">" if crop_src else "<p>representative crop: none</p>")
                + (
                    f"<img src=\"{html.escape(str(context_src))}\" alt=\"context\">"
                    if context_src
                    else "<p>representative context: none</p>"
                )
                + "</article>"
            )
        return "<details id=\"seed-identities\"><summary>seed-identities</summary>" + "".join(cards) + "</details>"

    @staticmethod
    def _render_overrides(*, promoted: str, disabled: str, invalid_ids: str) -> str:
        return (
            "<details id=\"overrides\">"
            "<summary>overrides</summary>"
            f"<p data-overrides-promoted-cluster-ids=\"{html.escape(promoted)}\">promoted cluster ids: {html.escape(promoted)}</p>"
            f"<p data-overrides-disabled-seed-cluster-ids=\"{html.escape(disabled)}\">disabled seed cluster ids: {html.escape(disabled)}</p>"
            f"<p data-overrides-invalid-prototype-cluster-ids=\"{html.escape(invalid_ids)}\">invalid prototype cluster ids: {html.escape(invalid_ids)}</p>"
            "</details>"
        )

    def _render_pending_clusters(self, manifest: dict[str, Any]) -> str:
        cards: list[str] = []
        for item in manifest["pending_clusters"]:
            cards.append(
                "<article class=\"card\" "
                f"data-pending-cluster-id=\"{int(item['cluster_id'])}\" "
                f"data-pending-retained-member-count=\"{int(item['retained_member_count'])}\" "
                f"data-pending-distinct-photo-count=\"{int(item['distinct_photo_count'])}\" "
                f"data-pending-representative-count=\"{int(item['representative_count'])}\" "
                f"data-pending-retained-count=\"{int(item['retained_count'])}\" "
                f"data-pending-excluded-count=\"{int(item['excluded_count'])}\" "
                f"data-pending-promoted-to-seed=\"{str(bool(item['promoted_to_seed'])).lower()}\">"
                f"<p>cluster #{int(item['cluster_id'])}: retained_member_count={int(item['retained_member_count'])}, distinct_photo_count={int(item['distinct_photo_count'])}</p>"
                f"<p>representative/retained/excluded={int(item['representative_count'])}/{int(item['retained_count'])}/{int(item['excluded_count'])}, promoted_to_seed={str(bool(item['promoted_to_seed'])).lower()}</p>"
                "</article>"
            )
        return "<details id=\"review-pending-clusters\"><summary>review-pending-clusters</summary>" + "".join(cards) + "</details>"

    def _render_bucket(self, bucket_id: str, assignments: list[dict[str, Any]], summary: dict[str, Any]) -> str:
        key = f"{bucket_id}_count"
        count = int(summary.get(key, len(assignments)))
        html_bucket_id = bucket_id.replace("_", "-")
        cards = "".join(self._render_assignment_card(item) for item in assignments)
        return (
            f"<details id=\"bucket-{html_bucket_id}\">"
            f"<summary>bucket-{html_bucket_id}</summary>"
            f"<div data-bucket-id=\"{html.escape(bucket_id)}\" data-bucket-count=\"{count}\">"
            f"<p>{bucket_id} count: {count}</p>"
            f"{cards}"
            "</div>"
            "</details>"
        )

    def _render_assignment_card(self, item: dict[str, Any]) -> str:
        assets = item["assets"]
        top_candidates = item.get("top_candidates", [])
        top_candidate_html = "".join(
            "<li "
            f"data-top-candidate-rank=\"{int(candidate['rank'])}\" "
            f"data-cluster-id=\"{int(candidate['cluster_id'])}\" "
            f"data-distance=\"{float(candidate['distance']):.6f}\">"
            f"rank={int(candidate['rank'])}, cluster={int(candidate['cluster_id'])}, "
            f"identity={html.escape(str(candidate['identity_id']))}, distance={float(candidate['distance']):.6f}"
            "</li>"
            for candidate in top_candidates
        )
        return (
            "<article class=\"card\" "
            f"data-assignment-observation-id=\"{int(item['observation_id'])}\" "
            f"data-assignment-photo-id=\"{int(item['photo_id'])}\" "
            f"data-assignment-source-kind=\"{html.escape(str(item['source_kind']))}\" "
            f"data-assignment-best-cluster-id=\"{html.escape(str(item['best_cluster_id']))}\" "
            f"data-assignment-distance-margin=\"{html.escape(str(item['distance_margin']))}\" "
            f"data-assignment-reason-code=\"{html.escape(str(item['reason_code']))}\">"
            "<div class=\"grid\">"
            + (
                f"<img src=\"{html.escape(str(assets['crop']))}\" alt=\"crop\">"
                if assets.get("crop")
                else "<p>crop: none</p>"
            )
            + (
                f"<img src=\"{html.escape(str(assets['context']))}\" alt=\"context\">"
                if assets.get("context")
                else (
                    f"<img src=\"{html.escape(str(assets['preview']))}\" alt=\"preview\">"
                    if assets.get("preview")
                    else "<p>context/preview: none</p>"
                )
            )
            + "</div>"
            f"<p>observation_id={int(item['observation_id'])}, photo_id={int(item['photo_id'])}, source_kind={html.escape(str(item['source_kind']))}, source_cluster_id={html.escape(str(item['source_cluster_id']))}</p>"
            f"<p>best_cluster={html.escape(str(item['best_cluster_id']))}, best_identity={html.escape(str(item['best_identity_id']))}, margin={html.escape(str(item['distance_margin']))}, same_photo_conflict={str(bool(item['same_photo_conflict'])).lower()}</p>"
            f"<p>decision={html.escape(str(item['decision']))}, reason_code={html.escape(str(item['reason_code']))}</p>"
            f"<ol class=\"top-candidates\">{top_candidate_html}</ol>"
            "</article>"
        )
