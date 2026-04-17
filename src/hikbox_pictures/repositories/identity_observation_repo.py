from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from typing import Any

import numpy as np

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]


@dataclass(frozen=True)
class _ObservationRow:
    observation_id: int
    photo_asset_id: int
    capture_datetime: str | None
    quality_score: float
    vector: np.ndarray


class IdentityObservationRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get_observation_profile(self, profile_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM identity_observation_profile
            WHERE id = ?
            """,
            (int(profile_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_observation_profile_required(self, profile_id: int) -> dict[str, Any]:
        profile = self.get_observation_profile(profile_id)
        if profile is None:
            raise ValueError(f"observation profile 不存在: {int(profile_id)}")
        return profile

    def get_active_profile(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM identity_observation_profile
            WHERE active = 1
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        return dict(row) if row is not None else None

    def compute_observation_dataset_hash(self, *, model_key: str) -> str:
        digest = sha256()
        rows = self.conn.execute(
            """
            SELECT fo.id,
                   COALESCE(fo.quality_score, 0.0) AS quality_score,
                   fe.vector_blob
            FROM face_observation AS fo
            JOIN face_embedding AS fe
              ON fe.face_observation_id = fo.id
             AND fe.feature_type = 'face'
             AND fe.model_key = ?
             AND fe.normalized = 1
            WHERE fo.active = 1
            ORDER BY fo.id ASC
            """,
            (str(model_key),),
        ).fetchall()
        for row in rows:
            digest.update(str(int(row["id"])).encode("utf-8"))
            digest.update(b"|")
            digest.update(f"{float(row['quality_score']):.8f}".encode("utf-8"))
            digest.update(b"|")
            vector_blob = row["vector_blob"]
            if not isinstance(vector_blob, (bytes, bytearray, memoryview)):
                continue
            digest.update(bytes(vector_blob))
            digest.update(b"\n")
        return digest.hexdigest()

    def compute_candidate_policy_hash(
        self,
        *,
        profile_id: int,
        candidate_knn_limit: int,
    ) -> str:
        profile = self.get_observation_profile_required(profile_id)
        payload = {
            "profile_id": int(profile_id),
            "core_quality_threshold": float(profile["core_quality_threshold"]),
            "attachment_quality_threshold": float(profile["attachment_quality_threshold"]),
            "exact_duplicate_distance_threshold": float(profile["exact_duplicate_distance_threshold"]),
            "same_photo_keep_best": str(profile["same_photo_keep_best"]),
            "burst_window_seconds": int(profile["burst_window_seconds"]),
            "burst_duplicate_distance_threshold": float(profile["burst_duplicate_distance_threshold"]),
            "pool_exclusion_rules_version": str(profile["pool_exclusion_rules_version"]),
        }
        return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    def find_reusable_snapshot(
        self,
        *,
        observation_profile_id: int,
        dataset_hash: str,
        candidate_policy_hash: str,
        required_knn_limit: int,
        algorithm_version: str,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT id, summary_json, max_knn_supported
            FROM identity_observation_snapshot
            WHERE observation_profile_id = ?
              AND dataset_hash = ?
              AND candidate_policy_hash = ?
              AND status = 'succeeded'
              AND algorithm_version = ?
              AND max_knn_supported >= ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                int(observation_profile_id),
                str(dataset_hash),
                str(candidate_policy_hash),
                str(algorithm_version),
                int(required_knn_limit),
            ),
        ).fetchone()
        if row is None:
            return None
        summary = self._load_json_object(row["summary_json"])
        pool_counts_raw = summary.get("pool_counts") if isinstance(summary, dict) else {}
        pool_counts = {
            "core_discovery": int((pool_counts_raw or {}).get("core_discovery", 0)),
            "attachment": int((pool_counts_raw or {}).get("attachment", 0)),
            "excluded": int((pool_counts_raw or {}).get("excluded", 0)),
        }
        return {
            "id": int(row["id"]),
            "max_knn_supported": int(row["max_knn_supported"]),
            "pool_counts": pool_counts,
        }

    def create_snapshot(
        self,
        *,
        observation_profile_id: int,
        dataset_hash: str,
        candidate_policy_hash: str,
        max_knn_supported: int,
        algorithm_version: str,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO identity_observation_snapshot(
                observation_profile_id,
                dataset_hash,
                candidate_policy_hash,
                max_knn_supported,
                algorithm_version,
                summary_json,
                status,
                started_at
            )
            VALUES (?, ?, ?, ?, ?, '{}', 'running', CURRENT_TIMESTAMP)
            """,
            (
                int(observation_profile_id),
                str(dataset_hash),
                str(candidate_policy_hash),
                int(max_knn_supported),
                str(algorithm_version),
            ),
        )
        return int(cursor.lastrowid)

    def mark_snapshot_failed(
        self,
        *,
        snapshot_id: int,
        reason: str,
    ) -> None:
        self.conn.execute(
            """
            UPDATE identity_observation_snapshot
            SET status = 'failed',
                summary_json = ?,
                finished_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                json.dumps(
                    {"error": str(reason)},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                int(snapshot_id),
            ),
        )

    def populate_snapshot_entries(
        self,
        *,
        snapshot_id: int,
        observation_profile_id: int,
    ) -> dict[str, int]:
        profile = self.get_observation_profile_required(observation_profile_id)
        rows = self._list_rows_for_pooling(model_key=str(profile["embedding_model_key"]))

        core_threshold = float(profile["core_quality_threshold"])
        attachment_threshold = float(profile["attachment_quality_threshold"])
        exact_threshold = float(profile["exact_duplicate_distance_threshold"])
        burst_threshold = float(profile["burst_duplicate_distance_threshold"])
        burst_window_seconds = int(profile["burst_window_seconds"])

        candidates: list[dict[str, Any]] = []
        for row in rows:
            pool_kind = "excluded"
            excluded_reason = "low_quality"
            if row.quality_score >= core_threshold:
                pool_kind = "core_discovery"
                excluded_reason = None
            elif row.quality_score >= attachment_threshold:
                pool_kind = "attachment"
                excluded_reason = None
            candidates.append(
                {
                    "row": row,
                    "pool_kind": pool_kind,
                    "excluded_reason": excluded_reason,
                }
            )

        same_photo_sorted = sorted(
            [item for item in candidates if item["excluded_reason"] is None],
            key=lambda item: (-float(item["row"].quality_score), int(item["row"].observation_id)),
        )
        photo_representative: dict[int, _ObservationRow] = {}
        dedup_excluded: list[dict[str, Any]] = [item for item in candidates if item["excluded_reason"] is not None]
        after_same_photo: list[dict[str, Any]] = []
        for item in same_photo_sorted:
            row = item["row"]
            representative = photo_representative.get(row.photo_asset_id)
            if representative is None:
                photo_representative[row.photo_asset_id] = row
                after_same_photo.append(item)
                continue
            dedup_group_key = f"photo:{row.photo_asset_id}"
            dedup_excluded.append(
                {
                    "row": row,
                    "pool_kind": "excluded",
                    "excluded_reason": "same_photo_duplicate",
                    "representative_observation_id": int(representative.observation_id),
                    "dedup_group_key": dedup_group_key,
                    "diagnostic_json": {
                        "dedup_group_key": dedup_group_key,
                        "policy": "same_photo_keep_best",
                    },
                }
            )

        kept_rows: list[dict[str, Any]] = []
        for item in sorted(after_same_photo, key=lambda value: int(value["row"].observation_id)):
            row = item["row"]
            matched, reason = self._find_duplicate(
                candidate=row,
                representatives=[entry["row"] for entry in kept_rows],
                exact_threshold=exact_threshold,
                burst_threshold=burst_threshold,
                burst_window_seconds=burst_window_seconds,
            )
            if matched is None or reason is None:
                kept_rows.append(item)
                continue
            dedup_group_key = f"rep:{int(matched.observation_id)}"
            dedup_excluded.append(
                {
                    "row": row,
                    "pool_kind": "excluded",
                    "excluded_reason": reason,
                    "representative_observation_id": int(matched.observation_id),
                    "dedup_group_key": dedup_group_key,
                    "diagnostic_json": {
                        "dedup_group_key": dedup_group_key,
                        "distance": float(self._cosine_distance(row.vector, matched.vector)),
                    },
                }
            )

        pool_counts = {
            "core_discovery": 0,
            "attachment": 0,
            "excluded": 0,
        }
        for item in kept_rows:
            row = item["row"]
            pool_kind = str(item["pool_kind"])
            pool_counts[pool_kind] += 1
            self._insert_pool_entry(
                snapshot_id=snapshot_id,
                observation_id=int(row.observation_id),
                pool_kind=pool_kind,
                quality_score_snapshot=float(row.quality_score),
                dedup_group_key=None,
                representative_observation_id=None,
                excluded_reason=None,
                diagnostic_json={
                    "dedup_group_key": f"self:{int(row.observation_id)}",
                },
            )

        for item in sorted(dedup_excluded, key=lambda value: int(value["row"].observation_id)):
            row = item["row"]
            pool_counts["excluded"] += 1
            self._insert_pool_entry(
                snapshot_id=snapshot_id,
                observation_id=int(row.observation_id),
                pool_kind="excluded",
                quality_score_snapshot=float(row.quality_score),
                dedup_group_key=item.get("dedup_group_key"),
                representative_observation_id=item.get("representative_observation_id"),
                excluded_reason=str(item["excluded_reason"]),
                diagnostic_json=item.get("diagnostic_json") or {},
            )

        self.conn.execute(
            """
            UPDATE identity_observation_snapshot
            SET summary_json = ?,
                status = 'succeeded',
                finished_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                json.dumps(
                    {
                        "pool_counts": pool_counts,
                        "observation_count": len(rows),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                int(snapshot_id),
            ),
        )
        return pool_counts

    def _insert_pool_entry(
        self,
        *,
        snapshot_id: int,
        observation_id: int,
        pool_kind: str,
        quality_score_snapshot: float,
        dedup_group_key: str | None,
        representative_observation_id: int | None,
        excluded_reason: str | None,
        diagnostic_json: dict[str, Any],
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO identity_observation_pool_entry(
                snapshot_id,
                observation_id,
                pool_kind,
                quality_score_snapshot,
                dedup_group_key,
                representative_observation_id,
                excluded_reason,
                diagnostic_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(snapshot_id),
                int(observation_id),
                str(pool_kind),
                float(quality_score_snapshot),
                dedup_group_key,
                int(representative_observation_id) if representative_observation_id is not None else None,
                excluded_reason,
                json.dumps(diagnostic_json, ensure_ascii=False, sort_keys=True),
            ),
        )

    def _list_rows_for_pooling(self, *, model_key: str) -> list[_ObservationRow]:
        rows = self.conn.execute(
            """
            SELECT fo.id AS observation_id,
                   fo.photo_asset_id,
                   COALESCE(fo.quality_score, 0.0) AS quality_score,
                   pa.capture_datetime,
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
            ORDER BY fo.id ASC
            """,
            (str(model_key),),
        ).fetchall()

        parsed: list[_ObservationRow] = []
        for row in rows:
            vector_blob = row["vector_blob"]
            if not isinstance(vector_blob, (bytes, bytearray, memoryview)):
                continue
            vector = np.frombuffer(vector_blob, dtype=np.float32).copy()
            parsed.append(
                _ObservationRow(
                    observation_id=int(row["observation_id"]),
                    photo_asset_id=int(row["photo_asset_id"]),
                    capture_datetime=str(row["capture_datetime"]) if row["capture_datetime"] else None,
                    quality_score=float(row["quality_score"] or 0.0),
                    vector=vector,
                )
            )
        return parsed

    def _find_duplicate(
        self,
        *,
        candidate: _ObservationRow,
        representatives: list[_ObservationRow],
        exact_threshold: float,
        burst_threshold: float,
        burst_window_seconds: int,
    ) -> tuple[_ObservationRow | None, str | None]:
        nearest: _ObservationRow | None = None
        nearest_distance = float("inf")
        for representative in representatives:
            distance = float(self._cosine_distance(candidate.vector, representative.vector))
            if distance < nearest_distance:
                nearest_distance = distance
                nearest = representative
        if nearest is None:
            return None, None
        if nearest_distance <= exact_threshold:
            return nearest, "duplicate_shadow"

        candidate_ts = self._to_unix_seconds(candidate.capture_datetime)
        nearest_ts = self._to_unix_seconds(nearest.capture_datetime)
        within_window = candidate_ts is not None and nearest_ts is not None and abs(candidate_ts - nearest_ts) <= float(
            burst_window_seconds
        )
        if within_window and nearest_distance <= burst_threshold:
            return nearest, "duplicate_burst"
        return None, None

    def _to_unix_seconds(self, value: str | None) -> float | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None

    def _cosine_distance(self, a: np.ndarray, b: np.ndarray) -> float:
        denom = float(np.linalg.norm(a) * np.linalg.norm(b))
        if denom <= 0.0:
            return 1.0
        score = float(np.dot(a, b) / denom)
        return float(max(0.0, 1.0 - score))

    def _load_json_object(self, raw: object) -> dict[str, Any]:
        if isinstance(raw, str) and raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return {}
            if isinstance(payload, dict):
                return payload
        return {}
