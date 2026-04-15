from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.repositories import ExportRepo, OpsEventRepo, PersonRepo, ReviewRepo, ScanRepo, SourceRepo
from hikbox_pictures.services.export_match_service import ExportMatchService

NEW_PERSON_GROUP_CENTROID_DISTANCE = 0.9
NEW_PERSON_GROUP_MEMBER_DISTANCE = 1.0
QUEUE_PREVIEW_VISIBLE_COUNT = 3


class WebQueryService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.scan_repo = ScanRepo(conn)
        self.source_repo = SourceRepo(conn)
        self.person_repo = PersonRepo(conn)
        self.review_repo = ReviewRepo(conn)
        self.export_repo = ExportRepo(conn)
        self.export_match_service = ExportMatchService(conn)
        self.ops_event_repo = OpsEventRepo(conn)

    def get_scan_status(self) -> dict[str, Any]:
        session = self.scan_repo.latest_session()
        if session is None:
            return {
                "session_id": None,
                "mode": None,
                "status": "idle",
                "created_at": None,
                "started_at": None,
                "stopped_at": None,
                "finished_at": None,
            }
        return {
            "session_id": session["id"],
            "mode": session["mode"],
            "status": session["status"],
            "created_at": session["created_at"],
            "started_at": session["started_at"],
            "stopped_at": session["stopped_at"],
            "finished_at": session["finished_at"],
        }

    def list_people(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT p.id,
                   p.display_name,
                   p.status,
                   p.confirmed,
                   p.ignored,
                   p.notes,
                   p.created_at,
                   p.updated_at,
                   COALESCE(
                       (
                           SELECT fo.id
                           FROM person_face_assignment AS pfa
                           JOIN face_observation AS fo
                             ON fo.id = pfa.face_observation_id
                           WHERE fo.id = p.cover_observation_id
                             AND pfa.person_id = p.id
                             AND pfa.active = 1
                             AND fo.active = 1
                       ),
                       (
                           SELECT pfa.face_observation_id
                           FROM person_face_assignment AS pfa
                           JOIN face_observation AS fo
                             ON fo.id = pfa.face_observation_id
                           WHERE pfa.person_id = p.id
                             AND pfa.active = 1
                             AND fo.active = 1
                           ORDER BY pfa.locked DESC, pfa.id ASC
                           LIMIT 1
                       )
                   ) AS cover_observation_id,
                   (
                       SELECT COUNT(*)
                       FROM person_face_assignment AS pfa
                       JOIN face_observation AS fo
                         ON fo.id = pfa.face_observation_id
                       WHERE pfa.person_id = p.id
                         AND pfa.active = 1
                         AND fo.active = 1
                   ) AS sample_count,
                   (
                       SELECT COUNT(DISTINCT fo.photo_asset_id)
                       FROM person_face_assignment AS pfa
                       JOIN face_observation AS fo
                         ON fo.id = pfa.face_observation_id
                       WHERE pfa.person_id = p.id
                         AND pfa.active = 1
                         AND fo.active = 1
                   ) AS photo_count,
                   (
                       SELECT COUNT(*)
                       FROM review_item AS ri
                       WHERE ri.status = 'open'
                         AND (
                             ri.primary_person_id = p.id
                             OR ri.secondary_person_id = p.id
                         )
                   ) AS pending_review_count
            FROM person AS p
            ORDER BY p.id ASC
            """
        ).fetchall()
        return [
            {
                "id": row["id"],
                "display_name": row["display_name"],
                "status": row["status"],
                "confirmed": bool(row["confirmed"]),
                "ignored": bool(row["ignored"]),
                "notes": row["notes"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "cover_observation_id": int(row["cover_observation_id"]) if row["cover_observation_id"] is not None else None,
                "cover_crop_url": (
                    f"/api/observations/{row['cover_observation_id']}/crop"
                    if row["cover_observation_id"] is not None
                    else None
                ),
                "sample_count": int(row["sample_count"]),
                "photo_count": int(row["photo_count"]),
                "pending_review_count": int(row["pending_review_count"]),
            }
            for row in rows
        ]

    def list_reviews(self) -> list[dict[str, Any]]:
        return self.review_repo.list_open_items()

    def list_review_queues(self) -> list[dict[str, Any]]:
        return self.get_review_page()["queues"]

    def get_review_page(self) -> dict[str, Any]:
        queue_order = [
            "new_person",
            "possible_merge",
            "possible_split",
            "low_confidence_assignment",
        ]
        queue_meta = {
            "new_person": {
                "title": "新人物",
                "subtitle": "需要确认是否建档",
                "description": "新出现的人脸样本，先核对证据，再决定是否进入人物库。",
            },
            "possible_merge": {
                "title": "候选合并",
                "subtitle": "重复人物待确认",
                "description": "对比两侧样本与原图，避免把不同人物误并到同一档案。",
            },
            "possible_split": {
                "title": "候选拆分",
                "subtitle": "人物内部疑似混入",
                "description": "重点检查同一人物卡是否混入不同面孔或跨场景误归属。",
            },
            "low_confidence_assignment": {
                "title": "低置信度归属",
                "subtitle": "模型无法稳妥自动归属",
                "description": "先看 context / original，再决定确认、驳回或暂时忽略。",
            },
        }
        all_people = self.list_people()
        assignable_people = [
            {
                "id": int(person["id"]),
                "display_name": str(person["display_name"]),
                "confirmed": bool(person["confirmed"]),
                "option_label": (
                    str(person["display_name"])
                    if bool(person["confirmed"])
                    else f"{person['display_name']}（未确认）"
                ),
            }
            for person in all_people
            if str(person["status"]) == "active" and not bool(person["ignored"])
        ]
        assignable_people_by_id = {
            int(person["id"]): person
            for person in assignable_people
        }
        raw_items = self.review_repo.list_open_items()
        payloads: dict[int, dict[str, Any]] = {}
        person_ids: set[int] = set()
        observation_ids: set[int] = set()
        for item in raw_items:
            item_id = int(item["id"])
            payload = self._parse_review_payload(item["payload_json"])
            payloads[item_id] = payload
            review_type = str(item["review_type"])
            if review_type not in queue_meta:
                continue
            if item["primary_person_id"] is not None:
                person_ids.add(int(item["primary_person_id"]))
            if item["secondary_person_id"] is not None:
                person_ids.add(int(item["secondary_person_id"]))
            if item["face_observation_id"] is not None:
                observation_ids.add(int(item["face_observation_id"]))
            payload_observation_id = payload.get("face_observation_id")
            if payload_observation_id is not None:
                try:
                    observation_ids.add(int(payload_observation_id))
                except (TypeError, ValueError):
                    pass
            for candidate in payload.get("candidates", []):
                if not isinstance(candidate, dict):
                    continue
                candidate_person_id = candidate.get("person_id")
                if candidate_person_id is None:
                    continue
                try:
                    person_ids.add(int(candidate_person_id))
                except (TypeError, ValueError):
                    continue

        people_by_id = {
            int(person["id"]): person
            for person in all_people
            if int(person["id"]) in person_ids
        }
        cover_observation_ids = {
            int(person["cover_observation_id"])
            for person in people_by_id.values()
            if person.get("cover_observation_id") is not None
        }
        observation_media = self._load_observation_media(observation_ids | cover_observation_ids)
        observation_embeddings = self._load_observation_embeddings(observation_ids)

        grouped: dict[str, list[dict[str, Any]]] = {key: [] for key in queue_order}
        raw_count_by_queue: dict[str, int] = {key: 0 for key in queue_order}
        viewer_items: list[dict[str, Any]] = []
        viewer_index_by_observation: dict[int, int] = {}
        focusable_count = 0
        for item in raw_items:
            review_type = str(item["review_type"])
            if review_type in raw_count_by_queue:
                raw_count_by_queue[review_type] += 1

        new_person_clusters = self._cluster_new_person_items(
            items=[item for item in raw_items if str(item["review_type"]) == "new_person"],
            payloads=payloads,
            observation_embeddings=observation_embeddings,
        )
        for cluster in new_person_clusters:
            review_ids = [int(member["id"]) for member in cluster["items"]]
            anchor_review_id = review_ids[0]
            anchor_item = cluster["items"][0]
            anchor_payload = payloads.get(anchor_review_id, {})
            anchor_primary_person = (
                people_by_id.get(int(anchor_item["primary_person_id"]))
                if len(review_ids) == 1 and anchor_item["primary_person_id"] is not None
                else None
            )
            anchor_secondary_person = (
                people_by_id.get(int(anchor_item["secondary_person_id"]))
                if len(review_ids) == 1 and anchor_item["secondary_person_id"] is not None
                else None
            )
            candidate_person_ids: list[int] = []
            candidate_names: list[str] = []
            for member in cluster["items"]:
                member_id = int(member["id"])
                member_payload = payloads.get(member_id, {})
                for candidate_person_id in self._candidate_person_ids(payload=member_payload):
                    if candidate_person_id not in assignable_people_by_id:
                        continue
                    if candidate_person_id not in candidate_person_ids:
                        candidate_person_ids.append(candidate_person_id)
                for name in self._candidate_person_names(payload=member_payload, people_by_id=people_by_id):
                    if name not in candidate_names:
                        candidate_names.append(name)

            preview_observation_ids = [
                int(observation_id)
                for observation_id in cluster["observation_ids"]
                if int(observation_id) in observation_media
            ]
            evidence_observation_id = preview_observation_ids[0] if preview_observation_ids else None
            if evidence_observation_id is None:
                evidence_observation_id = self._pick_review_evidence_observation(
                    item=anchor_item,
                    payload=anchor_payload,
                    primary_person=anchor_primary_person,
                    secondary_person=anchor_secondary_person,
                    available_observation_ids=set(observation_media),
                )
            preview_faces = (
                self._build_new_person_cluster_previews(
                    observation_ids=preview_observation_ids,
                    observation_media=observation_media,
                )
                if preview_observation_ids
                else self._build_review_previews(
                    review_type="new_person",
                    primary_person=anchor_primary_person,
                    secondary_person=anchor_secondary_person,
                    observation_media=observation_media,
                    observation_id=evidence_observation_id,
                )
            )
            viewer_index = self._ensure_viewer_item(
                item_id=anchor_review_id,
                observation_id=evidence_observation_id,
                observation_media=observation_media,
                viewer_items=viewer_items,
                viewer_index_by_observation=viewer_index_by_observation,
            )
            if viewer_index is not None:
                focusable_count += 1

            for face in preview_faces:
                face_observation_id = face.get("observation_id")
                face["viewer_index"] = self._ensure_viewer_item(
                    item_id=anchor_review_id,
                    observation_id=int(face_observation_id) if face_observation_id is not None else None,
                    observation_media=observation_media,
                    viewer_items=viewer_items,
                    viewer_index_by_observation=viewer_index_by_observation,
                )

            grouped["new_person"].append(
                {
                    "id": anchor_review_id,
                    "review_ids": review_ids,
                    "review_label": self._build_review_label(review_ids),
                    "review_type": "new_person",
                    "priority": max(int(member["priority"]) for member in cluster["items"]),
                    "viewer_index": viewer_index,
                    "headline": self._build_review_headline(
                        review_type="new_person",
                        primary_person=anchor_primary_person,
                        secondary_person=anchor_secondary_person,
                        candidate_names=candidate_names,
                        sample_count=len(review_ids),
                    ),
                    "description": self._build_review_description(
                        review_type="new_person",
                        primary_person=anchor_primary_person,
                        secondary_person=anchor_secondary_person,
                        candidate_names=candidate_names,
                        sample_count=len(review_ids),
                    ),
                    "chips": self._build_review_chips(
                        primary_person=anchor_primary_person,
                        secondary_person=anchor_secondary_person,
                        observation_id=evidence_observation_id,
                        candidate_names=candidate_names,
                        sample_count=len(review_ids),
                    ),
                    "candidate_person_ids": candidate_person_ids,
                    "preview_faces": preview_faces,
                    "preview_total_count": len(review_ids),
                    "preview_visible_count": min(QUEUE_PREVIEW_VISIBLE_COUNT, len(preview_faces)),
                    "preview_summary": self._build_preview_summary(
                        total_count=len(review_ids),
                        visible_count=min(QUEUE_PREVIEW_VISIBLE_COUNT, len(preview_faces)),
                    ),
                    "observation_label": (
                        f"{len(review_ids)} 张样本"
                        if len(review_ids) > 1
                        else f"样本 #{evidence_observation_id}"
                        if evidence_observation_id is not None
                        else None
                    ),
                }
            )

        for item in raw_items:
            review_type = str(item["review_type"])
            if review_type == "new_person":
                continue
            if review_type not in grouped:
                continue
            item_id = int(item["id"])
            payload = payloads.get(item_id, {})
            primary_person = (
                people_by_id.get(int(item["primary_person_id"]))
                if item["primary_person_id"] is not None
                else None
            )
            secondary_person = (
                people_by_id.get(int(item["secondary_person_id"]))
                if item["secondary_person_id"] is not None
                else None
            )
            evidence_observation_id = self._pick_review_evidence_observation(
                item=item,
                payload=payload,
                primary_person=primary_person,
                secondary_person=secondary_person,
                available_observation_ids=set(observation_media),
            )
            viewer_index = self._ensure_viewer_item(
                item_id=item_id,
                observation_id=evidence_observation_id,
                observation_media=observation_media,
                viewer_items=viewer_items,
                viewer_index_by_observation=viewer_index_by_observation,
            )
            if viewer_index is not None:
                focusable_count += 1

            candidate_names = self._candidate_person_names(payload=payload, people_by_id=people_by_id)
            preview_faces = self._build_review_previews(
                review_type=review_type,
                primary_person=primary_person,
                secondary_person=secondary_person,
                observation_media=observation_media,
                observation_id=evidence_observation_id,
            )
            for face in preview_faces:
                face_observation_id = face.get("observation_id")
                face["viewer_index"] = self._ensure_viewer_item(
                    item_id=item_id,
                    observation_id=int(face_observation_id) if face_observation_id is not None else None,
                    observation_media=observation_media,
                    viewer_items=viewer_items,
                    viewer_index_by_observation=viewer_index_by_observation,
                )
            grouped[review_type].append(
                {
                    "id": item_id,
                    "review_ids": [item_id],
                    "review_label": self._build_review_label([item_id]),
                    "review_type": review_type,
                    "priority": int(item["priority"]),
                    "viewer_index": viewer_index,
                    "headline": self._build_review_headline(
                        review_type=review_type,
                        primary_person=primary_person,
                        secondary_person=secondary_person,
                        candidate_names=candidate_names,
                        sample_count=1,
                    ),
                    "description": self._build_review_description(
                        review_type=review_type,
                        primary_person=primary_person,
                        secondary_person=secondary_person,
                        candidate_names=candidate_names,
                        sample_count=1,
                    ),
                    "chips": self._build_review_chips(
                        primary_person=primary_person,
                        secondary_person=secondary_person,
                        observation_id=evidence_observation_id,
                        candidate_names=candidate_names,
                        sample_count=1,
                    ),
                    "preview_faces": preview_faces,
                    "preview_total_count": len(preview_faces),
                    "preview_visible_count": min(QUEUE_PREVIEW_VISIBLE_COUNT, len(preview_faces)),
                    "preview_summary": self._build_preview_summary(
                        total_count=len(preview_faces),
                        visible_count=min(QUEUE_PREVIEW_VISIBLE_COUNT, len(preview_faces)),
                    ),
                    "observation_label": (
                        f"样本 #{evidence_observation_id}" if evidence_observation_id is not None else None
                    ),
                }
            )

        grouped["new_person"].sort(
            key=lambda item: (
                -len(item["review_ids"]),
                -int(item["priority"]),
                int(item["id"]),
            )
        )

        active_queue_count = sum(1 for review_type in queue_order if grouped[review_type])
        return {
            "queues": [
                {
                    "review_type": review_type,
                    "title": queue_meta[review_type]["title"],
                    "subtitle": queue_meta[review_type]["subtitle"],
                    "description": (
                        self._build_new_person_queue_description(
                            sample_count=raw_count_by_queue[review_type],
                            group_count=len(grouped[review_type]),
                        )
                        if review_type == "new_person"
                        else queue_meta[review_type]["description"]
                    ),
                    "count": len(grouped[review_type]),
                    "raw_count": raw_count_by_queue[review_type],
                    "items": grouped[review_type],
                }
                for review_type in queue_order
            ],
            "viewer_items": viewer_items,
            "summary": {
                "open_count": len(raw_items),
                "active_queue_count": active_queue_count,
                "focusable_count": focusable_count,
                "new_person_sample_count": raw_count_by_queue["new_person"],
                "new_person_group_count": len(grouped["new_person"]),
            },
            "assignable_people": assignable_people,
        }

    def _parse_review_payload(self, payload_json: Any) -> dict[str, Any]:
        if payload_json in (None, ""):
            return {}
        try:
            payload = json.loads(str(payload_json))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _load_observation_media(self, observation_ids: set[int]) -> dict[int, dict[str, str]]:
        if not observation_ids:
            return {}
        ordered_ids = sorted(int(observation_id) for observation_id in observation_ids)
        placeholders = ", ".join("?" for _ in ordered_ids)
        rows = self.conn.execute(
            f"""
            SELECT fo.id AS observation_id,
                   fo.photo_asset_id AS photo_id
            FROM face_observation AS fo
            JOIN photo_asset AS pa
              ON pa.id = fo.photo_asset_id
            WHERE fo.active = 1
              AND fo.id IN ({placeholders})
            """,
            tuple(ordered_ids),
        ).fetchall()
        return {
            int(row["observation_id"]): {
                "crop_url": f"/api/observations/{row['observation_id']}/crop",
                "context_url": f"/api/observations/{row['observation_id']}/context",
                "original_url": f"/api/photos/{row['photo_id']}/original",
            }
            for row in rows
        }

    def _load_observation_embeddings(self, observation_ids: set[int]) -> dict[int, np.ndarray]:
        if not observation_ids:
            return {}
        ordered_ids = sorted(int(observation_id) for observation_id in observation_ids)
        placeholders = ", ".join("?" for _ in ordered_ids)
        rows = self.conn.execute(
            f"""
            SELECT id,
                   face_observation_id AS observation_id,
                   dimension,
                   vector_blob
            FROM face_embedding
            WHERE feature_type = 'face'
              AND face_observation_id IN ({placeholders})
            ORDER BY face_observation_id ASC, id DESC
            """,
            tuple(ordered_ids),
        ).fetchall()
        embeddings: dict[int, np.ndarray] = {}
        for row in rows:
            observation_id = int(row["observation_id"])
            if observation_id in embeddings:
                continue
            vector_blob = row["vector_blob"]
            if not isinstance(vector_blob, (bytes, bytearray, memoryview)):
                continue
            vector = np.frombuffer(vector_blob, dtype=np.float32, count=int(row["dimension"])).copy()
            if vector.ndim != 1 or vector.size == 0:
                continue
            embeddings[observation_id] = vector
        return embeddings

    def _cluster_new_person_items(
        self,
        *,
        items: list[dict[str, Any]],
        payloads: dict[int, dict[str, Any]],
        observation_embeddings: dict[int, np.ndarray],
    ) -> list[dict[str, Any]]:
        ordered_items = sorted(items, key=lambda item: (-int(item["priority"]), int(item["id"])))
        clusters: list[dict[str, Any]] = []
        for item in ordered_items:
            item_id = int(item["id"])
            payload = payloads.get(item_id, {})
            observation_id = self._extract_review_observation_id(item=item, payload=payload)
            embedding = observation_embeddings.get(observation_id) if observation_id is not None else None
            best_cluster_index: int | None = None
            best_score: tuple[float, float, int] | None = None
            if embedding is not None:
                for index, cluster in enumerate(clusters):
                    centroid = cluster["centroid"]
                    if centroid is None:
                        continue
                    centroid_distance = float(np.linalg.norm(embedding - centroid))
                    if centroid_distance > NEW_PERSON_GROUP_CENTROID_DISTANCE:
                        continue
                    member_distance = min(
                        float(np.linalg.norm(embedding - member_embedding))
                        for member_embedding in cluster["embeddings"]
                    )
                    if member_distance > NEW_PERSON_GROUP_MEMBER_DISTANCE:
                        continue
                    score = (centroid_distance, member_distance, int(cluster["items"][0]["id"]))
                    if best_score is None or score < best_score:
                        best_score = score
                        best_cluster_index = index

            if best_cluster_index is None:
                clusters.append(
                    {
                        "items": [item],
                        "observation_ids": [observation_id] if observation_id is not None else [],
                        "embeddings": [embedding] if embedding is not None else [],
                        "centroid": embedding.copy() if embedding is not None else None,
                    }
                )
                continue

            cluster = clusters[best_cluster_index]
            cluster["items"].append(item)
            if observation_id is not None and observation_id not in cluster["observation_ids"]:
                cluster["observation_ids"].append(observation_id)
            if embedding is not None:
                cluster["embeddings"].append(embedding)
                cluster["centroid"] = self._build_cluster_centroid(cluster["embeddings"])
        return clusters

    def _build_cluster_centroid(self, embeddings: list[np.ndarray]) -> np.ndarray | None:
        if not embeddings:
            return None
        centroid = np.mean(np.vstack(embeddings), axis=0).astype(np.float32, copy=False)
        norm = float(np.linalg.norm(centroid))
        if norm > 0.0:
            centroid = centroid / norm
        return centroid.astype(np.float32, copy=False)

    def _build_new_person_cluster_previews(
        self,
        *,
        observation_ids: list[int],
        observation_media: dict[int, dict[str, str]],
    ) -> list[dict[str, Any]]:
        previews: list[dict[str, Any]] = []
        for observation_id in observation_ids:
            media = observation_media.get(int(observation_id))
            previews.append(
                {
                    "label": f"样本 #{observation_id}",
                    "crop_url": media["crop_url"] if media is not None else None,
                    "observation_id": int(observation_id),
                }
            )
        return previews

    def _build_preview_summary(self, *, total_count: int, visible_count: int) -> str | None:
        clean_total = max(0, int(total_count))
        clean_visible = max(0, int(visible_count))
        if clean_total <= 1:
            return None
        return f"预览 {clean_visible} / {clean_total}"

    def _build_new_person_queue_description(self, *, sample_count: int, group_count: int) -> str:
        if sample_count <= 0:
            return "新出现的人脸样本，先核对证据，再决定是否进入人物库。"
        if group_count < sample_count:
            return f"{sample_count} 张新脸样本已自动归成 {group_count} 组候选，先核对是否真是同一人，再决定是否统一建档。"
        return f"当前共有 {sample_count} 张新脸样本，暂未发现可直接并看的候选组，建议逐张核对证据。"

    def _extract_review_observation_id(self, *, item: dict[str, Any], payload: dict[str, Any]) -> int | None:
        if item["face_observation_id"] is not None:
            return int(item["face_observation_id"])
        payload_observation_id = payload.get("face_observation_id")
        if payload_observation_id is None:
            return None
        try:
            return int(payload_observation_id)
        except (TypeError, ValueError):
            return None

    def _pick_review_evidence_observation(
        self,
        *,
        item: dict[str, Any],
        payload: dict[str, Any],
        primary_person: dict[str, Any] | None,
        secondary_person: dict[str, Any] | None,
        available_observation_ids: set[int],
    ) -> int | None:
        candidates: list[int] = []
        if item["face_observation_id"] is not None:
            candidates.append(int(item["face_observation_id"]))
        payload_observation_id = payload.get("face_observation_id")
        if payload_observation_id is not None:
            try:
                candidates.append(int(payload_observation_id))
            except (TypeError, ValueError):
                pass
        for person in (primary_person, secondary_person):
            if person is None or person.get("cover_observation_id") is None:
                continue
            candidates.append(int(person["cover_observation_id"]))
        for observation_id in candidates:
            if observation_id > 0 and observation_id in available_observation_ids:
                return observation_id
        return None

    def _candidate_person_names(
        self,
        *,
        payload: dict[str, Any],
        people_by_id: dict[int, dict[str, Any]],
    ) -> list[str]:
        names: list[str] = []
        for candidate in payload.get("candidates", []):
            if not isinstance(candidate, dict):
                continue
            person_id = candidate.get("person_id")
            if person_id is None:
                continue
            try:
                clean_person_id = int(person_id)
            except (TypeError, ValueError):
                continue
            person = people_by_id.get(clean_person_id)
            label = str(person["display_name"]) if person is not None else f"人物#{clean_person_id}"
            if label not in names:
                names.append(label)
        return names

    def _candidate_person_ids(self, *, payload: dict[str, Any]) -> list[int]:
        person_ids: list[int] = []
        for candidate in payload.get("candidates", []):
            if not isinstance(candidate, dict):
                continue
            person_id = candidate.get("person_id")
            if person_id is None:
                continue
            try:
                clean_person_id = int(person_id)
            except (TypeError, ValueError):
                continue
            if clean_person_id <= 0 or clean_person_id in person_ids:
                continue
            person_ids.append(clean_person_id)
        return person_ids

    def _build_review_previews(
        self,
        *,
        review_type: str,
        primary_person: dict[str, Any] | None,
        secondary_person: dict[str, Any] | None,
        observation_media: dict[int, dict[str, str]],
        observation_id: int | None,
    ) -> list[dict[str, Any]]:
        previews: list[dict[str, Any]] = []
        if review_type == "possible_merge":
            for person in (primary_person, secondary_person):
                preview = self._build_person_preview(person=person, observation_media=observation_media)
                if preview is not None:
                    previews.append(preview)
        else:
            preview = self._build_person_preview(person=primary_person, observation_media=observation_media)
            if preview is None:
                preview = self._build_person_preview(person=secondary_person, observation_media=observation_media)
            if preview is not None:
                previews.append(preview)
        if not previews and observation_id is not None:
            media = observation_media.get(int(observation_id))
            previews.append(
                {
                    "label": "待审核样本",
                    "crop_url": media["crop_url"] if media is not None else None,
                    "observation_id": int(observation_id),
                }
            )
        return previews[:2]

    def _build_person_preview(
        self,
        *,
        person: dict[str, Any] | None,
        observation_media: dict[int, dict[str, str]],
    ) -> dict[str, Any] | None:
        if person is None:
            return None
        crop_url: str | None = None
        if person.get("cover_observation_id") is not None:
            media = observation_media.get(int(person["cover_observation_id"]))
            if media is not None:
                crop_url = media["crop_url"]
        return {
            "label": str(person["display_name"]),
            "crop_url": crop_url,
            "observation_id": int(person["cover_observation_id"]) if person.get("cover_observation_id") is not None else None,
        }

    def _ensure_viewer_item(
        self,
        *,
        item_id: int,
        observation_id: int | None,
        observation_media: dict[int, dict[str, str]],
        viewer_items: list[dict[str, Any]],
        viewer_index_by_observation: dict[int, int],
    ) -> int | None:
        if observation_id is None:
            return None
        clean_observation_id = int(observation_id)
        if clean_observation_id in viewer_index_by_observation:
            return viewer_index_by_observation[clean_observation_id]
        media = observation_media.get(clean_observation_id)
        if media is None:
            return None
        viewer_index = len(viewer_items)
        viewer_items.append(
            {
                "label": f"review #{item_id} · 样本 #{clean_observation_id}",
                "crop_url": media["crop_url"],
                "context_url": media["context_url"],
                "original_url": media["original_url"],
            }
        )
        viewer_index_by_observation[clean_observation_id] = viewer_index
        return viewer_index

    def _build_review_headline(
        self,
        *,
        review_type: str,
        primary_person: dict[str, Any] | None,
        secondary_person: dict[str, Any] | None,
        candidate_names: list[str],
        sample_count: int,
    ) -> str:
        primary_name = str(primary_person["display_name"]) if primary_person is not None else None
        secondary_name = str(secondary_person["display_name"]) if secondary_person is not None else None
        if review_type == "new_person":
            if sample_count > 1:
                if candidate_names:
                    return f"候选新人物组含 {sample_count} 张相似样本，模型曾召回到 {' / '.join(candidate_names[:2])}"
                return f"候选新人物组含 {sample_count} 张相似样本，建议整组核对后再建档"
            if candidate_names:
                return f"疑似新人物，模型只召回到 {' / '.join(candidate_names[:2])}"
            return "疑似新人物，需要确认是否单独建档"
        if review_type == "possible_merge":
            if primary_name and secondary_name:
                return f"“{primary_name}” 与 “{secondary_name}” 可能是同一人"
            return "两个人物可能重复，需要人工确认是否合并"
        if review_type == "possible_split":
            if primary_name:
                return f"“{primary_name}” 的人物卡里可能混入了不同的人"
            return "当前人物卡可能需要拆分出新的归属"
        if review_type == "low_confidence_assignment":
            if primary_name:
                return f"“{primary_name}” 的归属置信度不足"
            if candidate_names:
                return f"归属候选在 {' / '.join(candidate_names[:2])} 之间摇摆"
            return "当前归属置信度不足，需要人工确认"
        return f"review #{review_type}"

    def _build_review_description(
        self,
        *,
        review_type: str,
        primary_person: dict[str, Any] | None,
        secondary_person: dict[str, Any] | None,
        candidate_names: list[str],
        sample_count: int,
    ) -> str:
        primary_name = str(primary_person["display_name"]) if primary_person is not None else None
        secondary_name = str(secondary_person["display_name"]) if secondary_person is not None else None
        if review_type == "new_person":
            if sample_count > 1:
                if candidate_names:
                    return (
                        f"这 {sample_count} 张样本已按 embedding 相似度自动归组，模型也曾召回到 {' / '.join(candidate_names[:3])}。"
                        "先逐张核对 crop / context，确认是否可作为同一候选人物统一处理。"
                    )
                return (
                    f"这 {sample_count} 张样本已按 embedding 相似度自动归组。"
                    "先逐张核对 crop / context，确认是否确实属于同一人，再决定是否统一建档。"
                )
            if candidate_names:
                return (
                    f"模型认为它和 {' / '.join(candidate_names[:3])} 有相似性，但还不足以自动归属。"
                    "先看 context / original，再决定新建、归入已有还是忽略。"
                )
            return "这条样本暂无可靠候选。建议先核对 crop 与 context，避免把边缘样本直接建成新人物。"
        if review_type == "possible_merge":
            if primary_name and secondary_name:
                return f"先对比 “{primary_name}” 和 “{secondary_name}” 的封面，再切到原图核对拍摄环境与时间线。"
            return "先比对封面，再切到 context / original 核对背景、姿态与时间线。"
        if review_type == "possible_split":
            if primary_name:
                return f"重点检查 “{primary_name}” 里是否混入不同面孔，必要时拆分出新的独立人物。"
            return "重点检查同一人物卡下是否混入不同面孔或跨场景误归属。"
        if review_type == "low_confidence_assignment":
            if candidate_names:
                return (
                    f"模型在 {' / '.join(candidate_names[:3])} 之间无法稳定决策。"
                    "先看 context / original，再决定确认、驳回或暂时忽略。"
                )
            return "建议先看 context / original，确认场景与面孔细节后再执行动作。"
        return "请先核对证据，再执行动作。"

    def _build_review_chips(
        self,
        *,
        primary_person: dict[str, Any] | None,
        secondary_person: dict[str, Any] | None,
        observation_id: int | None,
        candidate_names: list[str],
        sample_count: int,
    ) -> list[str]:
        chips: list[str] = []
        if sample_count > 1:
            chips.append(f"候选组：{sample_count} 张样本")
        if primary_person is not None:
            chips.append(f"关联人物：{primary_person['display_name']}")
        if secondary_person is not None:
            chips.append(f"对比人物：{secondary_person['display_name']}")
        if observation_id is not None:
            chips.append(f"样本 #{observation_id}")
        if candidate_names:
            chips.append(f"候选：{' / '.join(candidate_names[:3])}")
        return chips

    def _build_review_label(self, review_ids: list[int]) -> str:
        if not review_ids:
            return "review"
        if len(review_ids) == 1:
            return f"review #{review_ids[0]}"
        return f"review #{review_ids[0]} 等 {len(review_ids)} 条"

    def _list_export_template_rows(self) -> list[dict[str, Any]]:
        rows = self.export_repo.list_templates()
        people_by_id = {
            int(person["id"]): person
            for person in self.list_people()
        }
        runs_by_template: dict[int, dict[str, Any]] = {}
        for row in rows:
            template_id = int(row["id"])
            runs = self.export_repo.list_runs_by_template(template_id, limit=1)
            if runs:
                runs_by_template[template_id] = runs[0]
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "output_root": row["output_root"],
                "include_group": bool(row["include_group"]),
                "export_live_mov": bool(row["export_live_mov"]),
                "start_datetime": row["start_datetime"],
                "end_datetime": row["end_datetime"],
                "enabled": bool(row["enabled"]),
                "person_ids": person_ids,
                "selected_people": [
                    {
                        "id": person_id,
                        "display_name": people_by_id.get(int(person_id), {}).get(
                            "display_name",
                            f"人物 #{person_id}",
                        ),
                    }
                    for person_id in person_ids
                ],
                "selected_people_label": " / ".join(
                    str(
                        people_by_id.get(int(person_id), {}).get(
                            "display_name",
                            f"人物 #{person_id}",
                        )
                    )
                    for person_id in person_ids
                ),
                "latest_run": runs_by_template.get(int(row["id"])),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
            for person_ids in [self.export_repo.list_template_person_ids(int(row["id"]))]
        ]

    def list_export_templates(self) -> list[dict[str, Any]]:
        return self._list_export_template_rows()

    def list_events(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.ops_event_repo.list_recent(limit=limit)

    def get_person_detail(self, person_id: int) -> dict[str, Any] | None:
        person = self.person_repo.get_person(int(person_id))
        if person is None:
            return None
        assignments = self.conn.execute(
            """
            SELECT pfa.id,
                   pfa.assignment_source,
                   pfa.confidence,
                   pfa.locked,
                   pfa.active,
                   pfa.created_at,
                   pfa.updated_at,
                   fo.id AS face_observation_id,
                   pa.id AS photo_asset_id,
                   pa.primary_path,
                   pa.live_mov_path
            FROM person_face_assignment AS pfa
            JOIN face_observation AS fo
              ON fo.id = pfa.face_observation_id
            JOIN photo_asset AS pa
              ON pa.id = fo.photo_asset_id
            WHERE pfa.person_id = ?
              AND pfa.active = 1
              AND fo.active = 1
            ORDER BY pfa.id ASC
            """,
            (int(person_id),),
        ).fetchall()
        assignment_rows = [dict(row) for row in assignments]
        viewer_items: list[dict[str, Any]] = []
        for index, row in enumerate(assignment_rows):
            observation_id = int(row["face_observation_id"])
            photo_id = int(row["photo_asset_id"])
            crop_url = f"/api/observations/{observation_id}/crop"
            context_url = f"/api/observations/{observation_id}/context"
            original_url = f"/api/photos/{photo_id}/original"
            file_name = Path(str(row["primary_path"])).name if row.get("primary_path") else f"图片#{photo_id}"
            is_live_photo = bool(row.get("live_mov_path"))
            viewer_label = f"{file_name} · 样本 #{observation_id} · 图片 #{photo_id}"
            if is_live_photo:
                viewer_label += " · live"

            row["preview_url"] = original_url
            row["crop_url"] = crop_url
            row["context_url"] = context_url
            row["original_url"] = original_url
            row["viewer_index"] = index
            row["viewer_label"] = viewer_label
            row["file_name"] = file_name
            row["is_live_photo"] = is_live_photo
            row["live_label"] = "live" if is_live_photo else None
            viewer_items.append(
                {
                    "label": viewer_label,
                    "crop_url": crop_url,
                    "context_url": context_url,
                    "original_url": original_url,
                    "assignment_id": int(row["id"]),
                    "observation_id": observation_id,
                }
            )
        return {
            "person": {
                "id": person["id"],
                "display_name": person["display_name"],
                "status": person["status"],
                "confirmed": bool(person["confirmed"]),
                "ignored": bool(person["ignored"]),
                "notes": person["notes"],
                "created_at": person["created_at"],
                "updated_at": person["updated_at"],
            },
            "assignments": assignment_rows,
            "viewer_items": viewer_items,
        }

    def get_sources_scan_view(self) -> dict[str, Any]:
        session = self.scan_repo.latest_session()
        session_sources: list[dict[str, Any]] = []
        if session is not None:
            session_sources = self.scan_repo.list_session_sources(int(session["id"]))
        return {
            "session": session,
            "session_sources": session_sources,
            "sources": self.source_repo.list_sources(active=True),
        }

    def get_export_page(self, *, preview_limit_per_template: int | None = None) -> dict[str, Any]:
        templates = self._list_export_template_rows()
        all_people = self.list_people()
        people_by_id = {
            int(person["id"]): person
            for person in all_people
        }
        available_people = [
            {
                "id": int(person["id"]),
                "display_name": str(person["display_name"]),
                "confirmed": bool(person["confirmed"]),
                "status": str(person["status"]),
                "ignored": bool(person["ignored"]),
                "badge": "已确认" if bool(person["confirmed"]) else "未确认",
            }
            for person in all_people
            if str(person["status"]) == "active" and not bool(person["ignored"])
        ]
        safe_limit: int | None = None
        if preview_limit_per_template is not None:
            safe_limit = max(1, min(int(preview_limit_per_template), 200))
        viewer_items: list[dict[str, Any]] = []

        enriched_templates: list[dict[str, Any]] = []
        for template in templates:
            enriched = dict(template)
            enriched.update(
                self._build_export_template_preview(
                    template=enriched,
                    sample_limit=safe_limit,
                    people_by_id=people_by_id,
                    viewer_items=viewer_items,
                )
            )
            enriched_templates.append(enriched)

        return {
            "templates": enriched_templates,
            "available_people": available_people,
            "viewer_items": viewer_items,
        }

    def list_viewer_samples(self, limit: int = 6) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 50))
        rows = self.conn.execute(
            """
            SELECT fo.id AS observation_id,
                   fo.photo_asset_id AS photo_id
            FROM face_observation AS fo
            JOIN photo_asset AS pa
              ON pa.id = fo.photo_asset_id
            WHERE fo.active = 1
            ORDER BY fo.id ASC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
        return [
            {
                "label": f"observation-{row['observation_id']}",
                "crop_url": f"/api/observations/{row['observation_id']}/crop",
                "context_url": f"/api/observations/{row['observation_id']}/context",
                "original_url": f"/api/photos/{row['photo_id']}/original",
            }
            for row in rows
        ]

    def _build_export_template_preview(
        self,
        *,
        template: dict[str, Any],
        sample_limit: int | None,
        people_by_id: dict[int, dict[str, Any]],
        viewer_items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        template_id = int(template["id"])
        required_person_ids = self.export_repo.list_template_person_ids(template_id)
        if not required_person_ids:
            return {
                "matched_only_count": 0,
                "matched_group_count": 0,
                "preview_samples": [],
                "preview_match_count": 0,
                "preview_error": "模板尚未配置人物，暂时无法生成预览。",
            }

        try:
            plan = self.export_match_service.build_template_plan(template_id)
        except (LookupError, ValueError) as exc:
            return {
                "matched_only_count": 0,
                "matched_group_count": 0,
                "preview_samples": [],
                "preview_match_count": 0,
                "preview_error": str(exc),
            }

        matches = list(plan["matches"])
        preview_evidence_by_photo = self._resolve_export_preview_evidence_by_photo(
            photo_asset_ids={int(match.photo_asset_id) for match in matches},
            required_person_ids=[int(person_id) for person_id in required_person_ids],
            start_datetime=plan["template"].get("start_datetime"),
            end_datetime=plan["template"].get("end_datetime"),
        )

        preview_matches = matches if sample_limit is None else matches[:sample_limit]
        preview_samples: list[dict[str, Any]] = []
        for match in preview_matches:
            sample_detail = preview_evidence_by_photo.get(
                int(match.photo_asset_id),
                {
                    "representative_observation_id": None,
                    "people": [],
                },
            )
            bucket_label = self._format_export_bucket(match.bucket.value)
            is_live_photo = match.live_mov_path is not None
            viewer_badge_label = f"{bucket_label} · live" if is_live_photo else bucket_label
            preview_people: list[dict[str, Any]] = []
            for person_preview in sample_detail["people"]:
                person_id = int(person_preview["person_id"])
                observation_id = int(person_preview["observation_id"])
                display_name = str(
                    people_by_id.get(person_id, {}).get(
                        "display_name",
                        f"人物 #{person_id}",
                    )
                )
                preview_people.append(
                    {
                        "person_id": person_id,
                        "display_name": display_name,
                        "observation_id": observation_id,
                        "crop_url": f"/api/observations/{observation_id}/crop",
                        "context_url": f"/api/observations/{observation_id}/context",
                    }
                )
            observation_id = sample_detail["representative_observation_id"]
            if observation_id is None and preview_people:
                observation_id = int(preview_people[0]["observation_id"])
            viewer_index = self._append_export_viewer_item(
                template_name=str(template["name"]),
                photo_id=int(match.photo_asset_id),
                bucket_label=viewer_badge_label,
                representative_observation_id=observation_id,
                preview_people=preview_people,
                viewer_items=viewer_items,
            )
            preview_samples.append(
                {
                    "photo_asset_id": int(match.photo_asset_id),
                    "bucket_label": bucket_label,
                    "is_live_photo": is_live_photo,
                    "live_label": "live" if is_live_photo else None,
                    "preview_url": f"/api/photos/{match.photo_asset_id}/preview",
                    "viewer_label": f"{template['name']} · 照片 #{match.photo_asset_id} · {viewer_badge_label}",
                    "viewer_index": viewer_index,
                    "preview_people": preview_people,
                }
            )

        return {
            "matched_only_count": int(plan["matched_only_count"]),
            "matched_group_count": int(plan["matched_group_count"]),
            "preview_samples": preview_samples,
            "preview_match_count": len(matches),
            "preview_error": None,
        }

    def _resolve_export_preview_evidence_by_photo(
        self,
        *,
        photo_asset_ids: set[int],
        required_person_ids: list[int],
        start_datetime: str | None,
        end_datetime: str | None,
    ) -> dict[int, dict[str, Any]]:
        if not photo_asset_ids:
            return {}

        required_person_id_set = {int(person_id) for person_id in required_person_ids}
        grouped: dict[int, dict[int, dict[str, Any]]] = {}
        for row in self.export_repo.list_assets_with_faces(
            start_datetime=start_datetime,
            end_datetime=end_datetime,
        ):
            photo_id = int(row["photo_asset_id"])
            if photo_id not in photo_asset_ids:
                continue
            observation_id = row["face_observation_id"]
            if observation_id is None:
                continue

            observation = grouped.setdefault(photo_id, {}).setdefault(
                int(observation_id),
                {
                    "face_area_ratio": None,
                    "person_ids": set(),
                },
            )
            face_area_ratio = row["face_area_ratio"]
            if face_area_ratio is not None:
                observation["face_area_ratio"] = float(face_area_ratio)
            person_id = row["person_id"]
            if person_id is not None:
                observation["person_ids"].add(int(person_id))

        preview_by_photo: dict[int, dict[str, Any]] = {}
        for photo_id, observations in grouped.items():
            matched_candidates: list[tuple[float, int, int]] = []
            fallback_candidates: list[tuple[float, int, int]] = []
            for observation_id, observation in observations.items():
                area = float(observation["face_area_ratio"]) if observation["face_area_ratio"] is not None else -1.0
                candidate = (area, -int(observation_id), int(observation_id))
                fallback_candidates.append(candidate)
                person_ids = set(int(person_id) for person_id in observation["person_ids"])
                if person_ids and person_ids.issubset(required_person_id_set):
                    matched_candidates.append(candidate)

            preview_people: list[dict[str, int]] = []
            for required_person_id in required_person_ids:
                person_candidates: list[tuple[float, int, int]] = []
                for observation_id, observation in observations.items():
                    person_ids = set(int(person_id) for person_id in observation["person_ids"])
                    if int(required_person_id) not in person_ids:
                        continue
                    area = float(observation["face_area_ratio"]) if observation["face_area_ratio"] is not None else -1.0
                    person_candidates.append((area, -int(observation_id), int(observation_id)))
                selected_person_observation = max(person_candidates, default=None)
                if selected_person_observation is None:
                    continue
                preview_people.append(
                    {
                        "person_id": int(required_person_id),
                        "observation_id": int(selected_person_observation[2]),
                    }
                )

            selected = max(matched_candidates or fallback_candidates, default=None)
            preview_by_photo[int(photo_id)] = {
                "representative_observation_id": int(selected[2]) if selected is not None else None,
                "people": preview_people,
            }

        return preview_by_photo

    def _append_export_viewer_item(
        self,
        *,
        template_name: str,
        photo_id: int,
        bucket_label: str,
        representative_observation_id: int | None,
        preview_people: list[dict[str, Any]],
        viewer_items: list[dict[str, Any]],
    ) -> int:
        viewer_index = len(viewer_items)
        crop_url = ""
        context_url = ""
        if preview_people:
            crop_url = str(preview_people[0]["crop_url"])
            context_url = str(preview_people[0]["context_url"])
        elif representative_observation_id is not None:
            crop_url = f"/api/observations/{int(representative_observation_id)}/crop"
            context_url = f"/api/observations/{int(representative_observation_id)}/context"
        viewer_items.append(
            {
                "label": f"{template_name} · 照片 #{int(photo_id)} · {bucket_label}",
                "crop_url": crop_url,
                "context_url": context_url,
                "original_url": f"/api/photos/{int(photo_id)}/original",
                "evidence_people": preview_people,
                "observation_id": int(representative_observation_id) if representative_observation_id is not None else None,
            }
        )
        return viewer_index

    @staticmethod
    def _format_export_bucket(bucket: str) -> str:
        if str(bucket) == "only":
            return "only"
        return "group"

    def list_export_preview_samples(self, limit: int = 6) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 50))
        return self.get_export_page(preview_limit_per_template=safe_limit)["viewer_items"][:safe_limit]
