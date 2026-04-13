from __future__ import annotations

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
        session = self.scan_repo.latest_resumable_session()
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
        rows = self.person_repo.list_people()
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
            }
            for row in rows
        ]

    def list_reviews(self) -> list[dict[str, Any]]:
        return self.review_repo.list_open_items()

    def list_review_queues(self) -> list[dict[str, Any]]:
        queue_order = [
            "new_person",
            "possible_merge",
            "possible_split",
            "low_confidence_assignment",
        ]
        queue_titles = {
            "new_person": "新人物",
            "possible_merge": "候选合并",
            "possible_split": "候选拆分",
            "low_confidence_assignment": "低置信度归属",
        }
        grouped: dict[str, list[dict[str, Any]]] = {key: [] for key in queue_order}
        for item in self.review_repo.list_open_items():
            review_type = str(item["review_type"])
            if review_type in grouped:
                grouped[review_type].append(item)
        return [
            {
                "review_type": review_type,
                "title": queue_titles[review_type],
                "items": grouped[review_type],
            }
            for review_type in queue_order
        ]

    def list_export_templates(self) -> list[dict[str, Any]]:
        rows = self.export_repo.list_templates()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "output_root": row["output_root"],
                "include_group": bool(row["include_group"]),
                "export_live_mov": bool(row["export_live_mov"]),
                "enabled": bool(row["enabled"]),
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
            "assignments": [dict(row) for row in assignments],
        }

    def get_sources_scan_view(self) -> dict[str, Any]:
        session = self.scan_repo.latest_resumable_session()
        session_sources: list[dict[str, Any]] = []
        if session is not None:
            session_sources = self.scan_repo.list_session_sources(int(session["id"]))
        return {
            "session": session,
            "session_sources": session_sources,
            "sources": self.source_repo.list_sources(active=True),
        }
