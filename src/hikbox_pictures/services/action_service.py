from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
from typing import Any, Callable, Sequence

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.repositories import PersonRepo
from hikbox_pictures.repositories import ExportRepo
from hikbox_pictures.repositories import ReviewRepo
from hikbox_pictures.services.export_delivery_service import ExportDeliveryService
from hikbox_pictures.services.export_match_service import ExportMatchService
from hikbox_pictures.services.person_truth_service import PersonTruthService


class ActionService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.person_repo = PersonRepo(conn)
        self.review_repo = ReviewRepo(conn)
        self.export_repo = ExportRepo(conn)
        self.person_truth_service = PersonTruthService(conn)
        self.export_match_service = ExportMatchService(conn)
        self.export_delivery_service = ExportDeliveryService(conn)

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

    def dismiss_review(self, review_id: int, *, review_ids: Sequence[int] | None = None) -> dict[str, Any]:
        return self._apply_review_action(
            review_id,
            review_ids=review_ids,
            target_status="dismissed",
            verb="dismiss",
            updater=self.review_repo.dismiss_item,
        )

    def resolve_review(self, review_id: int, *, review_ids: Sequence[int] | None = None) -> dict[str, Any]:
        return self._apply_review_action(
            review_id,
            review_ids=review_ids,
            target_status="resolved",
            verb="resolve",
            updater=self.review_repo.resolve_item,
        )

    def ignore_review(self, review_id: int, *, review_ids: Sequence[int] | None = None) -> dict[str, Any]:
        return self._apply_review_action(
            review_id,
            review_ids=review_ids,
            target_status="dismissed",
            verb="ignore",
            updater=self.review_repo.ignore_item,
        )

    def create_person_from_review(
        self,
        review_id: int,
        *,
        review_ids: Sequence[int] | None = None,
        display_name: str,
    ) -> dict[str, Any]:
        clean_name = display_name.strip()
        if not clean_name:
            raise ValueError("display_name 不能为空")

        ordered_review_ids, observation_ids = self._load_new_person_review_batch(
            review_id,
            review_ids=review_ids,
        )

        try:
            person_id = self.person_repo.create_person(
                clean_name,
                status="active",
                confirmed=True,
                ignored=False,
            )
            assigned_observation_count = self._assign_observations_to_person(
                person_id=int(person_id),
                observation_ids=observation_ids,
            )
            updated_count = self._resolve_review_batch(ordered_review_ids)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        latest = self.review_repo.get_item(ordered_review_ids[0])
        if latest is None:
            raise LookupError(f"review {ordered_review_ids[0]} 不存在")
        return {
            "id": latest["id"],
            "status": latest["status"],
            "resolved_at": latest["resolved_at"],
            "review_ids": ordered_review_ids,
            "updated_count": updated_count,
            "person_id": int(person_id),
            "display_name": clean_name,
            "assigned_observation_count": assigned_observation_count,
        }

    def assign_review_to_existing_person(
        self,
        review_id: int,
        *,
        review_ids: Sequence[int] | None = None,
        person_id: int,
    ) -> dict[str, Any]:
        target_person = self.person_repo.get_person(int(person_id))
        if target_person is None:
            raise LookupError(f"person {person_id} 不存在")
        if str(target_person["status"]) != "active" or bool(target_person["ignored"]):
            raise ValueError("目标人物必须是 active 且未忽略")

        ordered_review_ids, observation_ids = self._load_new_person_review_batch(
            review_id,
            review_ids=review_ids,
        )

        try:
            assigned_observation_count = self._assign_observations_to_person(
                person_id=int(person_id),
                observation_ids=observation_ids,
            )
            updated_count = self._resolve_review_batch(ordered_review_ids)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        latest = self.review_repo.get_item(ordered_review_ids[0])
        if latest is None:
            raise LookupError(f"review {ordered_review_ids[0]} 不存在")
        return {
            "id": latest["id"],
            "status": latest["status"],
            "resolved_at": latest["resolved_at"],
            "review_ids": ordered_review_ids,
            "updated_count": updated_count,
            "person_id": int(target_person["id"]),
            "display_name": str(target_person["display_name"]),
            "assigned_observation_count": assigned_observation_count,
        }

    def preview_export_template(self, template_id: int) -> dict[str, Any]:
        return asdict(self.export_match_service.preview_template(int(template_id)))

    def create_export_template(
        self,
        *,
        name: str,
        output_root: str,
        person_ids: Sequence[int],
        include_group: bool = True,
        export_live_mov: bool = False,
        start_datetime: str | None = None,
        end_datetime: str | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        payload = self._validate_export_template_payload(
            name=name,
            output_root=output_root,
            person_ids=person_ids,
            include_group=include_group,
            export_live_mov=export_live_mov,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            enabled=enabled,
        )

        try:
            template_id = self.export_repo.create_template(
                name=str(payload["name"]),
                output_root=str(payload["output_root"]),
                include_group=bool(payload["include_group"]),
                export_live_mov=bool(payload["export_live_mov"]),
                start_datetime=payload["start_datetime"],
                end_datetime=payload["end_datetime"],
                enabled=bool(payload["enabled"]),
            )
            self.export_repo.replace_template_people(
                template_id=int(template_id),
                person_ids=list(payload["person_ids"]),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        row = self.export_repo.get_template(int(template_id))
        if row is None:
            raise LookupError(f"export template {template_id} 不存在")
        return self._serialize_export_template(row=row)

    def update_export_template(
        self,
        template_id: int,
        *,
        name: str,
        output_root: str,
        person_ids: Sequence[int],
        include_group: bool = True,
        export_live_mov: bool = False,
        start_datetime: str | None = None,
        end_datetime: str | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        if self.export_repo.get_template(int(template_id)) is None:
            raise LookupError(f"export template {template_id} 不存在")

        payload = self._validate_export_template_payload(
            name=name,
            output_root=output_root,
            person_ids=person_ids,
            include_group=include_group,
            export_live_mov=export_live_mov,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            enabled=enabled,
        )

        try:
            updated = self.export_repo.update_template(
                int(template_id),
                name=str(payload["name"]),
                output_root=str(payload["output_root"]),
                include_group=bool(payload["include_group"]),
                export_live_mov=bool(payload["export_live_mov"]),
                start_datetime=payload["start_datetime"],
                end_datetime=payload["end_datetime"],
                enabled=bool(payload["enabled"]),
            )
            if updated == 0:
                self.conn.rollback()
                raise LookupError(f"export template {template_id} 不存在")
            self.export_repo.replace_template_people(
                template_id=int(template_id),
                person_ids=list(payload["person_ids"]),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        row = self.export_repo.get_template(int(template_id))
        if row is None:
            raise LookupError(f"export template {template_id} 不存在")
        return self._serialize_export_template(row=row)

    def delete_export_template(self, template_id: int) -> dict[str, Any]:
        row = self.export_repo.get_template(int(template_id))
        if row is None:
            raise LookupError(f"export template {template_id} 不存在")
        if self.export_repo.list_runs_by_template(int(template_id), limit=1):
            raise ValueError("已有导出历史的模板不能删除，请改为停用或修改配置")

        try:
            deleted = self.export_repo.delete_template(int(template_id))
            if deleted == 0:
                self.conn.rollback()
                raise LookupError(f"export template {template_id} 不存在")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

        return {
            "id": int(template_id),
            "status": "deleted",
        }

    def run_export_template(self, template_id: int) -> dict[str, Any]:
        return asdict(self.export_delivery_service.run_template(int(template_id)))

    def list_export_template_runs(self, template_id: int) -> list[dict[str, Any]]:
        if self.export_repo.get_template(int(template_id)) is None:
            raise LookupError(f"export template {template_id} 不存在")
        rows = self.export_repo.list_runs_by_template(int(template_id))
        return [dict(row) for row in rows]

    def _validate_export_template_payload(
        self,
        *,
        name: str,
        output_root: str,
        person_ids: Sequence[int],
        include_group: bool,
        export_live_mov: bool,
        start_datetime: str | None,
        end_datetime: str | None,
        enabled: bool,
    ) -> dict[str, Any]:
        clean_name = str(name).strip()
        if not clean_name:
            raise ValueError("name 不能为空")

        clean_output_root = str(output_root).strip()
        if not clean_output_root:
            raise ValueError("output_root 不能为空")

        normalized_person_ids = self._normalize_person_ids(person_ids)
        active_person_ids = set(self.person_repo.list_active_person_ids())
        invalid_person_ids = [person_id for person_id in normalized_person_ids if person_id not in active_person_ids]
        if invalid_person_ids:
            joined = ", ".join(str(person_id) for person_id in invalid_person_ids)
            raise ValueError(f"person_ids 包含不存在或不可导出的人物: {joined}")

        clean_start_datetime = self._normalize_optional_iso_datetime(
            start_datetime,
            field_name="start_datetime",
        )
        clean_end_datetime = self._normalize_optional_iso_datetime(
            end_datetime,
            field_name="end_datetime",
        )
        if clean_start_datetime is not None and clean_end_datetime is not None:
            start_moment = datetime.fromisoformat(clean_start_datetime)
            end_moment = datetime.fromisoformat(clean_end_datetime)
            if (start_moment.tzinfo is None) == (end_moment.tzinfo is None) and start_moment > end_moment:
                raise ValueError("start_datetime 不能晚于 end_datetime")

        return {
            "name": clean_name,
            "output_root": clean_output_root,
            "person_ids": normalized_person_ids,
            "include_group": bool(include_group),
            "export_live_mov": bool(export_live_mov),
            "start_datetime": clean_start_datetime,
            "end_datetime": clean_end_datetime,
            "enabled": bool(enabled),
        }

    @staticmethod
    def _normalize_person_ids(person_ids: Sequence[int]) -> list[int]:
        ordered: list[int] = []
        seen: set[int] = set()
        for raw_value in person_ids:
            try:
                clean_value = int(raw_value)
            except (TypeError, ValueError) as exc:
                raise ValueError("person_ids 必须为整数列表") from exc
            if clean_value <= 0:
                raise ValueError("person_ids 必须为正整数")
            if clean_value in seen:
                continue
            seen.add(clean_value)
            ordered.append(clean_value)
        if not ordered:
            raise ValueError("person_ids 至少选择一人")
        return ordered

    @staticmethod
    def _normalize_optional_iso_datetime(value: str | None, *, field_name: str) -> str | None:
        if value is None:
            return None
        clean_value = str(value).strip()
        if not clean_value:
            return None
        try:
            return datetime.fromisoformat(clean_value).isoformat(timespec="seconds")
        except ValueError as exc:
            raise ValueError(f"{field_name} 必须是合法 ISO 时间") from exc

    def _serialize_export_template(self, *, row: dict[str, Any]) -> dict[str, Any]:
        template_id = int(row["id"])
        return {
            "id": template_id,
            "name": row["name"],
            "output_root": row["output_root"],
            "include_group": bool(row["include_group"]),
            "export_live_mov": bool(row["export_live_mov"]),
            "start_datetime": row["start_datetime"],
            "end_datetime": row["end_datetime"],
            "enabled": bool(row["enabled"]),
            "person_ids": self.export_repo.list_template_person_ids(template_id),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _apply_review_action(
        self,
        review_id: int,
        *,
        review_ids: Sequence[int] | None,
        target_status: str,
        verb: str,
        updater: Callable[[int], int],
    ) -> dict[str, Any]:
        ordered_review_ids = self._normalize_review_ids(review_id, review_ids)
        current_rows = []
        for current_id in ordered_review_ids:
            row = self.review_repo.get_item(current_id)
            if row is None:
                raise LookupError(f"review {current_id} 不存在")
            current_rows.append(row)

        updated_count = 0
        if not all(
            row["status"] == target_status and row["resolved_at"] is not None
            for row in current_rows
        ):
            try:
                for row in current_rows:
                    if row["status"] == target_status and row["resolved_at"] is not None:
                        continue
                    updated = updater(int(row["id"]))
                    if updated == 0:
                        self.conn.rollback()
                        raise RuntimeError(f"review {row['id']} {verb} 失败")
                    updated_count += 1
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

        latest = self.review_repo.get_item(ordered_review_ids[0])
        if latest is None:
            raise LookupError(f"review {ordered_review_ids[0]} 不存在")
        return {
            "id": latest["id"],
            "status": latest["status"],
            "resolved_at": latest["resolved_at"],
            "review_ids": ordered_review_ids,
            "updated_count": updated_count,
        }

    @staticmethod
    def _normalize_review_ids(review_id: int, review_ids: Sequence[int] | None) -> list[int]:
        ordered: list[int] = []
        seen: set[int] = set()
        for raw_value in [int(review_id), *(int(value) for value in review_ids or [])]:
            clean_value = int(raw_value)
            if clean_value <= 0 or clean_value in seen:
                continue
            ordered.append(clean_value)
            seen.add(clean_value)
        if not ordered:
            raise ValueError("review_ids 不能为空")
        return ordered

    def _load_new_person_review_batch(
        self,
        review_id: int,
        *,
        review_ids: Sequence[int] | None,
    ) -> tuple[list[int], list[int]]:
        ordered_review_ids = self._normalize_review_ids(review_id, review_ids)
        observation_ids: list[int] = []
        seen_observation_ids: set[int] = set()
        for current_id in ordered_review_ids:
            row = self.review_repo.get_item(current_id)
            if row is None:
                raise LookupError(f"review {current_id} 不存在")
            if str(row["review_type"]) != "new_person":
                raise ValueError("只有新人物 review 支持建档或归入现有人物")
            if str(row["status"]) != "open":
                raise ValueError(f"review {current_id} 已处理，不能重复执行建档或归入")
            payload = self._parse_review_payload(row.get("payload_json"))
            observation_id = self._extract_review_observation_id(row=row, payload=payload)
            if observation_id is None:
                raise ValueError(f"review {current_id} 缺少 face_observation_id，无法处理")
            if observation_id in seen_observation_ids:
                continue
            seen_observation_ids.add(observation_id)
            observation_ids.append(observation_id)
        if not observation_ids:
            raise ValueError("没有可处理的人脸样本")
        return ordered_review_ids, observation_ids

    def _assign_observations_to_person(self, *, person_id: int, observation_ids: Sequence[int]) -> int:
        assigned_count = 0
        for observation_id in observation_ids:
            self._assign_single_observation(
                person_id=int(person_id),
                observation_id=int(observation_id),
            )
            assigned_count += 1
        return assigned_count

    def _assign_single_observation(self, *, person_id: int, observation_id: int) -> None:
        active_assignment = self.person_truth_service.asset_repo.get_active_assignment_for_observation(int(observation_id))
        if active_assignment is None:
            self.person_truth_service.asset_repo.create_assignment(
                person_id=int(person_id),
                face_observation_id=int(observation_id),
                assignment_source="manual",
                confidence=None,
                locked=True,
            )
            return

        assignment_id = int(active_assignment["id"])
        current_person_id = int(active_assignment["person_id"])
        is_locked = int(active_assignment["locked"]) == 1
        if current_person_id == int(person_id):
            if is_locked:
                return
            locked = self.person_truth_service.asset_repo.lock_assignment(assignment_id, person_id=int(person_id))
            if locked == 0:
                raise RuntimeError(f"assignment {assignment_id} 锁定失败")
            return

        if is_locked:
            raise ValueError(f"样本 {observation_id} 已锁定到其他人物，不能直接改归属")

        updated = self.person_truth_service.asset_repo.update_assignment(
            assignment_id,
            person_id=int(person_id),
            assignment_source="manual",
            confidence=None,
        )
        if updated == 0:
            raise RuntimeError(f"assignment {assignment_id} 更新失败")

        locked = self.person_truth_service.asset_repo.lock_assignment(
            assignment_id,
            person_id=int(person_id),
        )
        if locked == 0:
            raise RuntimeError(f"assignment {assignment_id} 锁定失败")

    def _resolve_review_batch(self, review_ids: Sequence[int]) -> int:
        updated_count = 0
        for current_id in review_ids:
            row = self.review_repo.get_item(int(current_id))
            if row is None:
                raise LookupError(f"review {current_id} 不存在")
            if str(row["status"]) == "resolved" and row["resolved_at"] is not None:
                continue
            updated = self.review_repo.resolve_item(int(current_id))
            if updated == 0:
                raise RuntimeError(f"review {current_id} resolve 失败")
            updated_count += 1
        return updated_count

    @staticmethod
    def _parse_review_payload(payload_json: Any) -> dict[str, Any]:
        if payload_json in (None, ""):
            return {}
        try:
            payload = json.loads(str(payload_json))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _extract_review_observation_id(*, row: dict[str, Any], payload: dict[str, Any]) -> int | None:
        face_observation_id = row.get("face_observation_id")
        if face_observation_id is not None:
            return int(face_observation_id)
        payload_observation_id = payload.get("face_observation_id")
        if payload_observation_id is None:
            return None
        try:
            return int(payload_observation_id)
        except (TypeError, ValueError):
            return None
