from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.models import ExportBucket, ExportMatch, ExportPreview
from hikbox_pictures.repositories import ExportRepo


class ExportMatchService:
    SPEC_VERSION = 1

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.export_repo = ExportRepo(conn)

    def preview_template(self, template_id: int) -> ExportPreview:
        plan = self.build_template_plan(template_id)
        return ExportPreview(
            template_id=int(plan["template"]["id"]),
            spec_hash=str(plan["spec_hash"]),
            matched_only_count=int(plan["matched_only_count"]),
            matched_group_count=int(plan["matched_group_count"]),
        )

    def build_template_plan(self, template_id: int) -> dict[str, Any]:
        template = self.export_repo.get_template(int(template_id))
        if template is None:
            raise LookupError(f"export template {template_id} 不存在")

        required_person_ids = self.export_repo.list_template_person_ids(int(template_id))
        if not required_person_ids:
            raise ValueError(f"export template {template_id} 未配置人物")

        spec_hash = self.build_spec_hash(template, required_person_ids)
        matches = self._collect_matches(template, required_person_ids)
        matched_only_count = sum(1 for match in matches if match.bucket is ExportBucket.ONLY)
        matched_group_count = sum(1 for match in matches if match.bucket is ExportBucket.GROUP)
        return {
            "template": template,
            "spec_hash": spec_hash,
            "matches": matches,
            "matched_only_count": matched_only_count,
            "matched_group_count": matched_group_count,
        }

    def build_spec_hash(self, template: dict[str, Any], required_person_ids: list[int]) -> str:
        payload = {
            "version": self.SPEC_VERSION,
            "person_ids": sorted(int(person_id) for person_id in required_person_ids),
            "start_datetime": template.get("start_datetime"),
            "end_datetime": template.get("end_datetime"),
            "output_root": template.get("output_root"),
            "include_group": bool(template.get("include_group")),
            "export_live_mov": bool(template.get("export_live_mov")),
        }
        normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256(normalized.encode("utf-8")).hexdigest()

    def _collect_matches(self, template: dict[str, Any], required_person_ids: list[int]) -> list[ExportMatch]:
        rows = self.export_repo.list_assets_with_faces(
            start_datetime=template.get("start_datetime"),
            end_datetime=template.get("end_datetime"),
        )
        required = set(int(person_id) for person_id in required_person_ids)

        assets: dict[int, dict[str, Any]] = {}
        for row in rows:
            photo_asset_id = int(row["photo_asset_id"])
            asset_state = assets.setdefault(
                photo_asset_id,
                {
                    "photo_asset_id": photo_asset_id,
                    "primary_path": row["primary_path"],
                    "primary_fingerprint": row["primary_fingerprint"],
                    "live_mov_path": row["live_mov_path"],
                    "live_mov_fingerprint": row["live_mov_fingerprint"],
                    "capture_month": row["capture_month"],
                    "observations": defaultdict(
                        lambda: {
                            "face_area_ratio": None,
                            "person_ids": set(),
                        }
                    ),
                },
            )
            observation_id = row["face_observation_id"]
            if observation_id is None:
                continue
            observation = asset_state["observations"][int(observation_id)]
            observation["face_area_ratio"] = row["face_area_ratio"]
            person_id = row["person_id"]
            if person_id is not None:
                observation["person_ids"].add(int(person_id))

        matched_assets: list[ExportMatch] = []
        for asset in assets.values():
            observations = list(asset["observations"].values())
            if not observations:
                continue

            matched_observations = [
                observation
                for observation in observations
                if observation["person_ids"] and observation["person_ids"].issubset(required)
            ]
            matched_persons = set()
            for observation in matched_observations:
                matched_persons.update(int(person_id) for person_id in observation["person_ids"])

            if not required.issubset(matched_persons):
                continue

            extra_observations = [
                observation
                for observation in observations
                if not (observation["person_ids"] and observation["person_ids"].issubset(required))
            ]
            bucket = self.classify_bucket(matched_observations=matched_observations, extra_observations=extra_observations)
            matched_assets.append(
                ExportMatch(
                    photo_asset_id=int(asset["photo_asset_id"]),
                    bucket=bucket,
                    primary_path=Path(str(asset["primary_path"])),
                    primary_fingerprint=str(asset["primary_fingerprint"]) if asset["primary_fingerprint"] else None,
                    live_mov_path=Path(str(asset["live_mov_path"])) if asset["live_mov_path"] else None,
                    live_mov_fingerprint=str(asset["live_mov_fingerprint"]) if asset["live_mov_fingerprint"] else None,
                    capture_month=str(asset["capture_month"]) if asset["capture_month"] else None,
                )
            )

        matched_assets.sort(key=lambda item: item.photo_asset_id)
        return matched_assets

    def classify_bucket(
        self,
        *,
        matched_observations: list[dict[str, Any]],
        extra_observations: list[dict[str, Any]],
    ) -> ExportBucket:
        areas = [observation["face_area_ratio"] for observation in matched_observations]
        if not areas or any(area is None for area in areas):
            return ExportBucket.GROUP

        selected_min_area = min(float(area) for area in areas)
        significant_extra_face_threshold = selected_min_area / 4.0

        for observation in extra_observations:
            area = observation.get("face_area_ratio")
            if area is None or float(area) >= significant_extra_face_threshold:
                return ExportBucket.GROUP
        return ExportBucket.ONLY
