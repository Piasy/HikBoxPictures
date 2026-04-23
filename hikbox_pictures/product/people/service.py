"""人物维护服务。"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from hikbox_pictures.product.export.run_service import assert_people_writes_unlocked
from hikbox_pictures.product.people.repository import (
    AssignmentRecord,
    ClusterSnapshotRecord,
    DeltaRecord,
    ExclusionRecord,
    PeopleRepository,
    PersonRecord,
)


class PeopleError(Exception):
    """人物维护域基础异常。"""


class PeopleNotFoundError(PeopleError):
    """人物不存在。"""


class PeopleMergeError(PeopleError):
    """人物合并失败。"""


class PeopleUndoMergeError(PeopleError):
    """撤销合并失败。"""


class PeopleUndoMergeConflictError(PeopleUndoMergeError):
    """撤销合并时发现 merge 之后已发生状态漂移。"""


class PeopleExcludeConflictError(PeopleError):
    """排除输入与当前归属不一致。"""


@dataclass(frozen=True)
class ExcludeFacesResult:
    person_id: int
    face_observation_ids: list[int]


@dataclass(frozen=True)
class MergePeopleResult:
    merge_operation_id: int
    winner_person_id: int
    loser_person_ids: list[int]


@dataclass(frozen=True)
class MergeUndoResult:
    merge_operation_id: int


class PeopleService:
    """人物重命名、排除、合并与撤销服务。"""

    def __init__(self, repo: PeopleRepository):
        self._repo = repo

    def rename_person(self, person_id: int, display_name: str) -> PersonRecord:
        normalized_name = display_name.strip()
        if not normalized_name:
            raise ValueError("display_name 不能为空")
        last_locked_error: sqlite3.OperationalError | None = None
        for attempt in range(20):
            conn = self._repo.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                person = self._repo.rename_person(person_id=int(person_id), display_name=normalized_name, conn=conn)
                if person is None:
                    raise PeopleNotFoundError(f"人物不存在或不可重命名，id={person_id}")
                conn.commit()
                return person
            except sqlite3.OperationalError as exc:
                _rollback_quietly(conn)
                if _is_database_locked_error(exc) and attempt < 19:
                    last_locked_error = exc
                    time.sleep(0.05)
                    continue
                raise
            except Exception:
                _rollback_quietly(conn)
                raise
            finally:
                conn.close()
        assert last_locked_error is not None
        raise last_locked_error

    def exclude_face(self, person_id: int, face_observation_id: int) -> ExcludeFacesResult:
        return self.exclude_faces(person_id=person_id, face_observation_ids=[face_observation_id])

    def exclude_faces(self, person_id: int, face_observation_ids: list[int]) -> ExcludeFacesResult:
        normalized_face_ids = sorted({int(face_id) for face_id in face_observation_ids if int(face_id) > 0})
        if not normalized_face_ids:
            raise ValueError("face_observation_ids 不能为空")
        conn = self._repo.connect()
        try:
            assert_people_writes_unlocked(conn)
            conn.execute("BEGIN IMMEDIATE")
            assert_people_writes_unlocked(conn)
            person = self._repo.get_person(int(person_id), conn=conn)
            if person is None or person.status != "active":
                raise PeopleNotFoundError(f"人物不存在或不可排除，id={person_id}")
            active_assignments_by_face = self._repo.list_active_assignments_for_faces(
                face_observation_ids=normalized_face_ids,
                conn=conn,
            )
            invalid_face_ids = [
                face_id
                for face_id in normalized_face_ids
                if int(active_assignments_by_face.get(face_id, AssignmentRecord(0, 0, 0, 0, "", False, None, None)).person_id)
                != int(person_id)
            ]
            if invalid_face_ids:
                raise PeopleExcludeConflictError(
                    f"face 当前并不归属该人物，person_id={person_id}, face_observation_ids={invalid_face_ids}"
                )
            for face_id in normalized_face_ids:
                self._repo.deactivate_assignment_for_person_face(
                    person_id=int(person_id),
                    face_observation_id=int(face_id),
                    conn=conn,
                )
                self._repo.activate_or_create_exclusion(
                    person_id=int(person_id),
                    face_observation_id=int(face_id),
                    conn=conn,
                )
            self._repo.prune_faces_from_active_clusters_for_person(
                person_id=int(person_id),
                face_observation_ids=normalized_face_ids,
                conn=conn,
            )
            self._repo.mark_faces_pending_reassign(face_observation_ids=normalized_face_ids, conn=conn)
            conn.commit()
            return ExcludeFacesResult(person_id=int(person_id), face_observation_ids=normalized_face_ids)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def merge_people(self, selected_person_ids: list[int]) -> MergePeopleResult:
        ordered_person_ids = _normalize_selected_person_ids(selected_person_ids)
        if len(ordered_person_ids) < 2:
            raise PeopleMergeError("至少需要两个不同人物才能合并")
        conn = self._repo.connect()
        try:
            assert_people_writes_unlocked(conn)
            conn.execute("BEGIN IMMEDIATE")
            assert_people_writes_unlocked(conn)
            persons = []
            persons_by_id: dict[int, PersonRecord] = {}
            for person_id in ordered_person_ids:
                person = self._repo.get_person(person_id, conn=conn)
                if person is None or person.status != "active":
                    raise PeopleNotFoundError(f"人物不存在或不可合并，id={person_id}")
                persons.append(person)
                persons_by_id[int(person.id)] = person
            winner = self._select_merge_winner(persons=persons, selected_person_ids=ordered_person_ids, conn=conn)
            loser_person_ids = [person_id for person_id in ordered_person_ids if person_id != winner.id]
            before_cluster_snapshots = self._repo.list_active_cluster_snapshots_for_people(
                person_ids=ordered_person_ids,
                conn=conn,
            )
            merge_operation_id = self._repo.create_merge_operation(
                selected_person_ids=ordered_person_ids,
                winner_person_id=winner.id,
                winner_person_uuid=winner.person_uuid,
                conn=conn,
            )
            winner_existing_exclusions = {
                int(item.face_observation_id)
                for item in self._repo.list_active_exclusions_for_people(person_ids=[winner.id], conn=conn)
            }
            loser_exclusions = self._repo.list_active_exclusions_for_people(person_ids=loser_person_ids, conn=conn)
            final_excluded_face_ids = winner_existing_exclusions | {
                int(item.face_observation_id)
                for item in loser_exclusions
            }
            conflict_face_ids = self._apply_merge_assignments(
                merge_operation_id=merge_operation_id,
                winner_person_id=winner.id,
                loser_person_ids=loser_person_ids,
                final_excluded_face_ids=final_excluded_face_ids,
                conn=conn,
            )
            self._apply_merge_exclusions(
                merge_operation_id=merge_operation_id,
                winner_person_id=winner.id,
                loser_person_ids=loser_person_ids,
                loser_exclusions=loser_exclusions,
                conn=conn,
            )
            for loser_person_id in loser_person_ids:
                loser_cluster_ids = [
                    snapshot.cluster_id
                    for snapshot in before_cluster_snapshots.get(loser_person_id, [])
                ]
                self._repo.move_clusters_to_person(
                    cluster_ids=loser_cluster_ids,
                    person_id=winner.id,
                    conn=conn,
                )
            if conflict_face_ids:
                self._repo.prune_faces_from_active_clusters_for_person(
                    person_id=winner.id,
                    face_observation_ids=sorted(conflict_face_ids),
                    conn=conn,
                )
            after_cluster_snapshots = self._repo.list_active_cluster_snapshots_for_people(
                person_ids=ordered_person_ids,
                conn=conn,
            )
            for person_id in ordered_person_ids:
                before = persons_by_id[person_id]
                before_snapshot = _person_snapshot(
                    before,
                    cluster_snapshots=before_cluster_snapshots.get(person_id, []),
                )
                if person_id == winner.id:
                    after = self._repo.get_person(person_id, conn=conn)
                else:
                    after = self._repo.update_person_merge_status(
                        person_id=person_id,
                        status="merged",
                        merged_into_person_id=winner.id,
                        conn=conn,
                    )
                after_snapshot = _person_snapshot(
                    after,
                    cluster_snapshots=after_cluster_snapshots.get(person_id, []),
                )
                after = self._repo.update_person_merge_status(
                    person_id=person_id,
                    status=after.status,
                    merged_into_person_id=after.merged_into_person_id,
                    conn=conn,
                )
                self._repo.insert_person_delta(
                    merge_operation_id=merge_operation_id,
                    person_id=person_id,
                    before_snapshot=before_snapshot,
                    after_snapshot=after_snapshot,
                    conn=conn,
                )
            conn.commit()
            return MergePeopleResult(
                merge_operation_id=merge_operation_id,
                winner_person_id=winner.id,
                loser_person_ids=loser_person_ids,
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def undo_last_merge(self) -> MergeUndoResult:
        conn = self._repo.connect()
        try:
            assert_people_writes_unlocked(conn)
            conn.execute("BEGIN IMMEDIATE")
            assert_people_writes_unlocked(conn)
            merge_operation = self._repo.get_latest_applied_merge_operation(conn=conn)
            if merge_operation is None:
                raise PeopleUndoMergeError("不存在可撤销的 merge_operation")
            self._ensure_merge_can_be_undone(merge_operation_id=merge_operation.id, conn=conn)
            for delta in self._repo.list_assignment_deltas(merge_operation_id=merge_operation.id, conn=conn):
                self._restore_assignment_delta(delta=delta, conn=conn)
            for delta in self._repo.list_exclusion_deltas(merge_operation_id=merge_operation.id, conn=conn):
                self._restore_exclusion_delta(delta=delta, conn=conn)
            for person_id, before_snapshot, _ in self._repo.list_person_deltas(
                merge_operation_id=merge_operation.id,
                conn=conn,
            ):
                for cluster_snapshot in _cluster_snapshots_from_payload(before_snapshot.get("cluster_snapshots", [])):
                    self._repo.restore_cluster_snapshot(snapshot=cluster_snapshot, conn=conn)
                self._repo.update_person_merge_status(
                    person_id=person_id,
                    status=str(before_snapshot["status"]),
                    merged_into_person_id=_as_optional_int(before_snapshot.get("merged_into_person_id")),
                    conn=conn,
                )
            self._repo.mark_merge_operation_undone(merge_operation_id=merge_operation.id, conn=conn)
            conn.commit()
            return MergeUndoResult(merge_operation_id=merge_operation.id)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _select_merge_winner(
        self,
        *,
        persons: list[PersonRecord],
        selected_person_ids: list[int],
        conn,
    ) -> PersonRecord:
        counts = self._repo.count_active_samples(
            person_ids=[person.id for person in persons],
            conn=conn,
        )
        order_index = {person_id: index for index, person_id in enumerate(selected_person_ids)}
        ranked = sorted(
            persons,
            key=lambda person: (-counts.get(person.id, 0), order_index[person.id]),
        )
        return ranked[0]

    def _apply_merge_assignments(
        self,
        *,
        merge_operation_id: int,
        winner_person_id: int,
        loser_person_ids: list[int],
        final_excluded_face_ids: set[int],
        conn,
    ) -> set[int]:
        conflict_face_ids: set[int] = set()
        winner_assignments = self._repo.list_active_assignments_for_people(person_ids=[winner_person_id], conn=conn)
        for assignment in winner_assignments:
            if int(assignment.face_observation_id) not in final_excluded_face_ids:
                continue
            conflict_face_ids.add(int(assignment.face_observation_id))
            before_snapshot = _assignment_snapshot(assignment, repo=self._repo, conn=conn)
            self._repo.deactivate_assignment_by_id(assignment_id=assignment.id, conn=conn)
            self._repo.set_face_pending_reassign(
                face_observation_id=assignment.face_observation_id,
                pending_reassign=True,
                conn=conn,
            )
            self._repo.insert_assignment_delta(
                merge_operation_id=merge_operation_id,
                face_observation_id=assignment.face_observation_id,
                before_snapshot=before_snapshot,
                after_snapshot=_assignment_snapshot(
                    self._repo.get_assignment_by_id(assignment.id, conn=conn),
                    repo=self._repo,
                    conn=conn,
                ),
                conn=conn,
            )
        for assignment in self._repo.list_active_assignments_for_people(person_ids=loser_person_ids, conn=conn):
            before_snapshot = _assignment_snapshot(assignment, repo=self._repo, conn=conn)
            self._repo.deactivate_assignment_by_id(assignment_id=assignment.id, conn=conn)
            if int(assignment.face_observation_id) in final_excluded_face_ids:
                conflict_face_ids.add(int(assignment.face_observation_id))
                self._repo.set_face_pending_reassign(
                    face_observation_id=assignment.face_observation_id,
                    pending_reassign=True,
                    conn=conn,
                )
                after_snapshot = _assignment_snapshot(
                    self._repo.get_assignment_by_id(assignment.id, conn=conn),
                    repo=self._repo,
                    conn=conn,
                )
            else:
                after_assignment = self._repo.insert_assignment(
                    person_id=winner_person_id,
                    face_observation_id=assignment.face_observation_id,
                    assignment_run_id=assignment.assignment_run_id,
                    assignment_source="merge",
                    confidence=assignment.confidence,
                    margin=assignment.margin,
                    conn=conn,
                )
                after_snapshot = _assignment_snapshot(after_assignment, repo=self._repo, conn=conn)
            self._repo.insert_assignment_delta(
                merge_operation_id=merge_operation_id,
                face_observation_id=assignment.face_observation_id,
                before_snapshot=before_snapshot,
                after_snapshot=after_snapshot,
                conn=conn,
            )
        return conflict_face_ids

    def _apply_merge_exclusions(
        self,
        *,
        merge_operation_id: int,
        winner_person_id: int,
        loser_person_ids: list[int],
        loser_exclusions: list[ExclusionRecord],
        conn,
    ) -> None:
        for exclusion in loser_exclusions:
            before_loser_snapshot = _exclusion_snapshot(exclusion)
            self._repo.update_exclusion_active(exclusion_id=exclusion.id, active=False, conn=conn)
            loser_after = self._repo.get_exclusion(
                person_id=exclusion.person_id,
                face_observation_id=exclusion.face_observation_id,
                conn=conn,
            )
            self._repo.insert_exclusion_delta(
                merge_operation_id=merge_operation_id,
                person_id=exclusion.person_id,
                face_observation_id=exclusion.face_observation_id,
                before_snapshot=before_loser_snapshot,
                after_snapshot=_exclusion_snapshot(loser_after),
                conn=conn,
            )
            winner_before = self._repo.get_exclusion(
                person_id=winner_person_id,
                face_observation_id=exclusion.face_observation_id,
                conn=conn,
            )
            if winner_before is not None and winner_before.active:
                continue
            winner_after = self._repo.activate_or_create_exclusion(
                person_id=winner_person_id,
                face_observation_id=exclusion.face_observation_id,
                conn=conn,
            )
            self._repo.insert_exclusion_delta(
                merge_operation_id=merge_operation_id,
                person_id=winner_person_id,
                face_observation_id=exclusion.face_observation_id,
                before_snapshot=_exclusion_snapshot(winner_before),
                after_snapshot=_exclusion_snapshot(winner_after),
                conn=conn,
            )

    def _restore_assignment_delta(self, *, delta: DeltaRecord, conn) -> None:
        after_snapshot = delta.after_payload
        before_snapshot = delta.before_payload
        after_row_id = _as_optional_int(after_snapshot.get("row_id"))
        if after_row_id is not None:
            self._repo.deactivate_assignment_by_id(assignment_id=after_row_id, conn=conn)
        if not bool(before_snapshot.get("exists")):
            return
        before_row_id = _as_optional_int(before_snapshot.get("row_id"))
        if before_row_id is not None:
            conn.execute(
                """
                UPDATE person_face_assignment
                SET person_id=?,
                    assignment_run_id=?,
                    assignment_source=?,
                    active=?,
                    confidence=?,
                    margin=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    int(before_snapshot["person_id"]),
                    int(before_snapshot["assignment_run_id"]),
                    str(before_snapshot["assignment_source"]),
                    1 if bool(before_snapshot.get("active", True)) else 0,
                    _as_optional_float(before_snapshot.get("confidence")),
                    _as_optional_float(before_snapshot.get("margin")),
                    int(before_row_id),
                ),
            )
            self._repo.set_face_pending_reassign(
                face_observation_id=int(before_snapshot["face_observation_id"]),
                pending_reassign=bool(before_snapshot.get("pending_reassign", False)),
                conn=conn,
            )
            return
        self._repo.insert_assignment(
            person_id=int(before_snapshot["person_id"]),
            face_observation_id=int(before_snapshot["face_observation_id"]),
            assignment_run_id=int(before_snapshot["assignment_run_id"]),
            assignment_source=str(before_snapshot["assignment_source"]),
            confidence=_as_optional_float(before_snapshot.get("confidence")),
            margin=_as_optional_float(before_snapshot.get("margin")),
            conn=conn,
        )
        self._repo.set_face_pending_reassign(
            face_observation_id=int(before_snapshot["face_observation_id"]),
            pending_reassign=bool(before_snapshot.get("pending_reassign", False)),
            conn=conn,
        )

    def _restore_exclusion_delta(self, *, delta: DeltaRecord, conn) -> None:
        before_snapshot = delta.before_payload
        after_snapshot = delta.after_payload
        row_id = _as_optional_int(before_snapshot.get("row_id")) or _as_optional_int(after_snapshot.get("row_id"))
        if row_id is None:
            return
        if not bool(before_snapshot.get("exists")):
            self._repo.update_exclusion_active(exclusion_id=row_id, active=False, conn=conn)
            return
        conn.execute(
            """
            UPDATE person_face_exclusion
            SET reason=?,
                active=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (
                str(before_snapshot.get("reason") or "manual_exclude"),
                1 if bool(before_snapshot.get("active")) else 0,
                int(row_id),
            ),
        )

    def _ensure_merge_can_be_undone(self, *, merge_operation_id: int, conn) -> None:
        merge_operation = self._repo.get_latest_applied_merge_operation(conn=conn)
        if merge_operation is None or int(merge_operation.id) != int(merge_operation_id):
            raise PeopleUndoMergeConflictError(f"merge_operation 不存在或已变更，id={merge_operation_id}")
        assignment_deltas = self._repo.list_assignment_deltas(merge_operation_id=merge_operation_id, conn=conn)
        active_assignments_by_face = self._repo.list_active_assignments_for_faces(
            face_observation_ids=[delta.face_observation_id for delta in assignment_deltas],
            conn=conn,
        )
        for delta in assignment_deltas:
            expected_after = delta.after_payload
            current = active_assignments_by_face.get(int(delta.face_observation_id))
            expected_active_row_id = (
                _as_optional_int(expected_after.get("row_id"))
                if bool(expected_after.get("exists")) and bool(expected_after.get("active"))
                else None
            )
            if expected_active_row_id is None:
                if current is not None:
                    raise PeopleUndoMergeConflictError(
                        f"merge 之后 face 已被重新归属，face_observation_id={delta.face_observation_id}"
                    )
                continue
            if current is None or int(current.id) != int(expected_active_row_id):
                raise PeopleUndoMergeConflictError(
                    f"merge 之后 face 当前 active assignment 已漂移，face_observation_id={delta.face_observation_id}"
                )

        exclusion_deltas = self._repo.list_exclusion_deltas(merge_operation_id=merge_operation_id, conn=conn)
        active_exclusions = self._repo.list_active_exclusions_for_face_person_pairs(
            face_person_pairs=[
                (delta.face_observation_id, delta.person_id)
                for delta in exclusion_deltas
            ],
            conn=conn,
        )
        for delta in exclusion_deltas:
            expected_after = delta.after_payload
            current = active_exclusions.get((int(delta.face_observation_id), int(delta.person_id)))
            expected_active_row_id = (
                _as_optional_int(expected_after.get("row_id"))
                if bool(expected_after.get("exists")) and bool(expected_after.get("active"))
                else None
            )
            if expected_active_row_id is None:
                if current is not None:
                    raise PeopleUndoMergeConflictError(
                        "merge 之后 exclusion 已漂移，"
                        f"person_id={delta.person_id}, face_observation_id={delta.face_observation_id}"
                    )
                continue
            if current is None or int(current.id) != int(expected_active_row_id):
                raise PeopleUndoMergeConflictError(
                    "merge 之后 exclusion 当前 active row 已漂移，"
                    f"person_id={delta.person_id}, face_observation_id={delta.face_observation_id}"
                )

        person_deltas = self._repo.list_person_deltas(merge_operation_id=merge_operation_id, conn=conn)
        person_deltas_by_id = {
            int(person_id): (before_snapshot, after_snapshot)
            for person_id, before_snapshot, after_snapshot in person_deltas
        }
        tracked_person_ids = [int(person_id) for person_id in merge_operation.selected_person_ids]
        current_cluster_snapshots = self._repo.list_active_cluster_snapshots_for_people(
            person_ids=tracked_person_ids,
            conn=conn,
        )
        for person_id in tracked_person_ids:
            if int(person_id) not in person_deltas_by_id:
                raise PeopleUndoMergeConflictError(
                    f"merge 缺少人物快照，无法安全撤销，person_id={person_id}"
                )
            _, after_snapshot = person_deltas_by_id[int(person_id)]
            expected_payload = after_snapshot.get("cluster_snapshots", [])
            expected_cluster_snapshots = _cluster_snapshots_from_payload(expected_payload)
            current_cluster_payload = _cluster_snapshots_payload(
                current_cluster_snapshots.get(int(person_id), []),
            )
            expected_cluster_payload = _cluster_snapshots_payload(expected_cluster_snapshots)
            if current_cluster_payload != expected_cluster_payload:
                raise PeopleUndoMergeConflictError(
                    f"merge 之后 cluster 真相层已漂移，person_id={person_id}"
                )


def _normalize_selected_person_ids(selected_person_ids: list[int]) -> list[int]:
    ordered: list[int] = []
    seen: set[int] = set()
    for person_id in selected_person_ids:
        normalized = int(person_id)
        if normalized <= 0 or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _person_snapshot(
    person: PersonRecord | None,
    *,
    cluster_snapshots: list[ClusterSnapshotRecord] | None = None,
) -> dict[str, object]:
    if person is None:
        return {"exists": False}
    return {
        "exists": True,
        "row_id": person.id,
        "person_uuid": person.person_uuid,
        "display_name": person.display_name,
        "is_named": person.is_named,
        "status": person.status,
        "merged_into_person_id": person.merged_into_person_id,
        "cluster_snapshots": [
            {
                "cluster_id": int(snapshot.cluster_id),
                "person_id": int(snapshot.person_id),
                "status": str(snapshot.status),
                "rebuild_scope": str(snapshot.rebuild_scope),
                "created_assignment_run_id": int(snapshot.created_assignment_run_id),
                "updated_assignment_run_id": int(snapshot.updated_assignment_run_id),
                "member_face_ids": [int(face_id) for face_id in snapshot.member_face_ids],
                "rep_face_ids": [int(face_id) for face_id in snapshot.rep_face_ids],
            }
            for snapshot in (cluster_snapshots or [])
        ],
    }


def _assignment_snapshot(
    assignment: AssignmentRecord | None,
    *,
    repo: PeopleRepository | None = None,
    conn=None,
) -> dict[str, object]:
    if assignment is None:
        return {"exists": False}
    pending_reassign = False
    if repo is not None and conn is not None:
        pending_reassign = repo.get_face_pending_reassign(
            face_observation_id=assignment.face_observation_id,
            conn=conn,
        )
    return {
        "exists": True,
        "row_id": assignment.id,
        "person_id": assignment.person_id,
        "face_observation_id": assignment.face_observation_id,
        "assignment_run_id": assignment.assignment_run_id,
        "assignment_source": assignment.assignment_source,
        "active": assignment.active,
        "confidence": assignment.confidence,
        "margin": assignment.margin,
        "pending_reassign": pending_reassign,
    }


def _exclusion_snapshot(exclusion: ExclusionRecord | None) -> dict[str, object]:
    if exclusion is None:
        return {"exists": False}
    return {
        "exists": True,
        "row_id": exclusion.id,
        "person_id": exclusion.person_id,
        "face_observation_id": exclusion.face_observation_id,
        "reason": exclusion.reason,
        "active": exclusion.active,
    }


def _as_optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _as_optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


def _is_database_locked_error(exc: sqlite3.OperationalError) -> bool:
    return "database is locked" in str(exc).lower()


def _rollback_quietly(conn) -> None:
    try:
        conn.rollback()
    except sqlite3.Error:
        return


def _cluster_snapshots_from_payload(payload: object) -> list[ClusterSnapshotRecord]:
    rows = payload if isinstance(payload, list) else []
    snapshots: list[ClusterSnapshotRecord] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        snapshots.append(
            ClusterSnapshotRecord(
                cluster_id=int(row["cluster_id"]),
                person_id=int(row["person_id"]),
                status=str(row["status"]),
                rebuild_scope=str(row["rebuild_scope"]),
                created_assignment_run_id=int(row["created_assignment_run_id"]),
                updated_assignment_run_id=int(row["updated_assignment_run_id"]),
                member_face_ids=[int(face_id) for face_id in row.get("member_face_ids", [])],
                rep_face_ids=[int(face_id) for face_id in row.get("rep_face_ids", [])],
            )
        )
    return snapshots


def _cluster_snapshots_payload(snapshots: list[ClusterSnapshotRecord]) -> list[dict[str, object]]:
    ordered = sorted(
        snapshots,
        key=lambda snapshot: int(snapshot.cluster_id),
    )
    return [
        {
            "cluster_id": int(snapshot.cluster_id),
            "person_id": int(snapshot.person_id),
            "status": str(snapshot.status),
            "rebuild_scope": str(snapshot.rebuild_scope),
            "created_assignment_run_id": int(snapshot.created_assignment_run_id),
            "updated_assignment_run_id": int(snapshot.updated_assignment_run_id),
            "member_face_ids": [int(face_id) for face_id in sorted(snapshot.member_face_ids)],
            "rep_face_ids": [int(face_id) for face_id in sorted(snapshot.rep_face_ids)],
        }
        for snapshot in ordered
    ]
