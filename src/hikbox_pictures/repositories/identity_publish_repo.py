from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]


class IdentityPublishRepo:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        artifact_root: Path,
        live_ann_artifact_path: Path,
        run_ann_prepare_failure_run_ids: set[int] | None = None,
        publish_stage_failure_reasons: dict[int, str] | None = None,
    ) -> None:
        self.conn = conn
        self.artifact_root = Path(artifact_root)
        self.live_ann_artifact_path = Path(live_ann_artifact_path)
        self._run_ann_prepare_failure_run_ids = run_ann_prepare_failure_run_ids
        self._publish_stage_failure_reasons = publish_stage_failure_reasons

    def get_run_required(self, run_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT *
            FROM identity_cluster_run
            WHERE id = ?
            """,
            (int(run_id),),
        ).fetchone()
        if row is None:
            raise ValueError(f"run 不存在: {int(run_id)}")
        return dict(row)

    def get_cluster_profile_for_run(self, *, run_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT p.*
            FROM identity_cluster_run AS r
            JOIN identity_cluster_profile AS p ON p.id = r.cluster_profile_id
            WHERE r.id = ?
            """,
            (int(run_id),),
        ).fetchone()
        if row is None:
            raise ValueError(f"run cluster profile 不存在: {int(run_id)}")
        return dict(row)

    def list_prepare_candidates(self, *, run_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT
                c.id AS cluster_id,
                c.anchor_core_count,
                c.distinct_photo_count,
                c.compactness_p90,
                c.separation_gap,
                c.boundary_ratio,
                c.cluster_state,
                r.resolution_state,
                r.trusted_seed_count,
                r.trusted_seed_candidate_count
            FROM identity_cluster AS c
            JOIN identity_cluster_resolution AS r ON r.cluster_id = c.id
            WHERE c.run_id = ?
              AND c.cluster_stage = 'final'
              AND c.cluster_state = 'active'
              AND r.resolution_state IN ('review_pending', 'unresolved', 'materialized')
            ORDER BY c.id ASC
            """,
            (int(run_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    def materialize_gate_reason(self, *, candidate: dict[str, Any], profile: dict[str, Any]) -> str | None:
        if int(candidate["anchor_core_count"] or 0) < int(profile["materialize_min_anchor_core_count"]):
            return "anchor_core_below_materialize_min"
        if int(candidate["distinct_photo_count"] or 0) < int(profile["materialize_min_distinct_photo_count"]):
            return "distinct_photo_below_materialize_min"
        compactness_p90 = float(candidate["compactness_p90"] or 0.0)
        if compactness_p90 > float(profile["materialize_max_compactness_p90"]):
            return "compactness_p90_exceeds_materialize_max"
        separation_gap = float(candidate["separation_gap"] or 0.0)
        if separation_gap < float(profile["materialize_min_separation_gap"]):
            return "separation_gap_below_materialize_min"
        boundary_ratio = float(candidate["boundary_ratio"] or 0.0)
        if boundary_ratio > float(profile["materialize_max_boundary_ratio"]):
            return "boundary_ratio_exceeds_materialize_max"
        if int(candidate["trusted_seed_count"] or 0) < int(profile["trusted_seed_min_count"]):
            return "trusted_seed_count_below_materialize_min"
        return None

    def prepare_cluster_bundle(self, *, cluster_id: int, run_id: int) -> dict[str, Any]:
        threshold_profile_id = self._get_active_threshold_profile_id()
        members = self._list_retained_members(cluster_id=int(cluster_id))
        if not members:
            manifest = {
                "run_id": int(run_id),
                "cluster_id": int(cluster_id),
                "person_publish_plan": {},
                "prototype": {"status": "failed"},
                "ann": {"status": "failed"},
                "checksum": "",
            }
            self._persist_cluster_manifest(cluster_id=int(cluster_id), manifest=manifest)
            return manifest

        trusted_seed_rows = [row for row in members if int(row["is_selected_trusted_seed"] or 0) == 1]
        trusted_seed_rows.sort(
            key=lambda row: (
                self._seed_role_order(str(row["member_role"])),
                -float(row["quality_score"] or 0.0),
                -float(row["support_ratio"] or 0.0),
                float(row["distance_to_medoid"] or 0.0),
                int(row["observation_id"]),
            )
        )
        self._rewrite_seed_ranks(
            cluster_id=int(cluster_id),
            ordered_observation_ids=[int(row["observation_id"]) for row in trusted_seed_rows],
        )

        cover_observation_id = int(
            max(
                members,
                key=lambda row: (
                    float(row["quality_score"] or 0.0),
                    -int(row["observation_id"]),
                ),
            )["observation_id"]
        )
        assignments = [
            {
                "face_observation_id": int(row["observation_id"]),
                "quality_score_snapshot": float(row["quality_score"] or 0.0),
                "assignment_source": "bootstrap",
            }
            for row in members
        ]
        trusted_seeds = [
            {
                "face_observation_id": int(row["observation_id"]),
                "trust_source": "bootstrap_seed",
                "trust_score": 1.0,
                "quality_score_snapshot": float(row["quality_score"] or 0.0),
                "seed_rank": index,
            }
            for index, row in enumerate(trusted_seed_rows, start=1)
        ]
        prototype_vector = self._build_centroid_vector(
            [int(row["observation_id"]) for row in trusted_seed_rows],
        )
        prototype_checksum = ""
        if prototype_vector is not None:
            prototype_checksum = hashlib.sha256(prototype_vector.tobytes()).hexdigest()

        payload: dict[str, Any] = {
            "run_id": int(run_id),
            "cluster_id": int(cluster_id),
            "person_publish_plan": {
                "cover_observation_id": int(cover_observation_id),
                "threshold_profile_id": int(threshold_profile_id),
                "assignments": assignments,
                "trusted_seeds": trusted_seeds,
            },
            "prototype": {
                "status": "prepared" if prototype_vector is not None else "failed",
                "vector_checksum": prototype_checksum,
            },
            "ann": {
                "status": "prepared" if prototype_vector is not None else "failed",
            },
            "prepared_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        payload["checksum"] = self._manifest_checksum(payload)
        self._persist_cluster_manifest(cluster_id=int(cluster_id), manifest=payload)
        return payload

    def verify_cluster_bundle_manifest(self, manifest: dict[str, Any]) -> bool:
        checksum = str(manifest.get("checksum") or "")
        if not checksum:
            return False
        expected = self._manifest_checksum({k: v for k, v in manifest.items() if k != "checksum"})
        if checksum != expected:
            return False
        publish_plan = manifest.get("person_publish_plan")
        if not isinstance(publish_plan, dict):
            return False
        prototype = manifest.get("prototype")
        ann = manifest.get("ann")
        if not isinstance(prototype, dict) or str(prototype.get("status") or "") != "prepared":
            return False
        if not isinstance(ann, dict) or str(ann.get("status") or "") != "prepared":
            return False
        assignments = publish_plan.get("assignments")
        trusted_seeds = publish_plan.get("trusted_seeds")
        if not isinstance(assignments, list) or not assignments:
            return False
        if not isinstance(trusted_seeds, list) or not trusted_seeds:
            return False
        cover_observation_id = publish_plan.get("cover_observation_id")
        assigned_ids = {int(item.get("face_observation_id")) for item in assignments if "face_observation_id" in item}
        if int(cover_observation_id) not in assigned_ids:
            return False
        return True

    def mark_cluster_review_pending(self, *, cluster_id: int, reason: str) -> None:
        self.conn.execute(
            """
            UPDATE identity_cluster_resolution
            SET resolution_state = 'review_pending',
                resolution_reason = ?,
                publish_state = 'not_applicable',
                publish_failure_reason = NULL,
                prepared_bundle_manifest_json = '{}',
                prototype_status = 'not_applicable',
                ann_status = 'not_applicable',
                updated_at = CURRENT_TIMESTAMP
            WHERE cluster_id = ?
            """,
            (str(reason), int(cluster_id)),
        )

    def prepare_run_ann_bundle(self, *, run_id: int, prepared_cluster_ids: list[int]) -> dict[str, Any]:
        if self._run_ann_prepare_failure_run_ids is not None and int(run_id) in self._run_ann_prepare_failure_run_ids:
            return {
                "artifact_path": "",
                "artifact_checksum": "",
                "cluster_count": 0,
            }

        run_dir = self.artifact_root / f"run-{int(run_id)}"
        run_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = run_dir / "prepared_ann_bundle.npz"
        vectors: list[np.ndarray[Any, np.dtype[np.float32]]] = []
        cluster_ids: list[int] = []
        for cluster_id in prepared_cluster_ids:
            manifest = self.get_cluster_bundle_manifest(cluster_id=int(cluster_id))
            publish_plan = manifest.get("person_publish_plan") if isinstance(manifest, dict) else None
            trusted_seeds = []
            if isinstance(publish_plan, dict):
                trusted_seeds = publish_plan.get("trusted_seeds") or []
            obs_ids = [int(item["face_observation_id"]) for item in trusted_seeds if "face_observation_id" in item]
            vector = self._build_centroid_vector(obs_ids)
            if vector is None:
                continue
            cluster_ids.append(int(cluster_id))
            vectors.append(vector)

        cluster_ids_arr = np.asarray(cluster_ids, dtype=np.int64)
        if vectors:
            vector_arr = np.vstack(vectors).astype(np.float32, copy=False)
        else:
            vector_arr = np.empty((0, 0), dtype=np.float32)
        with artifact_path.open("wb") as fp:
            np.savez_compressed(
                fp,
                cluster_ids=cluster_ids_arr,
                person_ids=cluster_ids_arr,
                vectors=vector_arr,
            )
        checksum = self._file_checksum(artifact_path)
        return {
            "artifact_path": str(artifact_path),
            "artifact_checksum": str(checksum),
            "cluster_count": int(len(cluster_ids)),
        }

    def build_live_ann_artifact_from_prepared(
        self,
        *,
        run_id: int,
        prepared_ann_path: Path,
        prepared_ann_checksum: str,
        cluster_person_pairs: list[tuple[int, int]],
    ) -> dict[str, Any]:
        artifact_path = Path(prepared_ann_path)
        if not artifact_path.exists():
            raise ValueError(f"prepared ann artifact 不存在: {artifact_path}")
        actual_checksum = self._file_checksum(artifact_path)
        if str(actual_checksum) != str(prepared_ann_checksum):
            raise ValueError(
                "prepared ann artifact checksum 不匹配，拒绝构建 live artifact: "
                f"expected={prepared_ann_checksum}, actual={actual_checksum}"
            )

        with np.load(artifact_path, allow_pickle=False) as payload:
            cluster_ids = payload["cluster_ids"].astype(np.int64, copy=False)
            vectors = payload["vectors"].astype(np.float32, copy=False)
        if cluster_ids.ndim != 1 or vectors.ndim != 2 or int(cluster_ids.size) != int(vectors.shape[0]):
            raise ValueError("prepared ann artifact 结构无效")

        vector_by_cluster: dict[int, np.ndarray[Any, np.dtype[np.float32]]] = {}
        for index, cluster_id in enumerate(cluster_ids.tolist()):
            vector_by_cluster[int(cluster_id)] = vectors[int(index)].astype(np.float32, copy=False)

        live_cluster_ids: list[int] = []
        live_person_ids: list[int] = []
        live_vectors: list[np.ndarray[Any, np.dtype[np.float32]]] = []
        for cluster_id, person_id in cluster_person_pairs:
            vector = vector_by_cluster.get(int(cluster_id))
            if vector is None:
                continue
            live_cluster_ids.append(int(cluster_id))
            live_person_ids.append(int(person_id))
            live_vectors.append(vector)

        if not live_vectors:
            raise ValueError("live ann artifact 缺少可发布向量")

        run_dir = self.artifact_root / f"run-{int(run_id)}"
        run_dir.mkdir(parents=True, exist_ok=True)
        live_path = run_dir / "live_ann_bundle.npz"
        with live_path.open("wb") as fp:
            np.savez_compressed(
                fp,
                cluster_ids=np.asarray(live_cluster_ids, dtype=np.int64),
                person_ids=np.asarray(live_person_ids, dtype=np.int64),
                vectors=np.vstack(live_vectors).astype(np.float32, copy=False),
            )
        checksum = self._file_checksum(live_path)
        return {
            "artifact_path": str(live_path),
            "artifact_checksum": str(checksum),
            "person_count": int(len(live_person_ids)),
        }

    def verify_run_ann_manifest(self, ann_manifest: dict[str, Any]) -> bool:
        artifact_path_raw = str(ann_manifest.get("artifact_path") or "").strip()
        expected_checksum = str(ann_manifest.get("artifact_checksum") or "").strip()
        if not artifact_path_raw or not expected_checksum:
            return False
        artifact_path = Path(artifact_path_raw)
        if not artifact_path.exists():
            return False
        return self._file_checksum(artifact_path) == expected_checksum

    def mark_run_prepare_failed_and_rollback_candidates(
        self,
        *,
        run_id: int,
        candidate_cluster_ids: list[int],
        reason: str,
    ) -> None:
        for cluster_id in candidate_cluster_ids:
            self.mark_cluster_review_pending(cluster_id=int(cluster_id), reason=str(reason))
        self.conn.execute(
            """
            UPDATE identity_cluster_run
            SET prepared_ann_manifest_json = '{}',
                prepared_artifact_root = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (int(run_id),),
        )

    def mark_run_prepared(self, *, run_id: int, cluster_ids: list[int], ann_manifest: dict[str, Any]) -> None:
        if cluster_ids:
            placeholders = ", ".join("?" for _ in cluster_ids)
            self.conn.execute(
                f"""
                UPDATE identity_cluster_resolution
                SET resolution_state = 'materialized',
                    publish_state = 'prepared',
                    publish_failure_reason = NULL,
                    prototype_status = 'prepared',
                    ann_status = 'prepared',
                    updated_at = CURRENT_TIMESTAMP
                WHERE cluster_id IN ({placeholders})
                """,
                tuple(int(cluster_id) for cluster_id in cluster_ids),
            )

        self.conn.execute(
            """
            UPDATE identity_cluster_run
            SET prepared_ann_manifest_json = ?,
                prepared_artifact_root = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                json.dumps(ann_manifest or {}, ensure_ascii=False, sort_keys=True),
                str(Path(str(ann_manifest.get("artifact_path") or "")).parent)
                if ann_manifest.get("artifact_path")
                else None,
                int(run_id),
            ),
        )

    def get_cluster_bundle_manifest(self, *, cluster_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT prepared_bundle_manifest_json
            FROM identity_cluster_resolution
            WHERE cluster_id = ?
            """,
            (int(cluster_id),),
        ).fetchone()
        if row is None:
            raise ValueError(f"cluster resolution 不存在: {int(cluster_id)}")
        payload = self._load_json(row["prepared_bundle_manifest_json"])
        return payload

    def get_prepared_run_required_with_verified_manifest(self, run_id: int) -> dict[str, Any]:
        run = self.get_run_required(int(run_id))
        ann_manifest = self._load_json(run.get("prepared_ann_manifest_json"))
        if not self.verify_run_ann_manifest(ann_manifest):
            raise ValueError("prepared ann manifest checksum 不匹配")
        prepared_count_row = self.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM identity_cluster_resolution AS r
            JOIN identity_cluster AS c ON c.id = r.cluster_id
            WHERE c.run_id = ?
              AND c.cluster_stage = 'final'
              AND r.publish_state = 'prepared'
              AND r.resolution_state = 'materialized'
            """,
            (int(run_id),),
        ).fetchone()
        if prepared_count_row is None or int(prepared_count_row["c"]) <= 0:
            raise ValueError(f"run 未准备可发布 cluster: {int(run_id)}")
        return {
            "run": run,
            "prepared_ann_path": str(ann_manifest.get("artifact_path") or ""),
            "prepared_ann_checksum": str(ann_manifest.get("artifact_checksum") or ""),
            "prepared_ann_manifest": ann_manifest,
        }

    def get_materialization_owner(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM identity_cluster_run
            WHERE is_materialization_owner = 1
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row is not None else None

    def clear_materialization_owner(self) -> None:
        self.conn.execute(
            """
            UPDATE identity_cluster_run
            SET is_materialization_owner = 0,
                updated_at = CURRENT_TIMESTAMP
            WHERE is_materialization_owner = 1
            """
        )

    def set_materialization_owner(self, *, run_id: int) -> None:
        self.clear_materialization_owner()
        self.conn.execute(
            """
            UPDATE identity_cluster_run
            SET is_materialization_owner = 1,
                activated_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (int(run_id),),
        )

    def mark_run_activated(self, *, run_id: int) -> None:
        self.conn.execute(
            """
            UPDATE identity_cluster_run
            SET activated_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (int(run_id),),
        )

    def list_prepared_publish_bundles(self, *, run_id: int) -> list[dict[str, Any]]:
        if self._publish_stage_failure_reasons is not None and int(run_id) in self._publish_stage_failure_reasons:
            reason = self._publish_stage_failure_reasons[int(run_id)]
            raise RuntimeError(f"publish stage failure: {reason}")

        rows = self.conn.execute(
            """
            SELECT r.cluster_id, r.prepared_bundle_manifest_json
            FROM identity_cluster_resolution AS r
            JOIN identity_cluster AS c ON c.id = r.cluster_id
            WHERE c.run_id = ?
              AND c.cluster_stage = 'final'
              AND c.cluster_state = 'active'
              AND r.resolution_state = 'materialized'
              AND r.publish_state = 'prepared'
            ORDER BY r.cluster_id ASC
            """,
            (int(run_id),),
        ).fetchall()
        bundles: list[dict[str, Any]] = []
        for row in rows:
            manifest = self._load_json(row["prepared_bundle_manifest_json"])
            bundles.append(
                {
                    "cluster_id": int(row["cluster_id"]),
                    "manifest": manifest,
                    "person_publish_plan": manifest.get("person_publish_plan") if isinstance(manifest, dict) else {},
                }
            )
        return bundles

    def mark_cluster_published(self, *, cluster_id: int, person_id: int) -> None:
        self.conn.execute(
            """
            UPDATE identity_cluster_resolution
            SET publish_state = 'published',
                publish_failure_reason = NULL,
                person_id = ?,
                prototype_status = 'published',
                ann_status = 'published',
                updated_at = CURRENT_TIMESTAMP
            WHERE cluster_id = ?
            """,
            (int(person_id), int(cluster_id)),
        )

    def mark_clusters_publish_failed_for_activation(self, *, run_id: int, reason: str) -> None:
        self.conn.execute(
            """
            UPDATE identity_cluster_resolution
            SET publish_state = 'publish_failed',
                publish_failure_reason = ?,
                ann_status = 'failed',
                updated_at = CURRENT_TIMESTAMP
            WHERE cluster_id IN (
                SELECT c.id
                FROM identity_cluster AS c
                WHERE c.run_id = ?
                  AND c.cluster_stage = 'final'
                  AND c.cluster_state = 'active'
            )
              AND resolution_state IN ('materialized', 'review_pending', 'unresolved')
            """,
            (str(reason), int(run_id)),
        )

    def _list_retained_members(self, *, cluster_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT
                m.observation_id,
                m.member_role,
                m.support_ratio,
                m.distance_to_medoid,
                m.is_selected_trusted_seed,
                fo.quality_score
            FROM identity_cluster_member AS m
            JOIN face_observation AS fo ON fo.id = m.observation_id
            WHERE m.cluster_id = ?
              AND m.decision_status = 'retained'
              AND m.member_role IN ('anchor_core', 'core', 'boundary', 'attachment')
            ORDER BY m.observation_id ASC
            """,
            (int(cluster_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    def _rewrite_seed_ranks(self, *, cluster_id: int, ordered_observation_ids: list[int]) -> None:
        self.conn.execute(
            """
            UPDATE identity_cluster_member
            SET is_selected_trusted_seed = 0,
                seed_rank = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE cluster_id = ?
            """,
            (int(cluster_id),),
        )
        for rank, obs_id in enumerate(ordered_observation_ids, start=1):
            self.conn.execute(
                """
                UPDATE identity_cluster_member
                SET is_selected_trusted_seed = 1,
                    seed_rank = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE cluster_id = ?
                  AND observation_id = ?
                """,
                (int(rank), int(cluster_id), int(obs_id)),
            )

    def _build_centroid_vector(self, observation_ids: list[int]) -> np.ndarray[Any, np.dtype[np.float32]] | None:
        if not observation_ids:
            return None
        placeholders = ", ".join("?" for _ in observation_ids)
        rows = self.conn.execute(
            f"""
            SELECT vector_blob
            FROM face_embedding
            WHERE face_observation_id IN ({placeholders})
              AND feature_type = 'face'
              AND model_key = 'insightface'
              AND normalized = 1
            ORDER BY face_observation_id ASC
            """,
            tuple(int(obs_id) for obs_id in observation_ids),
        ).fetchall()
        vectors: list[np.ndarray[Any, np.dtype[np.float32]]] = []
        for row in rows:
            vector_blob = row["vector_blob"]
            if not isinstance(vector_blob, (bytes, bytearray, memoryview)):
                continue
            vector = np.frombuffer(vector_blob, dtype=np.float32).copy()
            if vector.ndim != 1 or vector.size <= 0:
                continue
            vectors.append(vector.astype(np.float32, copy=False))
        if not vectors:
            return None
        centroid = np.mean(np.vstack(vectors), axis=0).astype(np.float32, copy=False)
        norm = float(np.linalg.norm(centroid))
        if norm > 0.0:
            centroid = centroid / norm
        return centroid

    def _get_active_threshold_profile_id(self) -> int:
        row = self.conn.execute(
            """
            SELECT id
            FROM identity_threshold_profile
            WHERE active = 1
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            row = self.conn.execute(
                """
                SELECT id
                FROM identity_threshold_profile
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            raise ValueError("identity_threshold_profile 不存在")
        return int(row["id"])

    def _persist_cluster_manifest(self, *, cluster_id: int, manifest: dict[str, Any]) -> None:
        prototype_status = "prepared"
        ann_status = "prepared"
        if not self.verify_cluster_bundle_manifest(manifest):
            prototype_status = "failed"
            ann_status = "failed"
        self.conn.execute(
            """
            UPDATE identity_cluster_resolution
            SET prepared_bundle_manifest_json = ?,
                prototype_status = ?,
                ann_status = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE cluster_id = ?
            """,
            (
                json.dumps(manifest or {}, ensure_ascii=False, sort_keys=True),
                str(prototype_status),
                str(ann_status),
                int(cluster_id),
            ),
        )

    def _manifest_checksum(self, payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _file_checksum(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _load_json(self, raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, str) or raw.strip() == "":
            return {}
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _seed_role_order(member_role: str) -> int:
        order = {
            "anchor_core": 0,
            "core": 1,
            "boundary": 2,
            "attachment": 9,
        }
        return int(order.get(str(member_role), 99))
