from __future__ import annotations

from collections import deque
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
    run_id: int | None = None
    observation_snapshot_id: int | None = None
    observation_profile_id: int | None = None
    cluster_profile_id: int | None = None
    cluster_stage: str | None = None
    member_role: str | None = None
    decision_status: str | None = None
    publish_state: str | None = None
    is_selected_trusted_seed: int | None = None
    seed_rank: int | None = None
    is_representative: int | None = None
    nearest_competing_cluster_distance: float | None = None
    separation_gap: float | None = None
    exclusion_reason: str | None = None


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
        observation_ids: list[int] | None,
        run_id: int | None = None,
        cluster_id: int | None = None,
        output_root: Path,
        neighbor_count: int = 8,
    ) -> dict[str, Path]:
        if neighbor_count <= 0:
            raise ValueError("neighbor_count 必须大于 0")

        output_dir = (
            Path(output_root).expanduser().resolve() / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        )
        output_dir.mkdir(parents=True, exist_ok=True)

        index_path = output_dir / "index.html"
        manifest_path = output_dir / "manifest.json"
        requested_ids = _dedupe_preserve_order(observation_ids or [])
        if cluster_id is not None and requested_ids:
            summary, html_text = self._build_cluster_connectivity_export(
                observation_ids=requested_ids,
                run_id=run_id,
                cluster_id=int(cluster_id),
                output_dir=output_dir,
                neighbor_count=int(neighbor_count),
            )
        else:
            summary, html_text = self._build_nearest_neighbor_export(
                observation_ids=requested_ids,
                run_id=run_id,
                cluster_id=cluster_id,
                output_dir=output_dir,
                neighbor_count=int(neighbor_count),
            )
        index_path.write_text(
            html_text,
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

    def _build_nearest_neighbor_export(
        self,
        *,
        observation_ids: list[int],
        run_id: int | None,
        cluster_id: int | None,
        output_dir: Path,
        neighbor_count: int,
    ) -> tuple[dict[str, Any], str]:
        run_context_summary: dict[str, Any] = {
            "run_id": None,
            "observation_snapshot_id": None,
            "observation_profile_id": None,
            "cluster_profile_id": None,
        }
        run_member_context: dict[int, dict[str, Any]] = {}
        requested_ids: list[int]
        if cluster_id is not None:
            effective_run_id = self._resolve_effective_run_id(run_id)
            run_context_summary, run_member_context = self._load_run_member_context(
                run_id=int(effective_run_id)
            )
            requested_ids = self._list_cluster_member_observation_ids(
                run_id=int(effective_run_id),
                cluster_id=int(cluster_id),
            )
            if not requested_ids:
                raise ValueError(f"cluster 没有成员，或不属于 run: cluster_id={int(cluster_id)}")
        else:
            requested_ids = _dedupe_preserve_order(observation_ids or [])
            if not requested_ids:
                raise ValueError("observation_ids 不能为空")
            effective_run_id = (
                int(run_id)
                if run_id is not None
                else self._resolve_review_target_run_id_or_none()
            )
            if effective_run_id is not None:
                run_context_summary, run_member_context = self._load_run_member_context(
                    run_id=int(effective_run_id)
                )

        profile, pool, cluster_meta = self._load_bootstrap_pool()
        idx_by_observation = self._index_pool_by_observation(pool=pool)
        missing = [observation_id for observation_id in requested_ids if observation_id not in idx_by_observation]
        if missing:
            missing_text = ", ".join(str(observation_id) for observation_id in missing)
            raise ValueError(
                "以下 observation 不在当前 bootstrap 候选集合中，可能缺少 embedding 或 quality 未达到 high_quality_threshold: "
                + missing_text
            )

        summary: dict[str, Any] = {
            "mode": "nearest_neighbors",
            "workspace": str(self.workspace),
            "db_path": str(self.db_path),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "profile": profile,
            "neighbor_count_per_target": int(neighbor_count),
            "run_id": run_context_summary["run_id"],
            "observation_snapshot_id": run_context_summary["observation_snapshot_id"],
            "observation_profile_id": run_context_summary["observation_profile_id"],
            "cluster_profile_id": run_context_summary["cluster_profile_id"],
            "targets": [],
        }

        html_sections: list[str] = []
        for observation_id in requested_ids:
            target_index = idx_by_observation[int(observation_id)]
            target_record = self._build_record(
                pool=pool,
                cluster_meta=cluster_meta,
                run_member_context=run_member_context,
                observation_index=target_index,
                distance=None,
            )
            neighbors = self._select_neighbors(
                pool=pool,
                cluster_meta=cluster_meta,
                run_member_context=run_member_context,
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

        return summary, self._render_index_html(summary=summary, sections=html_sections)

    def _build_cluster_connectivity_export(
        self,
        *,
        observation_ids: list[int],
        run_id: int | None,
        cluster_id: int,
        output_dir: Path,
        neighbor_count: int,
    ) -> tuple[dict[str, Any], str]:
        requested_ids = _dedupe_preserve_order(observation_ids or [])
        if not requested_ids:
            raise ValueError("observation_ids 不能为空")

        effective_run_id = self._resolve_effective_run_id(run_id)
        run_context_summary, run_member_context = self._load_run_member_context(
            run_id=int(effective_run_id)
        )
        profile, pool = self._load_snapshot_core_pool(
            run_id=int(effective_run_id),
            observation_snapshot_id=int(run_context_summary["observation_snapshot_id"]),
            cluster_profile_id=int(run_context_summary["cluster_profile_id"]),
        )
        idx_by_observation = self._index_pool_by_observation(pool=pool)
        missing = [observation_id for observation_id in requested_ids if observation_id not in idx_by_observation]
        if missing:
            missing_text = ", ".join(str(observation_id) for observation_id in missing)
            raise ValueError(
                "以下 observation 不在该 run 的 core_discovery 图中，无法做 raw mutual-kNN 连通分析: "
                + missing_text
            )

        graph_context = self._load_cluster_connectivity_context(
            run_id=int(effective_run_id),
            cluster_id=int(cluster_id),
            requested_ids=requested_ids,
        )

        vector_map = self._build_vector_map(pool=pool)
        adjacency, edge_distances = self._build_raw_cluster_graph(
            raw_member_ids=graph_context["raw_member_ids"],
            raw_summary=graph_context["raw_summary"],
            vector_map=vector_map,
            discovery_knn_k=int(profile["discovery_knn_k"]),
        )

        path_specs = self._build_connectivity_paths(
            requested_ids=requested_ids,
            adjacency=adjacency,
            edge_distances=edge_distances,
            neighbor_count=int(neighbor_count),
        )

        targets_dir = output_dir / "targets"
        targets_dir.mkdir(parents=True, exist_ok=True)
        target_payloads: list[dict[str, Any]] = []
        for target_rank, observation_id in enumerate(requested_ids, start=1):
            target_payloads.append(
                {
                    **self._export_record_assets_with_dir(
                    record=self._build_record(
                        pool=pool,
                        cluster_meta={},
                        run_member_context=run_member_context,
                        observation_index=idx_by_observation[int(observation_id)],
                        distance=None,
                    ),
                    output_dir=targets_dir,
                    file_prefix=f"{target_rank:02d}-target_obs-{int(observation_id)}",
                    asset_dir="targets",
                    ),
                    "is_requested_target": True,
                }
            )

        graph_paths: list[dict[str, Any]] = []
        html_sections: list[str] = []
        requested_id_set = set(requested_ids)
        for path_rank, path_spec in enumerate(path_specs, start=1):
            node_ids = [int(value) for value in path_spec["node_ids"]]
            asset_dir = f"path-{path_rank:02d}_obs-{int(path_spec['source_observation_id'])}-to-{int(path_spec['target_observation_id'])}"
            path_dir = output_dir / asset_dir
            path_dir.mkdir(parents=True, exist_ok=True)

            edge_payloads: list[dict[str, Any]] = []
            for edge_rank, (from_observation_id, to_observation_id) in enumerate(
                zip(node_ids, node_ids[1:]),
                start=1,
            ):
                edge_distance = self._lookup_edge_distance(
                    edge_distances=edge_distances,
                    from_observation_id=int(from_observation_id),
                    to_observation_id=int(to_observation_id),
                )
                from_payload = self._export_record_assets_with_dir(
                    record=self._build_record(
                        pool=pool,
                        cluster_meta={},
                        run_member_context=run_member_context,
                        observation_index=idx_by_observation[int(from_observation_id)],
                        distance=edge_distance,
                    ),
                    output_dir=path_dir,
                    file_prefix=f"{edge_rank:02d}-from_obs-{int(from_observation_id)}",
                    asset_dir=asset_dir,
                )
                to_payload = self._export_record_assets_with_dir(
                    record=self._build_record(
                        pool=pool,
                        cluster_meta={},
                        run_member_context=run_member_context,
                        observation_index=idx_by_observation[int(to_observation_id)],
                        distance=edge_distance,
                    ),
                    output_dir=path_dir,
                    file_prefix=f"{edge_rank:02d}-to_obs-{int(to_observation_id)}",
                    asset_dir=asset_dir,
                )
                edge_payloads.append(
                    {
                        "rank": int(edge_rank),
                        "distance": float(edge_distance),
                        "from": {
                            **from_payload,
                            "is_requested_target": bool(int(from_observation_id) in requested_id_set),
                        },
                        "to": {
                            **to_payload,
                            "is_requested_target": bool(int(to_observation_id) in requested_id_set),
                        },
                    }
                )

            path_payload = {
                "source_observation_id": int(path_spec["source_observation_id"]),
                "target_observation_id": int(path_spec["target_observation_id"]),
                "hop_count": int(max(0, len(node_ids) - 1)),
                "node_ids": node_ids,
                "asset_dir": asset_dir,
                "total_distance": float(sum(float(edge["distance"]) for edge in edge_payloads)),
                "edges": edge_payloads,
            }
            graph_paths.append(path_payload)
            html_sections.append(self._render_connectivity_path_section(path_payload=path_payload))

        summary: dict[str, Any] = {
            "mode": "cluster_connectivity",
            "workspace": str(self.workspace),
            "db_path": str(self.db_path),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "run_id": int(run_context_summary["run_id"]),
            "observation_snapshot_id": int(run_context_summary["observation_snapshot_id"]),
            "observation_profile_id": int(run_context_summary["observation_profile_id"]),
            "cluster_profile_id": int(run_context_summary["cluster_profile_id"]),
            "cluster_id": int(cluster_id),
            "cluster_stage": str(graph_context["target_cluster_stage"]),
            "profile": profile,
            "graph_scope": {
                "raw_cluster_id": int(graph_context["raw_cluster_id"]),
                "raw_cluster_stage": "raw",
                "raw_member_count": int(len(graph_context["raw_member_ids"])),
                "mutual_knn_edge_count": int(len(edge_distances)),
                "discovery_knn_k": int(profile["discovery_knn_k"]),
            },
            "lineage_chain": list(graph_context["lineage_chain"]),
            "targets": target_payloads,
            "graph_paths": graph_paths,
        }
        return summary, self._render_connectivity_index_html(summary=summary, sections=html_sections)

    def _load_snapshot_core_pool(
        self,
        *,
        run_id: int,
        observation_snapshot_id: int,
        cluster_profile_id: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        conn = connect_db(self.db_path)
        try:
            profile_row = conn.execute(
                """
                SELECT p.id,
                       p.profile_name,
                       p.profile_version,
                       p.discovery_knn_k
                FROM identity_cluster_profile AS p
                WHERE p.id = ?
                """,
                (int(cluster_profile_id),),
            ).fetchone()
            if profile_row is None:
                raise ValueError(f"cluster profile 不存在: {int(cluster_profile_id)}")

            rows = conn.execute(
                """
                SELECT pe.observation_id,
                       fo.photo_asset_id,
                       COALESCE(pe.quality_score_snapshot, fo.quality_score, 0.0) AS quality_score,
                       pa.primary_path,
                       fe.vector_blob
                FROM identity_observation_pool_entry AS pe
                JOIN identity_observation_snapshot AS s
                  ON s.id = pe.snapshot_id
                JOIN identity_observation_profile AS op
                  ON op.id = s.observation_profile_id
                JOIN face_observation AS fo
                  ON fo.id = pe.observation_id
                JOIN photo_asset AS pa
                  ON pa.id = fo.photo_asset_id
                JOIN face_embedding AS fe
                  ON fe.face_observation_id = pe.observation_id
                 AND fe.feature_type = 'face'
                 AND fe.normalized = 1
                WHERE pe.snapshot_id = ?
                  AND pe.pool_kind = 'core_discovery'
                ORDER BY pe.observation_id ASC,
                         CASE
                           WHEN fe.model_key = op.embedding_model_key THEN 0
                           ELSE 1
                         END ASC,
                         fe.id DESC
                """,
                (int(observation_snapshot_id),),
            ).fetchall()
        finally:
            conn.close()

        observation_ids: list[int] = []
        photo_ids: list[int] = []
        quality_scores: list[float] = []
        primary_paths: list[str] = []
        vectors: list[np.ndarray[Any, np.dtype[np.float32]]] = []
        expected_dimension: int | None = None
        seen_observation_ids: set[int] = set()
        for row in rows:
            observation_id = int(row["observation_id"])
            if observation_id in seen_observation_ids:
                continue
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
            seen_observation_ids.add(observation_id)
            observation_ids.append(int(observation_id))
            photo_ids.append(int(row["photo_asset_id"]))
            quality_scores.append(float(row["quality_score"]))
            primary_paths.append(str(row["primary_path"]))
            vectors.append(vector.astype(np.float32, copy=False))

        if not vectors:
            raise ValueError(
                f"run {int(run_id)} 对应 snapshot 的 core_discovery 池为空，无法导出 cluster 连通路径"
            )

        profile = {
            "run_id": int(run_id),
            "cluster_profile_id": int(profile_row["id"]),
            "profile_name": str(profile_row["profile_name"]),
            "profile_version": str(profile_row["profile_version"]),
            "discovery_knn_k": int(profile_row["discovery_knn_k"]),
        }
        pool = {
            "observation_ids": np.asarray(observation_ids, dtype=np.int64),
            "photo_ids": np.asarray(photo_ids, dtype=np.int64),
            "quality_scores": np.asarray(quality_scores, dtype=np.float32),
            "primary_paths": primary_paths,
            "vectors": np.stack(vectors).astype(np.float32),
        }
        return profile, pool

    def _load_cluster_connectivity_context(
        self,
        *,
        run_id: int,
        cluster_id: int,
        requested_ids: list[int],
    ) -> dict[str, Any]:
        conn = connect_db(self.db_path)
        try:
            target_row = conn.execute(
                """
                SELECT id, cluster_stage, member_count, summary_json
                FROM identity_cluster
                WHERE id = ?
                  AND run_id = ?
                """,
                (int(cluster_id), int(run_id)),
            ).fetchone()
            if target_row is None:
                raise ValueError(f"cluster 不存在，或不属于 run: cluster_id={int(cluster_id)} run_id={int(run_id)}")

            target_member_ids = [
                int(row["observation_id"])
                for row in conn.execute(
                    """
                    SELECT observation_id
                    FROM identity_cluster_member
                    WHERE cluster_id = ?
                    ORDER BY id ASC
                    """,
                    (int(cluster_id),),
                ).fetchall()
            ]
            missing_members = [obs_id for obs_id in requested_ids if obs_id not in set(target_member_ids)]
            if missing_members:
                missing_text = ", ".join(str(obs_id) for obs_id in missing_members)
                raise ValueError(
                    "以下 observation 不属于指定 cluster，无法做 cluster 连通分析: " + missing_text
                )

            lineage_chain: list[dict[str, Any]] = []
            current_cluster_id = int(cluster_id)
            current_stage = str(target_row["cluster_stage"])
            current_row = target_row
            while current_stage != "raw":
                parent_rows = conn.execute(
                    """
                    SELECT l.parent_cluster_id,
                           p.cluster_stage AS parent_cluster_stage,
                           l.child_cluster_id,
                           c.cluster_stage AS child_cluster_stage,
                           l.relation_kind,
                           l.reason_code,
                           l.detail_json
                    FROM identity_cluster_lineage AS l
                    JOIN identity_cluster AS p
                      ON p.id = l.parent_cluster_id
                    JOIN identity_cluster AS c
                      ON c.id = l.child_cluster_id
                    WHERE l.child_cluster_id = ?
                      AND p.run_id = ?
                      AND c.run_id = ?
                    ORDER BY l.id ASC
                    """,
                    (int(current_cluster_id), int(run_id), int(run_id)),
                ).fetchall()
                if not parent_rows:
                    raise ValueError(
                        f"cluster {int(cluster_id)} 缺少通往 raw stage 的 lineage，无法解释图连通关系"
                    )

                selected_parent = None
                for row in parent_rows:
                    parent_member_ids = {
                        int(item["observation_id"])
                        for item in conn.execute(
                            """
                            SELECT observation_id
                            FROM identity_cluster_member
                            WHERE cluster_id = ?
                            """,
                            (int(row["parent_cluster_id"]),),
                        ).fetchall()
                    }
                    if all(int(obs_id) in parent_member_ids for obs_id in requested_ids):
                        selected_parent = row
                        break
                if selected_parent is None:
                    selected_parent = parent_rows[0]

                lineage_chain.append(
                    {
                        "parent_cluster_id": int(selected_parent["parent_cluster_id"]),
                        "parent_cluster_stage": str(selected_parent["parent_cluster_stage"]),
                        "child_cluster_id": int(selected_parent["child_cluster_id"]),
                        "child_cluster_stage": str(selected_parent["child_cluster_stage"]),
                        "relation_kind": str(selected_parent["relation_kind"]),
                        "reason_code": (
                            None
                            if selected_parent["reason_code"] is None
                            else str(selected_parent["reason_code"])
                        ),
                        "detail": self._load_json(selected_parent["detail_json"]),
                    }
                )
                current_cluster_id = int(selected_parent["parent_cluster_id"])
                current_row = conn.execute(
                    """
                    SELECT id, cluster_stage, member_count, summary_json
                    FROM identity_cluster
                    WHERE id = ?
                    """,
                    (int(current_cluster_id),),
                ).fetchone()
                if current_row is None:
                    raise ValueError(f"lineage parent cluster 不存在: {int(current_cluster_id)}")
                current_stage = str(current_row["cluster_stage"])

            raw_member_ids = [
                int(row["observation_id"])
                for row in conn.execute(
                    """
                    SELECT observation_id
                    FROM identity_cluster_member
                    WHERE cluster_id = ?
                    ORDER BY id ASC
                    """,
                    (int(current_cluster_id),),
                ).fetchall()
            ]
        finally:
            conn.close()

        return {
            "target_cluster_id": int(cluster_id),
            "target_cluster_stage": str(target_row["cluster_stage"]),
            "raw_cluster_id": int(current_cluster_id),
            "raw_summary": self._load_json(current_row["summary_json"]),
            "raw_member_ids": raw_member_ids,
            "lineage_chain": lineage_chain,
        }

    def _build_connectivity_paths(
        self,
        *,
        requested_ids: list[int],
        adjacency: dict[int, set[int]],
        edge_distances: dict[tuple[int, int], float],
        neighbor_count: int,
    ) -> list[dict[str, Any]]:
        if len(requested_ids) == 1:
            source_observation_id = int(requested_ids[0])
            neighbor_ids = sorted(
                adjacency.get(int(source_observation_id), set()),
                key=lambda observation_id: (
                    self._lookup_edge_distance(
                        edge_distances=edge_distances,
                        from_observation_id=int(source_observation_id),
                        to_observation_id=int(observation_id),
                    ),
                    int(observation_id),
                ),
            )
            return [
                {
                    "source_observation_id": int(source_observation_id),
                    "target_observation_id": int(target_observation_id),
                    "node_ids": [int(source_observation_id), int(target_observation_id)],
                }
                for target_observation_id in neighbor_ids[: int(neighbor_count)]
            ]

        anchor_observation_id = int(requested_ids[0])
        paths: list[dict[str, Any]] = []
        for target_observation_id in requested_ids[1:]:
            node_ids = self._shortest_path(
                adjacency=adjacency,
                source_observation_id=int(anchor_observation_id),
                target_observation_id=int(target_observation_id),
            )
            if node_ids is None:
                raise ValueError(
                    f"raw mutual-kNN 图中找不到 observation {int(anchor_observation_id)} -> {int(target_observation_id)} 的连通路径"
                )
            paths.append(
                {
                    "source_observation_id": int(anchor_observation_id),
                    "target_observation_id": int(target_observation_id),
                    "node_ids": [int(value) for value in node_ids],
                }
            )
        return paths

    def _shortest_path(
        self,
        *,
        adjacency: dict[int, set[int]],
        source_observation_id: int,
        target_observation_id: int,
    ) -> list[int] | None:
        queue: deque[int] = deque([int(source_observation_id)])
        parent: dict[int, int | None] = {int(source_observation_id): None}
        while queue:
            current = int(queue.popleft())
            if current == int(target_observation_id):
                break
            for neighbor in sorted(adjacency.get(int(current), set())):
                if int(neighbor) in parent:
                    continue
                parent[int(neighbor)] = int(current)
                queue.append(int(neighbor))
        if int(target_observation_id) not in parent:
            return None
        result: list[int] = []
        cursor: int | None = int(target_observation_id)
        while cursor is not None:
            result.append(int(cursor))
            cursor = parent.get(int(cursor))
        result.reverse()
        return result

    def _build_raw_cluster_graph(
        self,
        *,
        raw_member_ids: list[int],
        raw_summary: dict[str, Any],
        vector_map: dict[int, np.ndarray[Any, np.dtype[np.float32]]],
        discovery_knn_k: int,
    ) -> tuple[dict[int, set[int]], dict[tuple[int, int], float]]:
        raw_member_set = {int(obs_id) for obs_id in raw_member_ids}
        edge_distances: dict[tuple[int, int], float] = {}
        adjacency: dict[int, set[int]] = {int(obs_id): set() for obs_id in raw_member_ids}

        mutual_knn_edges = list(raw_summary.get("mutual_knn_edges") or [])
        for item in mutual_knn_edges:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            a = int(item[0])
            b = int(item[1])
            if a == b or a not in raw_member_set or b not in raw_member_set:
                continue
            edge_key = _edge_key(a, b)
            if edge_key in edge_distances:
                continue
            if int(a) not in vector_map or int(b) not in vector_map:
                continue
            distance = self._cosine_distance(vector_map[int(a)], vector_map[int(b)])
            edge_distances[edge_key] = float(distance)
            adjacency[int(a)].add(int(b))
            adjacency[int(b)].add(int(a))

        if edge_distances:
            return adjacency, edge_distances

        # 历史 run 如果没有落 raw mutual-kNN 边，回退到按当前 run 的 snapshot/profile 重新计算。
        for observation_id in raw_member_ids:
            distance_rows = sorted(
                (
                    (
                        int(other_observation_id),
                        self._cosine_distance(
                            vector_map[int(observation_id)],
                            vector_map[int(other_observation_id)],
                        ),
                    )
                    for other_observation_id in raw_member_ids
                    if int(other_observation_id) != int(observation_id)
                ),
                key=lambda item: (float(item[1]), int(item[0])),
            )
            adjacency[int(observation_id)] = {
                int(item[0]) for item in distance_rows[: int(max(1, discovery_knn_k))]
            }

        mutual_adjacency: dict[int, set[int]] = {int(obs_id): set() for obs_id in raw_member_ids}
        edge_distances = {}
        for observation_id in raw_member_ids:
            for other_observation_id in adjacency.get(int(observation_id), set()):
                if int(observation_id) not in adjacency.get(int(other_observation_id), set()):
                    continue
                edge_key = _edge_key(int(observation_id), int(other_observation_id))
                if edge_key in edge_distances:
                    continue
                edge_distances[edge_key] = self._cosine_distance(
                    vector_map[int(edge_key[0])],
                    vector_map[int(edge_key[1])],
                )
                mutual_adjacency[int(edge_key[0])].add(int(edge_key[1]))
                mutual_adjacency[int(edge_key[1])].add(int(edge_key[0]))
        return mutual_adjacency, edge_distances

    def _build_vector_map(
        self, *, pool: dict[str, Any]
    ) -> dict[int, np.ndarray[Any, np.dtype[np.float32]]]:
        observation_ids = [int(item) for item in pool["observation_ids"].tolist()]
        vectors = np.asarray(pool["vectors"], dtype=np.float32)
        return {
            int(observation_id): np.asarray(vectors[index], dtype=np.float32)
            for index, observation_id in enumerate(observation_ids)
        }

    def _index_pool_by_observation(self, *, pool: dict[str, Any]) -> dict[int, int]:
        return {
            int(observation_id): index for index, observation_id in enumerate(pool["observation_ids"].tolist())
        }

    def _lookup_edge_distance(
        self,
        *,
        edge_distances: dict[tuple[int, int], float],
        from_observation_id: int,
        to_observation_id: int,
    ) -> float:
        edge_key = _edge_key(int(from_observation_id), int(to_observation_id))
        if edge_key not in edge_distances:
            raise ValueError(
                f"raw mutual-kNN 图缺少边: {int(from_observation_id)} -> {int(to_observation_id)}"
            )
        return float(edge_distances[edge_key])

    def _cosine_distance(
        self,
        left: np.ndarray[Any, np.dtype[np.float32]],
        right: np.ndarray[Any, np.dtype[np.float32]],
    ) -> float:
        left_vector = np.asarray(left, dtype=np.float32)
        right_vector = np.asarray(right, dtype=np.float32)
        denominator = float(np.linalg.norm(left_vector) * np.linalg.norm(right_vector))
        if denominator <= 0.0:
            return 1.0
        similarity = float(np.dot(left_vector, right_vector) / denominator)
        similarity = max(-1.0, min(1.0, similarity))
        return float(1.0 - similarity)

    def _select_neighbors(
        self,
        *,
        pool: dict[str, Any],
        cluster_meta: dict[int, dict[str, Any]],
        run_member_context: dict[int, dict[str, Any]],
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
                    run_member_context=run_member_context,
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
        run_member_context: dict[int, dict[str, Any]],
        observation_index: int,
        distance: float | None,
    ) -> _ObservationRecord:
        observation_id = int(pool["observation_ids"][int(observation_index)])
        cluster_info = cluster_meta.get(observation_id, {})
        run_context = run_member_context.get(observation_id, {})
        return _ObservationRecord(
            observation_id=observation_id,
            photo_id=int(pool["photo_ids"][int(observation_index)]),
            quality_score=float(pool["quality_scores"][int(observation_index)]),
            primary_path=str(pool["primary_paths"][int(observation_index)]),
            cluster_id=(
                int(run_context["cluster_id"])
                if run_context.get("cluster_id") is not None
                else int(cluster_info["cluster_id"])
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
            run_id=int(run_context["run_id"]) if run_context.get("run_id") is not None else None,
            observation_snapshot_id=(
                int(run_context["observation_snapshot_id"])
                if run_context.get("observation_snapshot_id") is not None
                else None
            ),
            observation_profile_id=(
                int(run_context["observation_profile_id"])
                if run_context.get("observation_profile_id") is not None
                else None
            ),
            cluster_profile_id=(
                int(run_context["cluster_profile_id"])
                if run_context.get("cluster_profile_id") is not None
                else None
            ),
            cluster_stage=(
                str(run_context["cluster_stage"])
                if run_context.get("cluster_stage") is not None
                else None
            ),
            member_role=(
                str(run_context["member_role"])
                if run_context.get("member_role") is not None
                else None
            ),
            decision_status=(
                str(run_context["decision_status"])
                if run_context.get("decision_status") is not None
                else None
            ),
            publish_state=(
                str(run_context["publish_state"])
                if run_context.get("publish_state") is not None
                else None
            ),
            is_selected_trusted_seed=(
                int(run_context["is_selected_trusted_seed"])
                if run_context.get("is_selected_trusted_seed") is not None
                else None
            ),
            seed_rank=(
                int(run_context["seed_rank"])
                if run_context.get("seed_rank") is not None
                else None
            ),
            is_representative=(
                int(run_context["is_representative"])
                if run_context.get("is_representative") is not None
                else None
            ),
            nearest_competing_cluster_distance=(
                float(run_context["nearest_competing_cluster_distance"])
                if run_context.get("nearest_competing_cluster_distance") is not None
                else None
            ),
            separation_gap=(
                float(run_context["separation_gap"])
                if run_context.get("separation_gap") is not None
                else None
            ),
            exclusion_reason=(
                str(run_context["exclusion_reason"])
                if run_context.get("exclusion_reason") is not None
                else None
            ),
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
            "run_id": int(record.run_id) if record.run_id is not None else None,
            "observation_snapshot_id": (
                int(record.observation_snapshot_id)
                if record.observation_snapshot_id is not None
                else None
            ),
            "observation_profile_id": (
                int(record.observation_profile_id)
                if record.observation_profile_id is not None
                else None
            ),
            "cluster_profile_id": (
                int(record.cluster_profile_id) if record.cluster_profile_id is not None else None
            ),
            "cluster_stage": record.cluster_stage,
            "member_role": record.member_role,
            "decision_status": record.decision_status,
            "publish_state": record.publish_state,
            "is_selected_trusted_seed": (
                int(record.is_selected_trusted_seed)
                if record.is_selected_trusted_seed is not None
                else None
            ),
            "seed_rank": int(record.seed_rank) if record.seed_rank is not None else None,
            "is_representative": (
                int(record.is_representative) if record.is_representative is not None else None
            ),
            "nearest_competing_cluster_distance": (
                float(record.nearest_competing_cluster_distance)
                if record.nearest_competing_cluster_distance is not None
                else None
            ),
            "separation_gap": (
                float(record.separation_gap) if record.separation_gap is not None else None
            ),
            "exclusion_reason": record.exclusion_reason,
            "crop_file": crop_file,
            "preview_file": preview_file,
        }
        return payload

    def _export_record_assets_with_dir(
        self,
        *,
        record: _ObservationRecord,
        output_dir: Path,
        file_prefix: str,
        asset_dir: str,
    ) -> dict[str, Any]:
        payload = self._export_record_assets(
            record=record,
            output_dir=output_dir,
            file_prefix=file_prefix,
        )
        payload["asset_dir"] = str(asset_dir)
        return payload

    def _resolve_effective_run_id(self, run_id: int | None) -> int:
        if run_id is not None:
            return int(run_id)
        effective_run_id = self._resolve_review_target_run_id_or_none()
        if effective_run_id is None:
            raise ValueError("未提供 run_id，且当前 workspace 没有 review target run")
        return int(effective_run_id)

    def _resolve_review_target_run_id_or_none(self) -> int | None:
        conn = connect_db(self.db_path)
        try:
            row = conn.execute(
                """
                SELECT id
                FROM identity_cluster_run
                WHERE is_review_target = 1
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            return int(row["id"])
        finally:
            conn.close()

    def _load_run_member_context(
        self, *, run_id: int
    ) -> tuple[dict[str, Any], dict[int, dict[str, Any]]]:
        conn = connect_db(self.db_path)
        try:
            run_row = conn.execute(
                """
                SELECT r.id AS run_id,
                       r.observation_snapshot_id,
                       r.cluster_profile_id,
                       s.observation_profile_id
                FROM identity_cluster_run AS r
                JOIN identity_observation_snapshot AS s
                  ON s.id = r.observation_snapshot_id
                WHERE r.id = ?
                """,
                (int(run_id),),
            ).fetchone()
            if run_row is None:
                raise ValueError(f"run 不存在: {int(run_id)}")

            rows = conn.execute(
                """
                SELECT m.observation_id,
                       c.id AS cluster_id,
                       c.cluster_stage,
                       m.member_role,
                       m.decision_status,
                       r.publish_state,
                       m.is_selected_trusted_seed,
                       m.seed_rank,
                       m.is_representative,
                       m.nearest_competing_cluster_distance,
                       m.separation_gap,
                       m.decision_reason_code AS exclusion_reason
                FROM identity_cluster AS c
                JOIN identity_cluster_member AS m
                  ON m.cluster_id = c.id
                LEFT JOIN identity_cluster_resolution AS r
                  ON r.cluster_id = c.id
                WHERE c.run_id = ?
                ORDER BY CASE c.cluster_stage
                           WHEN 'final' THEN 0
                           WHEN 'cleaned' THEN 1
                           ELSE 2
                         END ASC,
                         c.id ASC,
                         m.id ASC
                """,
                (int(run_id),),
            ).fetchall()
        finally:
            conn.close()

        summary = {
            "run_id": int(run_row["run_id"]),
            "observation_snapshot_id": int(run_row["observation_snapshot_id"]),
            "observation_profile_id": int(run_row["observation_profile_id"]),
            "cluster_profile_id": int(run_row["cluster_profile_id"]),
        }
        context_by_observation: dict[int, dict[str, Any]] = {}
        for row in rows:
            observation_id = int(row["observation_id"])
            if observation_id in context_by_observation:
                continue
            context_by_observation[observation_id] = {
                "run_id": int(run_row["run_id"]),
                "observation_snapshot_id": int(run_row["observation_snapshot_id"]),
                "observation_profile_id": int(run_row["observation_profile_id"]),
                "cluster_profile_id": int(run_row["cluster_profile_id"]),
                "cluster_id": int(row["cluster_id"]),
                "cluster_stage": str(row["cluster_stage"]),
                "member_role": str(row["member_role"]),
                "decision_status": str(row["decision_status"]),
                "publish_state": None if row["publish_state"] is None else str(row["publish_state"]),
                "is_selected_trusted_seed": int(row["is_selected_trusted_seed"]),
                "seed_rank": None if row["seed_rank"] is None else int(row["seed_rank"]),
                "is_representative": int(row["is_representative"]),
                "nearest_competing_cluster_distance": (
                    None
                    if row["nearest_competing_cluster_distance"] is None
                    else float(row["nearest_competing_cluster_distance"])
                ),
                "separation_gap": (
                    None if row["separation_gap"] is None else float(row["separation_gap"])
                ),
                "exclusion_reason": (
                    None if row["exclusion_reason"] is None else str(row["exclusion_reason"])
                ),
            }
        return summary, context_by_observation

    def _list_cluster_member_observation_ids(self, *, run_id: int, cluster_id: int) -> list[int]:
        conn = connect_db(self.db_path)
        try:
            rows = conn.execute(
                """
                SELECT m.observation_id
                FROM identity_cluster_member AS m
                JOIN identity_cluster AS c
                  ON c.id = m.cluster_id
                WHERE c.id = ?
                  AND c.run_id = ?
                ORDER BY m.id ASC
                """,
                (int(cluster_id), int(run_id)),
            ).fetchall()
        finally:
            conn.close()
        return [int(row["observation_id"]) for row in rows]

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

    def _load_json(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            return dict(payload)
        if payload is None:
            return {}
        return json.loads(str(payload))

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

    def _render_connectivity_index_html(
        self,
        *,
        summary: dict[str, Any],
        sections: list[str],
    ) -> str:
        target_cards = [
            self._render_connectivity_record_card(
                payload=target,
                label=f"目标 {index}",
            )
            for index, target in enumerate(summary["targets"], start=1)
        ]
        return "\n".join(
            [
                "<!DOCTYPE html>",
                "<html lang=\"zh-CN\">",
                "<head>",
                "  <meta charset=\"utf-8\" />",
                "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />",
                "  <title>Cluster 连通路径核对</title>",
                "  <style>",
                "    body { font-family: -apple-system, BlinkMacSystemFont, \"PingFang SC\", sans-serif; margin: 24px; background: #f6f7f4; color: #182018; }",
                "    h1, h2, p, pre { margin: 0; }",
                "    .meta { margin-top: 8px; color: #526052; font-size: 14px; line-height: 1.45; word-break: break-word; }",
                "    .target, .path { margin-top: 28px; padding: 18px; border: 1px solid #d8dfd3; border-radius: 16px; background: #ffffff; }",
                "    .cards { margin-top: 16px; display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; }",
                "    .edge-stack { margin-top: 16px; display: grid; gap: 14px; }",
                "    .edge-row { display: grid; grid-template-columns: minmax(260px, 1fr) 100px minmax(260px, 1fr); gap: 14px; align-items: stretch; }",
                "    .edge-metric { display: grid; place-items: center; font-weight: 700; color: #35543a; border: 1px dashed #c9d8c8; border-radius: 14px; background: #f7fbf5; padding: 12px; text-align: center; }",
                "    .card { border: 1px solid #e4eadf; border-radius: 14px; padding: 12px; background: #fbfcfa; display: grid; gap: 10px; }",
                "    .card.is-requested { border-color: #9ab58b; background: #f5faf1; }",
                "    .label { font-weight: 700; }",
                "    .detail { font-size: 13px; color: #4c5a4c; line-height: 1.45; word-break: break-word; }",
                "    .images { display: grid; grid-template-columns: 120px 1fr; gap: 10px; align-items: start; }",
                "    .images img { width: 100%; border-radius: 10px; border: 1px solid #d6dfd1; background: #eef4ea; }",
                "    .images img.preview { object-fit: contain; background: #f1f4ef; }",
                "    code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }",
                "    pre { margin-top: 12px; padding: 12px; border-radius: 12px; background: #f2f5ef; overflow-x: auto; }",
                "  </style>",
                "</head>",
                "<body>",
                "  <h1>Cluster 连通路径核对</h1>",
                f"  <p class=\"meta\">workspace: <code>{html.escape(str(summary['workspace']))}</code></p>",
                f"  <p class=\"meta\">db_path: <code>{html.escape(str(summary['db_path']))}</code></p>",
                (
                    "  <p class=\"meta\">"
                    f"run #{int(summary['run_id'])} · cluster #{int(summary['cluster_id'])} · "
                    f"stage={html.escape(str(summary['cluster_stage']))} · "
                    f"raw scope #{int(summary['graph_scope']['raw_cluster_id'])} · "
                    f"k={int(summary['graph_scope']['discovery_knn_k'])} · "
                    f"raw members={int(summary['graph_scope']['raw_member_count'])} · "
                    f"raw mutual-kNN edges={int(summary['graph_scope']['mutual_knn_edge_count'])}"
                    "</p>"
                ),
                "  <section class=\"target\">",
                "    <h2>目标 Observation</h2>",
                "    <div class=\"cards\">",
                *target_cards,
                "    </div>",
                "  </section>",
                "  <section class=\"target\">",
                "    <h2>Lineage Chain</h2>",
                "    <pre>"
                + html.escape(json.dumps(summary["lineage_chain"], ensure_ascii=False, indent=2))
                + "</pre>",
                "  </section>",
                *sections,
                "</body>",
                "</html>",
            ]
        )

    def _render_connectivity_path_section(
        self,
        *,
        path_payload: dict[str, Any],
    ) -> str:
        edge_rows: list[str] = []
        for edge in path_payload["edges"]:
            edge_rows.append(
                "\n".join(
                    [
                        "      <div class=\"edge-row\">",
                        self._render_connectivity_record_card(
                            payload=edge["from"],
                            label=f"边 {int(edge['rank'])} · 起点",
                        ),
                        (
                            "        <div class=\"edge-metric\">"
                            f"distance<br />{_format_metric(float(edge['distance']))}"
                            "</div>"
                        ),
                        self._render_connectivity_record_card(
                            payload=edge["to"],
                            label=f"边 {int(edge['rank'])} · 终点",
                        ),
                        "      </div>",
                    ]
                )
            )

        node_path = " -> ".join(str(int(node_id)) for node_id in path_payload["node_ids"])
        return "\n".join(
            [
                "  <section class=\"path\">",
                (
                    f"    <h2>obs {int(path_payload['source_observation_id'])} -> "
                    f"obs {int(path_payload['target_observation_id'])} / "
                    f"{int(path_payload['hop_count'])} hops</h2>"
                ),
                (
                    "    <p class=\"meta\">"
                    f"node path: <code>{html.escape(node_path)}</code> · "
                    f"total distance {_format_metric(float(path_payload['total_distance']))}"
                    "</p>"
                ),
                "    <div class=\"edge-stack\">",
                *edge_rows,
                "    </div>",
                "  </section>",
            ]
        )

    def _render_connectivity_record_card(
        self,
        *,
        payload: dict[str, Any],
        label: str,
    ) -> str:
        card_class = "card is-requested" if bool(payload.get("is_requested_target")) else "card"
        crop_src = f"{payload['asset_dir']}/{payload['crop_file']}"
        preview_src = f"{payload['asset_dir']}/{payload['preview_file']}"
        cluster_bits = [
            f"cluster #{_format_optional_int(payload.get('cluster_id'))}",
            f"stage={html.escape(str(payload.get('cluster_stage') or '(null)'))}",
            f"role={html.escape(str(payload.get('member_role') or '(null)'))}",
            f"decision={html.escape(str(payload.get('decision_status') or '(null)'))}",
        ]
        return "\n".join(
            [
                f"        <article class=\"{card_class}\">",
                f"          <div class=\"label\">{html.escape(label)}</div>",
                (
                    "          <div class=\"detail\">"
                    f"obs {int(payload['observation_id'])} · "
                    f"photo {int(payload['photo_id'])} · "
                    f"quality {_format_metric(float(payload['quality_score']))}"
                    "</div>"
                ),
                "          <div class=\"detail\">" + " · ".join(cluster_bits) + "</div>",
                (
                    "          <div class=\"detail\"><code>"
                    + html.escape(str(payload["primary_path"]))
                    + "</code></div>"
                ),
                "          <div class=\"images\">",
                f"            <img src=\"{html.escape(crop_src)}\" alt=\"crop\" />",
                f"            <img class=\"preview\" src=\"{html.escape(preview_src)}\" alt=\"preview\" />",
                "          </div>",
                "        </article>",
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


def _edge_key(left: int, right: int) -> tuple[int, int]:
    return (int(min(left, right)), int(max(left, right)))


def _format_metric(value: float) -> str:
    return f"{float(value):.2f}"


def _format_optional_int(value: int | None) -> str:
    if value is None:
        return "(null)"
    return str(int(value))
