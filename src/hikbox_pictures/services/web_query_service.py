from __future__ import annotations

import json
from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.repositories import ExportRepo, OpsEventRepo, PersonRepo, ReviewRepo, ScanRepo, SourceRepo


class WebQueryService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.scan_repo = ScanRepo(conn)
        self.source_repo = SourceRepo(conn)
        self.person_repo = PersonRepo(conn)
        self.review_repo = ReviewRepo(conn)
        self.export_repo = ExportRepo(conn)
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
                           FROM face_observation AS fo
                           WHERE fo.id = p.cover_observation_id
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
            for person in self.list_people()
            if int(person["id"]) in person_ids
        }
        cover_observation_ids = {
            int(person["cover_observation_id"])
            for person in people_by_id.values()
            if person.get("cover_observation_id") is not None
        }
        observation_media = self._load_observation_media(observation_ids | cover_observation_ids)

        grouped: dict[str, list[dict[str, Any]]] = {key: [] for key in queue_order}
        viewer_items: list[dict[str, Any]] = []
        viewer_index_by_observation: dict[int, int] = {}
        focusable_count = 0

        for item in raw_items:
            review_type = str(item["review_type"])
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
                    "review_type": review_type,
                    "priority": int(item["priority"]),
                    "viewer_index": viewer_index,
                    "headline": self._build_review_headline(
                        review_type=review_type,
                        primary_person=primary_person,
                        secondary_person=secondary_person,
                        candidate_names=candidate_names,
                    ),
                    "description": self._build_review_description(
                        review_type=review_type,
                        primary_person=primary_person,
                        secondary_person=secondary_person,
                        candidate_names=candidate_names,
                    ),
                    "chips": self._build_review_chips(
                        primary_person=primary_person,
                        secondary_person=secondary_person,
                        observation_id=evidence_observation_id,
                        candidate_names=candidate_names,
                    ),
                    "preview_faces": preview_faces,
                    "observation_label": (
                        f"样本 #{evidence_observation_id}" if evidence_observation_id is not None else None
                    ),
                }
            )

        active_queue_count = sum(1 for review_type in queue_order if grouped[review_type])
        return {
            "queues": [
                {
                    "review_type": review_type,
                    "title": queue_meta[review_type]["title"],
                    "subtitle": queue_meta[review_type]["subtitle"],
                    "description": queue_meta[review_type]["description"],
                    "count": len(grouped[review_type]),
                    "items": grouped[review_type],
                }
                for review_type in queue_order
            ],
            "viewer_items": viewer_items,
            "summary": {
                "open_count": len(raw_items),
                "active_queue_count": active_queue_count,
                "focusable_count": focusable_count,
            },
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
    ) -> str:
        primary_name = str(primary_person["display_name"]) if primary_person is not None else None
        secondary_name = str(secondary_person["display_name"]) if secondary_person is not None else None
        if review_type == "new_person":
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
    ) -> str:
        primary_name = str(primary_person["display_name"]) if primary_person is not None else None
        secondary_name = str(secondary_person["display_name"]) if secondary_person is not None else None
        if review_type == "new_person":
            if candidate_names:
                return (
                    f"模型认为它和 {' / '.join(candidate_names[:3])} 有相似性，但还不足以自动归属。"
                    "先看 context / original，再决定新建还是驳回。"
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
    ) -> list[str]:
        chips: list[str] = []
        if primary_person is not None:
            chips.append(f"关联人物：{primary_person['display_name']}")
        if secondary_person is not None:
            chips.append(f"对比人物：{secondary_person['display_name']}")
        if observation_id is not None:
            chips.append(f"样本 #{observation_id}")
        if candidate_names:
            chips.append(f"候选：{' / '.join(candidate_names[:3])}")
        return chips

    def list_export_templates(self) -> list[dict[str, Any]]:
        rows = self.export_repo.list_templates()
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
                "enabled": bool(row["enabled"]),
                "latest_run": runs_by_template.get(int(row["id"])),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

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
                   pa.primary_path
            FROM person_face_assignment AS pfa
            JOIN face_observation AS fo
              ON fo.id = pfa.face_observation_id
            JOIN photo_asset AS pa
              ON pa.id = fo.photo_asset_id
            WHERE pfa.person_id = ?
            ORDER BY pfa.id ASC
            """,
            (int(person_id),),
        ).fetchall()
        assignment_rows = [dict(row) for row in assignments]
        viewer_items = [
            {
                "label": f"assignment-{row['id']}",
                "crop_url": f"/api/observations/{row['face_observation_id']}/crop",
                "context_url": f"/api/observations/{row['face_observation_id']}/context",
                "original_url": f"/api/photos/{row['photo_asset_id']}/original",
            }
            for row in assignment_rows
        ]
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

    def list_export_preview_samples(self, limit: int = 6) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 50))
        rows = self.conn.execute(
            """
            SELECT pa.id AS photo_id,
                   MIN(fo.id) AS observation_id
            FROM photo_asset AS pa
            JOIN face_observation AS fo
              ON fo.photo_asset_id = pa.id
             AND fo.active = 1
            GROUP BY pa.id
            ORDER BY pa.id ASC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
        return [
            {
                "label": f"export-photo-{row['photo_id']}",
                "crop_url": f"/api/observations/{row['observation_id']}/crop",
                "context_url": f"/api/observations/{row['observation_id']}/context",
                "original_url": f"/api/photos/{row['photo_id']}/original",
            }
            for row in rows
        ]
