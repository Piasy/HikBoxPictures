from __future__ import annotations

import math
from pathlib import Path

import numpy as np

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.image_io import load_oriented_image
from hikbox_pictures.repositories import AssetRepo, IdentityRepo
from hikbox_pictures.services.quality_score_service import QualityScoreService
from hikbox_pictures.workspace import load_workspace_paths_from_db_path


class ObservationQualityBackfillService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.asset_repo = AssetRepo(conn)
        self.identity_repo = IdentityRepo(conn)
        self.quality_score_service = QualityScoreService()
        db_path = self._resolve_db_path()
        paths = load_workspace_paths_from_db_path(db_path)
        self.face_crop_dir = paths.artifacts_dir / "face-crops" / "scan"

    def backfill_all_observations(
        self,
        *,
        profile_id: int,
        update_profile_quantiles: bool = False,
    ) -> dict[str, int | float]:
        rows = self.asset_repo.list_active_observations_for_quality_backfill()
        return self._backfill_rows(
            rows=rows,
            profile_id=int(profile_id),
            update_profile_quantiles=update_profile_quantiles,
        )

    def backfill_observations(
        self,
        *,
        observation_ids: list[int],
        profile_id: int | None = None,
        update_profile_quantiles: bool = False,
    ) -> dict[str, int | float]:
        rows = self.asset_repo.list_active_observations_for_quality_backfill_by_ids(observation_ids)
        return self._backfill_rows(
            rows=rows,
            profile_id=profile_id,
            update_profile_quantiles=update_profile_quantiles,
        )

    def _backfill_rows(
        self,
        *,
        rows: list[dict[str, object]],
        profile_id: int | None,
        update_profile_quantiles: bool,
    ) -> dict[str, int | float]:
        managed_transaction = not self.conn.in_transaction
        created_crop_files: list[Path] = []
        db_write_started = False
        try:
            if not rows:
                result = {
                    "updated_observation_count": 0,
                    "area_log_p10": 0.0,
                    "area_log_p90": 0.0,
                    "sharpness_log_p10": 0.0,
                    "sharpness_log_p90": 0.0,
                }
                if managed_transaction:
                    self.conn.commit()
                return result

            area_logs = [math.log10(max(float(row.get("face_area_ratio") or 0.0), 1e-6)) for row in rows]
            area_p10, area_p90 = self._quantile_pair(area_logs)

            prepared_rows: list[dict[str, object]] = []
            sharpness_logs: list[float] = []
            for row in rows:
                observation_id = int(row["id"])
                crop_path, created_now = self._resolve_or_rebuild_crop_path(row)
                sharpness_raw = self._compute_sharpness_raw(crop_path)
                if created_now:
                    created_crop_files.append(crop_path)
                prepared_rows.append(
                    {
                        "row": row,
                        "observation_id": observation_id,
                        "sharpness_raw": sharpness_raw,
                        "crop_path": str(crop_path),
                        "created_crop": created_now,
                    }
                )
                sharpness_logs.append(math.log1p(max(sharpness_raw, 0.0)))
            sharpness_p10, sharpness_p90 = self._quantile_pair(sharpness_logs)

            db_write_started = True
            sharpness_by_observation: dict[int, float] = {}
            for prepared in prepared_rows:
                observation_id = int(prepared["observation_id"])
                if bool(prepared["created_crop"]):
                    self.asset_repo.update_observation_crop_path(observation_id, str(prepared["crop_path"]))
                sharpness_raw = float(prepared["sharpness_raw"])
                self.asset_repo.update_observation_sharpness_score(observation_id, sharpness_raw)
                sharpness_by_observation[observation_id] = sharpness_raw

            if profile_id is not None and update_profile_quantiles:
                self.identity_repo.update_profile_quality_quantiles(
                    profile_id=int(profile_id),
                    area_log_p10=area_p10,
                    area_log_p90=area_p90,
                    sharpness_log_p10=sharpness_p10,
                    sharpness_log_p90=sharpness_p90,
                )

            if profile_id is not None:
                profile = self.identity_repo.get_profile_required(int(profile_id))
                for prepared in prepared_rows:
                    row = prepared["row"]
                    if not isinstance(row, dict):
                        continue
                    observation_id = int(prepared["observation_id"])
                    quality_score = self.quality_score_service.compute_quality_score(
                        face_area_ratio=float(row.get("face_area_ratio") or 0.0),
                        sharpness_score=sharpness_by_observation.get(observation_id),
                        pose_score=row.get("pose_score"),  # type: ignore[arg-type]
                        profile=profile,
                    )
                    self.asset_repo.update_observation_quality_score(observation_id, quality_score)

            if managed_transaction:
                self.conn.commit()
            return {
                "updated_observation_count": len(rows),
                "area_log_p10": float(area_p10),
                "area_log_p90": float(area_p90),
                "sharpness_log_p10": float(sharpness_p10),
                "sharpness_log_p90": float(sharpness_p90),
            }
        except Exception:
            if managed_transaction and self.conn.in_transaction:
                self.conn.rollback()
            should_cleanup_created_crop = managed_transaction or not db_write_started
            if should_cleanup_created_crop:
                self._cleanup_created_crops(created_crop_files)
            raise

    def _quantile_pair(self, values: list[float]) -> tuple[float, float]:
        if not values:
            return 0.0, 0.0
        data = np.asarray(values, dtype=np.float64)
        p10 = float(np.quantile(data, 0.1))
        p90 = float(np.quantile(data, 0.9))
        if p90 < p10:
            return p90, p10
        return p10, p90

    def _resolve_or_rebuild_crop_path(self, row: dict[str, object]) -> tuple[Path, bool]:
        observation_id = int(row["id"])
        crop_path_raw = row.get("crop_path")
        if crop_path_raw:
            crop_path = Path(str(crop_path_raw))
            if crop_path.exists() and crop_path.is_file():
                return crop_path, False

        source_path = Path(str(row["primary_path"]))
        if not source_path.exists() or not source_path.is_file():
            raise FileNotFoundError(f"媒体文件不存在: {source_path}")

        image = load_oriented_image(source_path)
        width, height = image.size
        left = max(0, min(width - 1, int(float(row["bbox_left"]) * width)))
        top = max(0, min(height - 1, int(float(row["bbox_top"]) * height)))
        right = max(left + 1, min(width, int(float(row["bbox_right"]) * width)))
        bottom = max(top + 1, min(height, int(float(row["bbox_bottom"]) * height)))

        self.face_crop_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.face_crop_dir / f"obs-{observation_id}.jpg"
        existed_before = out_path.exists()
        image.crop((left, top, right, bottom)).convert("RGB").save(out_path, format="JPEG")
        return out_path, not existed_before

    def _cleanup_created_crops(self, created_crop_files: list[Path]) -> None:
        for crop_path in created_crop_files:
            try:
                crop_path.unlink(missing_ok=True)
            except OSError:
                continue

    def _compute_sharpness_raw(self, crop_path: Path) -> float:
        image = load_oriented_image(crop_path).convert("L")
        gray = np.asarray(image, dtype=np.float32)
        if gray.ndim != 2 or gray.shape[0] < 2 or gray.shape[1] < 2:
            return 0.0
        grad_x = np.diff(gray, axis=1)
        grad_y = np.diff(gray, axis=0)
        score = float(np.var(grad_x) + np.var(grad_y))
        return max(0.0, score)

    def _resolve_db_path(self) -> Path:
        rows = self.conn.execute("PRAGMA database_list").fetchall()
        for row in rows:
            name = row["name"] if isinstance(row, sqlite3.Row) else row[1]
            if str(name) != "main":
                continue
            raw_path = row["file"] if isinstance(row, sqlite3.Row) else row[2]
            if raw_path:
                return Path(str(raw_path)).resolve()
        raise RuntimeError("无法解析当前连接对应的数据库路径")
