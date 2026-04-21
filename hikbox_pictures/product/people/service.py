from __future__ import annotations

from datetime import UTC, datetime

from .repository import (
    ExcludeAssignmentResult,
    ExcludeAssignmentsResult,
    MergeOperationResult,
    PersonView,
    SQLitePeopleRepository,
    UndoMergeResult,
)


class PeopleServiceError(RuntimeError):
    """人物维护服务基础异常。"""


class MergeOperationNotFoundError(PeopleServiceError):
    """不存在可撤销的最近合并操作。"""


class PeopleService:
    def __init__(self, repo: SQLitePeopleRepository) -> None:
        self._repo = repo

    def rename_person(self, *, person_id: int, display_name: str) -> PersonView:
        clean_name = display_name.strip()
        if not clean_name:
            raise ValueError("display_name 不能为空")
        return self._repo.rename_person(person_id=person_id, display_name=clean_name, now=_utc_now())

    def exclude_assignment(self, *, person_id: int, face_observation_id: int) -> ExcludeAssignmentResult:
        return self._repo.exclude_assignment(
            person_id=person_id,
            face_observation_id=face_observation_id,
            now=_utc_now(),
        )

    def exclude_assignments(self, *, person_id: int, face_observation_ids: list[int]) -> ExcludeAssignmentsResult:
        if not face_observation_ids:
            raise ValueError("face_observation_ids 不能为空")
        return self._repo.exclude_assignments(
            person_id=person_id,
            face_observation_ids=face_observation_ids,
            now=_utc_now(),
        )

    def merge_people(self, *, selected_person_ids: list[int]) -> MergeOperationResult:
        normalized: list[int] = []
        seen: set[int] = set()
        for person_id in selected_person_ids:
            if person_id in seen:
                continue
            seen.add(person_id)
            normalized.append(person_id)
        if len(normalized) < 2:
            raise ValueError("selected_person_ids 至少需要 2 个不同人物")
        return self._repo.merge_people(selected_person_ids=normalized, now=_utc_now())

    def undo_last_merge(self) -> UndoMergeResult:
        try:
            return self._repo.undo_last_merge(now=_utc_now())
        except LookupError as exc:
            raise MergeOperationNotFoundError("没有可撤销的最近 merge 操作") from exc


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
