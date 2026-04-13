from __future__ import annotations

from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.repositories import AssetRepo, PersonRepo


class PersonTruthService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.person_repo = PersonRepo(conn)
        self.asset_repo = AssetRepo(conn)

    def merge_people(self, source_person_id: int, target_person_id: int) -> dict[str, Any]:
        if int(source_person_id) == int(target_person_id):
            raise ValueError("source_person_id 与 target_person_id 不能相同")

        source = self.person_repo.get_person(int(source_person_id))
        if source is None:
            raise LookupError(f"person {source_person_id} 不存在")
        if source["status"] != "active":
            raise ValueError("源人物必须是 active 状态，不能重复 merge")
        target = self.person_repo.get_person(int(target_person_id))
        if target is None:
            raise LookupError(f"person {target_person_id} 不存在")
        if target["status"] != "active":
            raise ValueError("目标人物必须是 active 状态")

        try:
            updated = self.person_repo.mark_merged(int(source_person_id), int(target_person_id))
            if updated == 0:
                self.conn.rollback()
                latest_source = self.person_repo.get_person(int(source_person_id))
                if latest_source is None:
                    raise LookupError(f"person {source_person_id} 不存在")
                raise ValueError("源人物必须是 active 状态，不能重复 merge")

            self.asset_repo.move_active_assignments_for_person(
                from_person_id=int(source_person_id),
                to_person_id=int(target_person_id),
                assignment_source="merge",
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        latest = self.person_repo.get_person(int(source_person_id))
        if latest is None:
            raise LookupError(f"person {source_person_id} 不存在")
        return latest

    def split_assignment(self, person_id: int, assignment_id: int, new_person_display_name: str) -> dict[str, int]:
        clean_name = new_person_display_name.strip()
        if not clean_name:
            raise ValueError("new_person_display_name 不能为空")

        person = self.person_repo.get_person(int(person_id))
        if person is None:
            raise LookupError(f"person {person_id} 不存在")

        assignment = self.asset_repo.get_assignment(int(assignment_id))
        if assignment is None:
            raise LookupError(f"assignment {assignment_id} 不存在")
        if int(assignment["active"]) != 1:
            raise ValueError(f"assignment {assignment_id} 不是 active 状态")
        if int(assignment["person_id"]) != int(person_id):
            raise ValueError(f"assignment {assignment_id} 不属于 person {person_id}")

        try:
            new_person_id = self.person_repo.create_person(
                display_name=clean_name,
                status="active",
                confirmed=False,
                ignored=False,
            )
            moved = self.asset_repo.move_assignment(
                int(assignment_id),
                from_person_id=int(person_id),
                to_person_id=int(new_person_id),
                assignment_source="split",
            )
            if moved == 0:
                self.conn.rollback()
                raise LookupError(f"assignment {assignment_id} 不存在")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        return {
            "assignment_id": int(assignment_id),
            "person_id": int(person_id),
            "new_person_id": int(new_person_id),
        }

    def lock_assignment(self, person_id: int, assignment_id: int) -> dict[str, Any]:
        person = self.person_repo.get_person(int(person_id))
        if person is None:
            raise LookupError(f"person {person_id} 不存在")

        assignment = self.asset_repo.get_assignment(int(assignment_id))
        if assignment is None:
            raise LookupError(f"assignment {assignment_id} 不存在")
        if int(assignment["person_id"]) != int(person_id):
            raise ValueError(f"assignment {assignment_id} 不属于 person {person_id}")
        if int(assignment["active"]) != 1:
            raise ValueError(f"assignment {assignment_id} 不是 active 状态")

        try:
            updated = self.asset_repo.lock_assignment(int(assignment_id), person_id=int(person_id))
            if updated == 0:
                self.conn.rollback()
            else:
                self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        latest_assignment = self.asset_repo.get_assignment(int(assignment_id))
        if latest_assignment is None:
            raise LookupError(f"assignment {assignment_id} 不存在")
        if int(latest_assignment["person_id"]) != int(person_id):
            raise ValueError(f"assignment {assignment_id} 不属于 person {person_id}")
        if int(latest_assignment["active"]) != 1:
            raise ValueError(f"assignment {assignment_id} 不是 active 状态")
        return latest_assignment

    def try_auto_reassign(self, assignment_id: int, candidate_person_id: int) -> bool:
        assignment = self.asset_repo.get_assignment(int(assignment_id))
        if assignment is None:
            return False
        if int(assignment["locked"]) == 1:
            return False

        candidate = self.person_repo.get_person(int(candidate_person_id))
        if candidate is None or candidate["status"] != "active":
            return False

        changed = self.asset_repo.reassign_if_unlocked(
            int(assignment_id),
            candidate_person_id=int(candidate_person_id),
        )
        if changed > 0:
            self.conn.commit()
            return True
        self.conn.rollback()
        return False
