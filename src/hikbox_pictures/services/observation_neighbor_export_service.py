from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import html
import json
from pathlib import Path
import shutil
from typing import Any

import numpy as np

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.image_io import load_oriented_image
from hikbox_pictures.repositories import AssetRepo
from hikbox_pictures.services.preview_artifact_service import PreviewArtifactService
from hikbox_pictures.workspace import load_workspace_paths


@dataclass(frozen=True)
class _ObservationRecord:
    observation_id: int
    photo_id: int
    quality_score: float
    primary_path: str
    cluster_id: int | None
    cluster_status: str | None
    reject_reason: str | None
    distance: float | None = None


class ObservationNeighborExportService:
    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.paths = load_workspace_paths(self.workspace)
        self.db_path = self.paths.db_path
        self.preview_artifact_service = PreviewArtifactService(
            db_path=self.db_path,
            workspace=self.workspace,
        )

    def export(
        self,
        *,
        observation_ids: list[int],
        output_root: Path,
        neighbor_count: int = 8,
    ) -> dict[str, Path]:
        if neighbor_count <= 0:
            raise ValueError("neighbor_count 必须大于 0")

        requested_ids = _dedupe_preserve_order(observation_ids)
        if not requested_ids:
            raise ValueError("observation_ids 不能为空")

        profile, pool, cluster_meta = self._load_bootstrap_pool()
        idx_by_observation = {
            int(observation_id): index for index, observation_id in enumerate(pool["observation_ids"].tolist())
        }
        missing = [observation_id for observation_id in requested_ids if observation_id not in idx_by_observation]
        if missing:
            missing_text = ", ".join(str(observation_id) for observation_id in missing)
            raise ValueError(
                "以下 observation 不在当前 bootstrap 候选集合中，可能缺少 embedding 或 quality 未达到 high_quality_threshold: "
                + missing_text
            )

        output_dir = (
            Path(output_root).expanduser().resolve() / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        )
        output_dir.mkdir(parents=True, exist_ok=True)

        summary: dict[str, Any] = {
            "workspace": str(self.workspace),
            "db_path": str(self.db_path),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "profile": profile,
            "neighbor_count_per_target": int(neighbor_count),
            "targets": [],
        }

        html_sections: list[str] = []
        for observation_id in requested_ids:
            target_index = idx_by_observation[int(observation_id)]
            target_record = self._build_record(
                pool=pool,
                cluster_meta=cluster_meta,
                observation_index=target_index,
                distance=None,
            )
            neighbors = self._select_neighbors(
                pool=pool,
                cluster_meta=cluster_meta,
                target_index=target_index,
                neighbor_count=int(neighbor_count),
            )

            observation_dir = output_dir / f"obs-{target_record.observation_id}"
            observation_dir.mkdir(parents=True, exist_ok=True)

            target_payload = self._export_record_assets(
                record=target_record,
                output_dir=observation_dir,
                file_prefix=(
                    f"00-target_obs-{target_record.observation_id}_photo-{target_record.photo_id}"
                ),
            )
            neighbor_payloads = [
                self._export_record_assets(
                    record=neighbor,
                    output_dir=observation_dir,
                    file_prefix=f"{rank:02d}-nn_obs-{neighbor.observation_id}_photo-{neighbor.photo_id}",
                )
                for rank, neighbor in enumerate(neighbors, start=1)
            ]

            summary["targets"].append(
                {
                    "target": target_payload,
                    "neighbors": neighbor_payloads,
                }
            )
            html_sections.append(
                self._render_target_section(
                    target=target_payload,
                    neighbors=neighbor_payloads,
                )
            )

        index_path = output_dir / "index.html"
        manifest_path = output_dir / "manifest.json"
        index_path.write_text(
            self._render_index_html(
                summary=summary,
                sections=html_sections,
            ),
            encoding="utf-8",
        )
        manifest_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return {
            "output_dir": output_dir,
            "index_path": index_path,
            "manifest_path": manifest_path,
        }

    def _load_bootstrap_pool(self) -> tuple[dict[str, float | str], dict[str, Any], dict[int, dict[str, Any]]]:
        conn = connect_db(self.db_path)
        try:
            profile_row = conn.execute(
                """
                SELECT embedding_model_key,
                       high_quality_threshold,
                       bootstrap_edge_candidate_threshold,
                       bootstrap_edge_accept_threshold,
                       bootstrap_margin_threshold
                FROM identity_threshold_profile
                WHERE active = 1
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            if profile_row is None:
                raise ValueError("当前 workspace 没有 active profile")

            profile = {
                "embedding_model_key": str(profile_row["embedding_model_key"]),
                "high_quality_threshold": float(profile_row["high_quality_threshold"]),
                "bootstrap_edge_candidate_threshold": float(profile_row["bootstrap_edge_candidate_threshold"]),
                "bootstrap_edge_accept_threshold": float(profile_row["bootstrap_edge_accept_threshold"]),
                "bootstrap_margin_threshold": float(profile_row["bootstrap_margin_threshold"]),
            }

            rows = conn.execute(
                """
                SELECT fo.id AS observation_id,
                       fo.photo_asset_id,
                       COALESCE(fo.quality_score, 0.0) AS quality_score,
                       pa.primary_path,
                       fe.vector_blob
                FROM face_observation AS fo
                JOIN photo_asset AS pa
                  ON pa.id = fo.photo_asset_id
                JOIN face_embedding AS fe
                  ON fe.face_observation_id = fo.id
                 AND fe.feature_type = 'face'
                 AND fe.model_key = ?
                 AND fe.normalized = 1
                WHERE fo.active = 1
                  AND COALESCE(fo.quality_score, 0.0) >= ?
                ORDER BY fo.id ASC
                """,
                (
                    str(profile["embedding_model_key"]),
                    float(profile["high_quality_threshold"]),
                ),
            ).fetchall()
            if not rows:
                raise ValueError("当前 bootstrap 候选集合为空")

            observation_ids: list[int] = []
            photo_ids: list[int] = []
            quality_scores: list[float] = []
            primary_paths: list[str] = []
            vectors: list[np.ndarray[Any, np.dtype[np.float32]]] = []
            expected_dimension: int | None = None

            for row in rows:
                vector_blob = row["vector_blob"]
                if not isinstance(vector_blob, (bytes, bytearray, memoryview)):
                    continue
                vector = np.frombuffer(vector_blob, dtype=np.float32).copy()
                if vector.ndim != 1 or int(vector.size) <= 0:
                    continue
                if expected_dimension is None:
                    expected_dimension = int(vector.size)
                elif int(vector.size) != expected_dimension:
                    continue
                observation_ids.append(int(row["observation_id"]))
                photo_ids.append(int(row["photo_asset_id"]))
                quality_scores.append(float(row["quality_score"]))
                primary_paths.append(str(row["primary_path"]))
                vectors.append(vector.astype(np.float32, copy=False))

            if not vectors:
                raise ValueError("当前 bootstrap 候选集合没有可用 embedding")

            cluster_meta_rows = conn.execute(
                """
                SELECT acm.face_observation_id AS observation_id,
                       ac.id AS cluster_id,
                       ac.cluster_status,
                       json_extract(ac.diagnostic_json, '$.reject_reason') AS reject_reason
                FROM auto_cluster_member AS acm
                JOIN auto_cluster AS ac
                  ON ac.id = acm.cluster_id
                """
            ).fetchall()
            cluster_meta = {
                int(row["observation_id"]): {
                    "cluster_id": int(row["cluster_id"]),
                    "cluster_status": str(row["cluster_status"]),
                    "reject_reason": (
                        None if row["reject_reason"] is None else str(row["reject_reason"])
                    ),
                }
                for row in cluster_meta_rows
            }
        finally:
            conn.close()

        return (
            profile,
            {
                "observation_ids": np.asarray(observation_ids, dtype=np.int64),
                "photo_ids": np.asarray(photo_ids, dtype=np.int64),
                "quality_scores": np.asarray(quality_scores, dtype=np.float32),
                "primary_paths": primary_paths,
                "vectors": np.stack(vectors).astype(np.float32),
            },
            cluster_meta,
        )

    def _select_neighbors(
        self,
        *,
        pool: dict[str, Any],
        cluster_meta: dict[int, dict[str, Any]],
        target_index: int,
        neighbor_count: int,
    ) -> list[_ObservationRecord]:
        vectors = np.asarray(pool["vectors"], dtype=np.float32)
        distances = np.linalg.norm(vectors - vectors[int(target_index)], axis=1)
        order = np.argsort(distances)

        neighbors: list[_ObservationRecord] = []
        for observation_index in order:
            if int(observation_index) == int(target_index):
                continue
            neighbors.append(
                self._build_record(
                    pool=pool,
                    cluster_meta=cluster_meta,
                    observation_index=int(observation_index),
                    distance=float(distances[int(observation_index)]),
                )
            )
            if len(neighbors) >= int(neighbor_count):
                break
        return neighbors

    def _build_record(
        self,
        *,
        pool: dict[str, Any],
        cluster_meta: dict[int, dict[str, Any]],
        observation_index: int,
        distance: float | None,
    ) -> _ObservationRecord:
        observation_id = int(pool["observation_ids"][int(observation_index)])
        cluster_info = cluster_meta.get(observation_id, {})
        return _ObservationRecord(
            observation_id=observation_id,
            photo_id=int(pool["photo_ids"][int(observation_index)]),
            quality_score=float(pool["quality_scores"][int(observation_index)]),
            primary_path=str(pool["primary_paths"][int(observation_index)]),
            cluster_id=(
                int(cluster_info["cluster_id"])
                if cluster_info.get("cluster_id") is not None
                else None
            ),
            cluster_status=(
                str(cluster_info["cluster_status"])
                if cluster_info.get("cluster_status") is not None
                else None
            ),
            reject_reason=(
                str(cluster_info["reject_reason"])
                if cluster_info.get("reject_reason") is not None
                else None
            ),
            distance=distance,
        )

    def _export_record_assets(
        self,
        *,
        record: _ObservationRecord,
        output_dir: Path,
        file_prefix: str,
    ) -> dict[str, Any]:
        crop_path = self._ensure_crop_path(int(record.observation_id))
        preview_path = Path(
            self.preview_artifact_service.ensure_photo_preview(
                photo_id=int(record.photo_id),
                source_path=Path(record.primary_path),
            )
        )

        crop_file = f"{file_prefix}__crop.jpg"
        preview_file = f"{file_prefix}__preview.jpg"
        shutil.copy2(crop_path, output_dir / crop_file)
        shutil.copy2(preview_path, output_dir / preview_file)

        payload = {
            "observation_id": int(record.observation_id),
            "photo_id": int(record.photo_id),
            "quality_score": float(record.quality_score),
            "distance": None if record.distance is None else float(record.distance),
            "primary_path": str(record.primary_path),
            "cluster_id": int(record.cluster_id) if record.cluster_id is not None else None,
            "cluster_status": record.cluster_status,
            "reject_reason": record.reject_reason,
            "crop_file": crop_file,
            "preview_file": preview_file,
        }
        return payload

    def _ensure_crop_path(self, observation_id: int) -> Path:
        conn = connect_db(self.db_path)
        try:
            repo = AssetRepo(conn)
            row = repo.get_observation_with_source(int(observation_id))
            if row is None:
                raise LookupError(f"observation {int(observation_id)} 不存在")

            crop_path_raw = row.get("crop_path")
            if crop_path_raw:
                crop_path = Path(str(crop_path_raw)).expanduser().resolve()
                if crop_path.exists() and crop_path.is_file():
                    if self.preview_artifact_service._is_crop_artifact_usable(row, crop_path):
                        return crop_path

            source_path = Path(str(row["primary_path"])).expanduser().resolve()
            if not source_path.exists() or not source_path.is_file():
                raise LookupError(f"媒体文件不存在: {source_path}")

            rebuilt_dir = self.paths.artifacts_dir / "face-crops" / "rebuilt"
            rebuilt_dir.mkdir(parents=True, exist_ok=True)
            rebuilt_path = rebuilt_dir / f"obs-{int(observation_id)}.jpg"

            image = load_oriented_image(source_path)
            left, top, right, bottom = self.preview_artifact_service._resolve_bbox_pixels(
                row,
                width=image.width,
                height=image.height,
            )
            image.crop((left, top, right, bottom)).convert("RGB").save(rebuilt_path, format="JPEG")
            repo.update_observation_crop_path(int(observation_id), str(rebuilt_path))
            conn.commit()
            return rebuilt_path
        finally:
            conn.close()

    def _render_index_html(
        self,
        *,
        summary: dict[str, Any],
        sections: list[str],
    ) -> str:
        profile = summary["profile"]
        return "\n".join(
            [
                "<!DOCTYPE html>",
                "<html lang=\"zh-CN\">",
                "<head>",
                "  <meta charset=\"utf-8\" />",
                "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />",
                "  <title>Observation 最近邻人工核对</title>",
                "  <style>",
                "    body { font-family: -apple-system, BlinkMacSystemFont, \"PingFang SC\", sans-serif; margin: 24px; background: #f6f7f4; color: #182018; }",
                "    h1, h2, p { margin: 0; }",
                "    .meta { margin-top: 8px; color: #526052; font-size: 14px; line-height: 1.45; word-break: break-word; }",
                "    .target { margin-top: 28px; padding: 18px; border: 1px solid #d8dfd3; border-radius: 16px; background: #ffffff; }",
                "    .cards { margin-top: 16px; display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; }",
                "    .card { border: 1px solid #e4eadf; border-radius: 14px; padding: 12px; background: #fbfcfa; display: grid; gap: 10px; }",
                "    .card.is-target { border-color: #9ab58b; background: #f5faf1; }",
                "    .label { font-weight: 700; }",
                "    .detail { font-size: 13px; color: #4c5a4c; line-height: 1.45; word-break: break-word; }",
                "    .images { display: grid; grid-template-columns: 120px 1fr; gap: 10px; align-items: start; }",
                "    .images img { width: 100%; border-radius: 10px; border: 1px solid #d6dfd1; background: #eef4ea; }",
                "    .images img.preview { object-fit: contain; background: #f1f4ef; }",
                "    code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }",
                "  </style>",
                "</head>",
                "<body>",
                "  <h1>Observation 最近邻人工核对</h1>",
                f"  <p class=\"meta\">workspace: <code>{html.escape(str(summary['workspace']))}</code></p>",
                f"  <p class=\"meta\">db_path: <code>{html.escape(str(summary['db_path']))}</code></p>",
                (
                    "  <p class=\"meta\">bootstrap 口径: "
                    f"high_quality_threshold={_format_metric(float(profile['high_quality_threshold']))}, "
                    f"candidate_threshold={_format_metric(float(profile['bootstrap_edge_candidate_threshold']))}, "
                    f"accept_threshold={_format_metric(float(profile['bootstrap_edge_accept_threshold']))}, "
                    f"margin_threshold={_format_metric(float(profile['bootstrap_margin_threshold']))}, "
                    f"每条 observation 展示前 {int(summary['neighbor_count_per_target'])} 个最近邻</p>"
                ),
                *sections,
                "</body>",
                "</html>",
            ]
        )

    def _render_target_section(
        self,
        *,
        target: dict[str, Any],
        neighbors: list[dict[str, Any]],
    ) -> str:
        target_id = int(target["observation_id"])
        cards = [
            "\n".join(
                [
                    "      <article class=\"card is-target\">",
                    "        <div class=\"label\">目标 observation</div>",
                    (
                        "        <div class=\"detail\">"
                        f"obs {int(target['observation_id'])} · photo {int(target['photo_id'])} · "
                        f"quality {_format_metric(float(target['quality_score']))}"
                        "</div>"
                    ),
                    (
                        "        <div class=\"detail\">"
                        f"cluster #{_format_optional_int(target['cluster_id'])} · "
                        f"{html.escape(str(target['cluster_status'] or '(null)'))} · "
                        f"reject_reason={html.escape(str(target['reject_reason'] or '(null)'))}"
                        "</div>"
                    ),
                    (
                        "        <div class=\"detail\"><code>"
                        + html.escape(str(target["primary_path"]))
                        + "</code></div>"
                    ),
                    "        <div class=\"images\">",
                    (
                        "          <img src=\""
                        + html.escape(f"obs-{target_id}/{target['crop_file']}")
                        + "\" alt=\"target crop\" />"
                    ),
                    (
                        "          <img class=\"preview\" src=\""
                        + html.escape(f"obs-{target_id}/{target['preview_file']}")
                        + "\" alt=\"target preview\" />"
                    ),
                    "        </div>",
                    "      </article>",
                ]
            )
        ]

        for rank, item in enumerate(neighbors, start=1):
            cards.append(
                "\n".join(
                    [
                        "      <article class=\"card\">",
                        f"        <div class=\"label\">NN {rank}</div>",
                        (
                            "        <div class=\"detail\">"
                            f"obs {int(item['observation_id'])} · photo {int(item['photo_id'])} · "
                            f"distance {_format_metric(float(item['distance']))} · "
                            f"quality {_format_metric(float(item['quality_score']))}"
                            "</div>"
                        ),
                        (
                            "        <div class=\"detail\">"
                            f"cluster #{_format_optional_int(item['cluster_id'])} · "
                            f"{html.escape(str(item['cluster_status'] or '(null)'))} · "
                            f"reject_reason={html.escape(str(item['reject_reason'] or '(null)'))}"
                            "</div>"
                        ),
                        (
                            "        <div class=\"detail\"><code>"
                            + html.escape(str(item["primary_path"]))
                            + "</code></div>"
                        ),
                        "        <div class=\"images\">",
                        (
                            "          <img src=\""
                            + html.escape(f"obs-{target_id}/{item['crop_file']}")
                            + "\" alt=\"neighbor crop\" />"
                        ),
                        (
                            "          <img class=\"preview\" src=\""
                            + html.escape(f"obs-{target_id}/{item['preview_file']}")
                            + "\" alt=\"neighbor preview\" />"
                        ),
                        "        </div>",
                        "      </article>",
                    ]
                )
            )

        return "\n".join(
            [
                "  <section class=\"target\">",
                (
                    f"    <h2>observation {int(target['observation_id'])} / "
                    f"photo {int(target['photo_id'])} / "
                    f"quality {_format_metric(float(target['quality_score']))}</h2>"
                ),
                (
                    "    <p class=\"meta\">"
                    f"cluster #{_format_optional_int(target['cluster_id'])} · "
                    f"{html.escape(str(target['cluster_status'] or '(null)'))} · "
                    f"reject_reason={html.escape(str(target['reject_reason'] or '(null)'))}"
                    "</p>"
                ),
                (
                    "    <p class=\"meta\"><code>"
                    + html.escape(str(target["primary_path"]))
                    + "</code></p>"
                ),
                "    <div class=\"cards\">",
                *cards,
                "    </div>",
                "  </section>",
            ]
        )


def _dedupe_preserve_order(values: list[int]) -> list[int]:
    seen: set[int] = set()
    result: list[int] = []
    for value in values:
        int_value = int(value)
        if int_value in seen:
            continue
        seen.add(int_value)
        result.append(int_value)
    return result


def _format_metric(value: float) -> str:
    return f"{float(value):.2f}"


def _format_optional_int(value: int | None) -> str:
    if value is None:
        return "(null)"
    return str(int(value))
