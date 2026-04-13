from __future__ import annotations

from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.repositories import PersonRepo
from hikbox_pictures.services.person_truth_service import PersonTruthService
from hikbox_pictures.services.review_workflow_service import ReviewWorkflowService


class ActionService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.person_repo = PersonRepo(conn)
        self.person_truth_service = PersonTruthService(conn)
        self.review_workflow_service = ReviewWorkflowService(conn)

    def rename_person(self, person_id: int, display_name: str) -> dict[str, Any]:
        clean_name = display_name.strip()
        if not clean_name:
            raise ValueError("display_name 不能为空")

        cursor = self.conn.execute(
            "UPDATE person SET display_name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (clean_name, int(person_id)),
        )
        if cursor.rowcount == 0:
            self.conn.rollback()
            raise LookupError(f"person {person_id} 不存在")

        self.conn.commit()
        row = self.person_repo.get_person(int(person_id))
        if row is None:
            raise LookupError(f"person {person_id} 不存在")
        return {
            "id": row["id"],
            "display_name": row["display_name"],
            "status": row["status"],
            "confirmed": bool(row["confirmed"]),
            "ignored": bool(row["ignored"]),
            "notes": row["notes"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def merge_person(self, source_person_id: int, target_person_id: int) -> dict[str, Any]:
        row = self.person_truth_service.merge_people(
            source_person_id=int(source_person_id),
            target_person_id=int(target_person_id),
        )
        return {
            "id": row["id"],
            "display_name": row["display_name"],
            "status": row["status"],
            "merged_into_person_id": row["merged_into_person_id"],
        }

    def split_person_assignment(self, person_id: int, assignment_id: int, new_person_display_name: str) -> dict[str, int]:
        return self.person_truth_service.split_assignment(
            person_id=int(person_id),
            assignment_id=int(assignment_id),
            new_person_display_name=new_person_display_name,
        )

    def lock_person_assignment(self, person_id: int, assignment_id: int) -> dict[str, Any]:
        row = self.person_truth_service.lock_assignment(
            person_id=int(person_id),
            assignment_id=int(assignment_id),
        )
        return {
            "id": row["id"],
            "person_id": row["person_id"],
            "locked": bool(row["locked"]),
            "assignment_source": row["assignment_source"],
        }

    def dismiss_review(self, review_id: int) -> dict[str, Any]:
        row = self.review_workflow_service.dismiss(int(review_id))
        return {
            "id": row["id"],
            "status": row["status"],
            "resolved_at": row["resolved_at"],
        }
