from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from hikbox_pictures.product.db.connection import connect_sqlite


@dataclass(frozen=True)
class PersonView:
    id: int
    person_uuid: str
    display_name: str | None
    is_named: bool
    status: str
    merged_into_person_id: int | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ExcludeAssignmentResult:
    person_id: int
    face_observation_id: int
    pending_reassign: int


@dataclass(frozen=True)
class ExcludeAssignmentsResult:
    person_id: int
    excluded_count: int


@dataclass(frozen=True)
class MergeOperationResult:
    merge_operation_id: int
    winner_person_id: int
    winner_person_uuid: str


@dataclass(frozen=True)
class UndoMergeResult:
    merge_operation_id: int
    status: str


class SQLitePeopleRepository:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def get_person(self, person_id: int) -> PersonView | None:
        with connect_sqlite(self._db_path) as conn:
            row = conn.execute("SELECT * FROM person WHERE id=?", (person_id,)).fetchone()
        if row is None:
            return None
        return _row_to_person(row)

    def rename_person(self, *, person_id: int, display_name: str, now: str) -> PersonView:
        clean_name = display_name.strip()
        if not clean_name:
            raise ValueError("display_name 不能为空")
        with connect_sqlite(self._db_path) as conn:
            person = self._get_person_row_in_conn(conn, person_id)
            if person is None:
                raise ValueError(f"person 不存在: id={person_id}")
            if str(person[4]) != "active":
                raise ValueError(f"person 状态={person[4]}，不允许重命名")
            conn.execute(
                """
                UPDATE person
                SET display_name=?,
                    is_named=1,
                    updated_at=?
                WHERE id=?
                """,
                (clean_name, now, person_id),
            )
            row = self._get_person_row_in_conn(conn, person_id)
            conn.commit()
        assert row is not None
        return _row_to_person(row)

    def exclude_assignment(
        self,
        *,
        person_id: int,
        face_observation_id: int,
        now: str,
    ) -> ExcludeAssignmentResult:
        with connect_sqlite(self._db_path) as conn:
            self._exclude_one_in_conn(
                conn,
                person_id=person_id,
                face_observation_id=face_observation_id,
                now=now,
            )
            conn.commit()
        return ExcludeAssignmentResult(
            person_id=person_id,
            face_observation_id=face_observation_id,
            pending_reassign=1,
        )

    def exclude_assignments(
        self,
        *,
        person_id: int,
        face_observation_ids: list[int],
        now: str,
    ) -> ExcludeAssignmentsResult:
        if not face_observation_ids:
            raise ValueError("face_observation_ids 不能为空")
        unique_ids: list[int] = []
        seen: set[int] = set()
        for face_observation_id in face_observation_ids:
            if face_observation_id in seen:
                continue
            seen.add(face_observation_id)
            unique_ids.append(face_observation_id)

        with connect_sqlite(self._db_path) as conn:
            for face_observation_id in unique_ids:
                self._exclude_one_in_conn(
                    conn,
                    person_id=person_id,
                    face_observation_id=face_observation_id,
                    now=now,
                )
            conn.commit()

        return ExcludeAssignmentsResult(person_id=person_id, excluded_count=len(unique_ids))

    def merge_people(self, *, selected_person_ids: list[int], now: str) -> MergeOperationResult:
        with connect_sqlite(self._db_path) as conn:
            selected = list(dict.fromkeys(selected_person_ids))
            if len(selected) < 2:
                raise ValueError("selected_person_ids 去重后至少需要 2 个人物")
            persons = self._load_people_in_conn(conn, selected)
            missing = [person_id for person_id in selected if person_id not in persons]
            if missing:
                raise ValueError(f"person 不存在: ids={missing}")
            for person_id in selected:
                status = persons[person_id]["status"]
                if status != "active":
                    raise ValueError(f"person 状态={status}，不能参与 merge: id={person_id}")

            sample_counts = self._count_active_assignments(conn, selected)
            winner_person_id = self._pick_winner(
                selected_person_ids=selected,
                sample_counts=sample_counts,
            )
            winner_person_uuid = str(persons[winner_person_id]["person_uuid"])

            merge_cursor = conn.execute(
                """
                INSERT INTO merge_operation(
                  selected_person_ids_json,
                  winner_person_id,
                  winner_person_uuid,
                  status,
                  created_at,
                  undone_at
                )
                VALUES (?, ?, ?, 'applied', ?, NULL)
                """,
                (
                    json.dumps(selected, ensure_ascii=False),
                    winner_person_id,
                    winner_person_uuid,
                    now,
                ),
            )
            merge_operation_id = int(merge_cursor.lastrowid)

            losers = [person_id for person_id in selected if person_id != winner_person_id]

            self._record_and_apply_person_delta(
                conn,
                merge_operation_id=merge_operation_id,
                loser_person_ids=losers,
                winner_person_id=winner_person_id,
                now=now,
            )
            self._record_and_apply_assignment_delta(
                conn,
                merge_operation_id=merge_operation_id,
                loser_person_ids=losers,
                winner_person_id=winner_person_id,
                now=now,
            )
            self._record_and_apply_exclusion_delta(
                conn,
                merge_operation_id=merge_operation_id,
                loser_person_ids=losers,
                winner_person_id=winner_person_id,
                now=now,
            )

            conn.commit()

        return MergeOperationResult(
            merge_operation_id=merge_operation_id,
            winner_person_id=winner_person_id,
            winner_person_uuid=winner_person_uuid,
        )

    def undo_last_merge(self, *, now: str) -> UndoMergeResult:
        with connect_sqlite(self._db_path) as conn:
            latest = conn.execute(
                """
                SELECT id, status
                FROM merge_operation
                ORDER BY id DESC
                LIMIT 1
                """,
            ).fetchone()
            if latest is None or str(latest[1]) != "applied":
                raise LookupError("没有可撤销的最近 merge 操作")

            merge_operation_id = int(latest[0])

            person_deltas = conn.execute(
                """
                SELECT person_id, before_snapshot_json
                FROM merge_operation_person_delta
                WHERE merge_operation_id=?
                ORDER BY id DESC
                """,
                (merge_operation_id,),
            ).fetchall()
            for delta in person_deltas:
                person_id = int(delta[0])
                before = json.loads(str(delta[1]))
                conn.execute(
                    """
                    UPDATE person
                    SET display_name=?,
                        is_named=?,
                        status=?,
                        merged_into_person_id=?,
                        updated_at=?
                    WHERE id=?
                    """,
                    (
                        before.get("display_name"),
                        int(before.get("is_named", 0)),
                        str(before["status"]),
                        before.get("merged_into_person_id"),
                        now,
                        person_id,
                    ),
                )

            assignment_deltas = conn.execute(
                """
                SELECT before_assignment_json
                FROM merge_operation_assignment_delta
                WHERE merge_operation_id=?
                ORDER BY id DESC
                """,
                (merge_operation_id,),
            ).fetchall()
            for delta in assignment_deltas:
                before = json.loads(str(delta[0]))
                conn.execute(
                    """
                    UPDATE person_face_assignment
                    SET person_id=?,
                        active=?,
                        assignment_source='undo',
                        updated_at=?
                    WHERE id=?
                    """,
                    (
                        int(before["person_id"]),
                        int(before["active"]),
                        now,
                        int(before["id"]),
                    ),
                )

            exclusion_deltas = conn.execute(
                """
                SELECT before_exclusion_json, after_exclusion_json
                FROM merge_operation_exclusion_delta
                WHERE merge_operation_id=?
                ORDER BY id DESC
                """,
                (merge_operation_id,),
            ).fetchall()
            for delta in exclusion_deltas:
                before = json.loads(str(delta[0]))
                after = json.loads(str(delta[1]))

                conn.execute(
                    "UPDATE person_face_exclusion SET active=?, updated_at=? WHERE id=?",
                    (int(before["loser_active"]), now, int(before["loser_row_id"])),
                )

                winner_before_id = before.get("winner_row_id")
                if winner_before_id is None:
                    winner_after_id = after.get("winner_row_id")
                    if winner_after_id is not None:
                        conn.execute(
                            "UPDATE person_face_exclusion SET active=0, updated_at=? WHERE id=?",
                            (now, int(winner_after_id)),
                        )
                else:
                    winner_before_active = before.get("winner_active")
                    if winner_before_active is not None:
                        conn.execute(
                            "UPDATE person_face_exclusion SET active=?, updated_at=? WHERE id=?",
                            (int(winner_before_active), now, int(winner_before_id)),
                        )

            updated = conn.execute(
                """
                UPDATE merge_operation
                SET status='undone',
                    undone_at=?
                WHERE id=?
                  AND status='applied'
                """,
                (now, merge_operation_id),
            )
            if updated.rowcount != 1:
                raise LookupError("merge 操作已不可撤销")

            conn.commit()

        return UndoMergeResult(merge_operation_id=merge_operation_id, status="undone")

    def _exclude_one_in_conn(
        self,
        conn: sqlite3.Connection,
        *,
        person_id: int,
        face_observation_id: int,
        now: str,
    ) -> None:
        person = self._get_person_row_in_conn(conn, person_id)
        if person is None:
            raise ValueError(f"person 不存在: id={person_id}")
        if str(person[4]) != "active":
            raise ValueError(f"person 状态={person[4]}，不允许排除")

        active_exclusion = conn.execute(
            """
            SELECT id
            FROM person_face_exclusion
            WHERE person_id=?
              AND face_observation_id=?
              AND active=1
            """,
            (person_id, face_observation_id),
        ).fetchone()
        if active_exclusion is not None:
            raise ValueError(f"face_observation_id={face_observation_id} 已排除")

        assignment = conn.execute(
            """
            SELECT id
            FROM person_face_assignment
            WHERE person_id=?
              AND face_observation_id=?
              AND active=1
            """,
            (person_id, face_observation_id),
        ).fetchone()
        if assignment is None:
            raise ValueError(
                f"未找到 active assignment: person_id={person_id}, face_observation_id={face_observation_id}",
            )

        conn.execute(
            """
            UPDATE person_face_assignment
            SET active=0,
                updated_at=?
            WHERE id=?
            """,
            (now, int(assignment[0])),
        )

        existing_exclusion = conn.execute(
            """
            SELECT id
            FROM person_face_exclusion
            WHERE person_id=?
              AND face_observation_id=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (person_id, face_observation_id),
        ).fetchone()
        if existing_exclusion is None:
            conn.execute(
                """
                INSERT INTO person_face_exclusion(
                  person_id,
                  face_observation_id,
                  reason,
                  active,
                  created_at,
                  updated_at
                )
                VALUES (?, ?, 'manual_exclude', 1, ?, ?)
                """,
                (person_id, face_observation_id, now, now),
            )
        else:
            conn.execute(
                """
                UPDATE person_face_exclusion
                SET reason='manual_exclude',
                    active=1,
                    updated_at=?
                WHERE id=?
                """,
                (now, int(existing_exclusion[0])),
            )

        touched_observation = conn.execute(
            """
            UPDATE face_observation
            SET pending_reassign=1,
                updated_at=?
            WHERE id=?
              AND active=1
            """,
            (now, face_observation_id),
        )
        if touched_observation.rowcount != 1:
            raise ValueError(f"face_observation 不存在或 inactive: id={face_observation_id}")

    def _get_person_row_in_conn(self, conn: sqlite3.Connection, person_id: int) -> sqlite3.Row | tuple[object, ...] | None:
        return conn.execute("SELECT * FROM person WHERE id=?", (person_id,)).fetchone()

    def _load_people_in_conn(self, conn: sqlite3.Connection, person_ids: list[int]) -> dict[int, dict[str, object | None]]:
        if not person_ids:
            return {}
        placeholders = ",".join("?" for _ in person_ids)
        rows = conn.execute(
            f"SELECT * FROM person WHERE id IN ({placeholders})",
            tuple(person_ids),
        ).fetchall()
        people: dict[int, dict[str, object | None]] = {}
        for row in rows:
            person_id = int(row[0])
            people[person_id] = {
                "id": person_id,
                "person_uuid": str(row[1]),
                "display_name": str(row[2]) if row[2] is not None else None,
                "is_named": int(row[3]),
                "status": str(row[4]),
                "merged_into_person_id": int(row[5]) if row[5] is not None else None,
                "created_at": str(row[6]),
                "updated_at": str(row[7]),
            }
        return people

    def _count_active_assignments(self, conn: sqlite3.Connection, person_ids: list[int]) -> dict[int, int]:
        counts = {person_id: 0 for person_id in person_ids}
        placeholders = ",".join("?" for _ in person_ids)
        rows = conn.execute(
            f"""
            SELECT person_id, COUNT(*)
            FROM person_face_assignment
            WHERE active=1
              AND person_id IN ({placeholders})
            GROUP BY person_id
            """,
            tuple(person_ids),
        ).fetchall()
        for row in rows:
            counts[int(row[0])] = int(row[1])
        return counts

    def _pick_winner(self, *, selected_person_ids: list[int], sample_counts: dict[int, int]) -> int:
        max_count = max(sample_counts[person_id] for person_id in selected_person_ids)
        for person_id in selected_person_ids:
            if sample_counts[person_id] == max_count:
                return person_id
        raise AssertionError("无法确定 winner")

    def _record_and_apply_person_delta(
        self,
        conn: sqlite3.Connection,
        *,
        merge_operation_id: int,
        loser_person_ids: list[int],
        winner_person_id: int,
        now: str,
    ) -> None:
        for person_id in loser_person_ids:
            before_row = self._get_person_row_in_conn(conn, person_id)
            assert before_row is not None
            before = _person_snapshot(before_row)

            conn.execute(
                """
                UPDATE person
                SET status='merged',
                    merged_into_person_id=?,
                    updated_at=?
                WHERE id=?
                """,
                (winner_person_id, now, person_id),
            )

            after_row = self._get_person_row_in_conn(conn, person_id)
            assert after_row is not None
            after = _person_snapshot(after_row)

            conn.execute(
                """
                INSERT INTO merge_operation_person_delta(
                  merge_operation_id,
                  person_id,
                  before_snapshot_json,
                  after_snapshot_json
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    merge_operation_id,
                    person_id,
                    json.dumps(before, ensure_ascii=False),
                    json.dumps(after, ensure_ascii=False),
                ),
            )

    def _record_and_apply_assignment_delta(
        self,
        conn: sqlite3.Connection,
        *,
        merge_operation_id: int,
        loser_person_ids: list[int],
        winner_person_id: int,
        now: str,
    ) -> None:
        if not loser_person_ids:
            return
        placeholders = ",".join("?" for _ in loser_person_ids)
        rows = conn.execute(
            f"""
            SELECT id,
                   person_id,
                   face_observation_id,
                   assignment_run_id,
                   assignment_source,
                   active,
                   confidence,
                   margin,
                   created_at,
                   updated_at
            FROM person_face_assignment
            WHERE active=1
              AND person_id IN ({placeholders})
            ORDER BY id
            """,
            tuple(loser_person_ids),
        ).fetchall()

        for row in rows:
            assignment_id = int(row[0])
            face_observation_id = int(row[2])
            before = _assignment_snapshot(row)
            conn.execute(
                """
                UPDATE person_face_assignment
                SET person_id=?,
                    assignment_source='merge',
                    updated_at=?
                WHERE id=?
                """,
                (winner_person_id, now, assignment_id),
            )
            after_row = conn.execute(
                """
                SELECT id,
                       person_id,
                       face_observation_id,
                       assignment_run_id,
                       assignment_source,
                       active,
                       confidence,
                       margin,
                       created_at,
                       updated_at
                FROM person_face_assignment
                WHERE id=?
                """,
                (assignment_id,),
            ).fetchone()
            assert after_row is not None
            after = _assignment_snapshot(after_row)
            conn.execute(
                """
                INSERT INTO merge_operation_assignment_delta(
                  merge_operation_id,
                  face_observation_id,
                  before_assignment_json,
                  after_assignment_json
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    merge_operation_id,
                    face_observation_id,
                    json.dumps(before, ensure_ascii=False),
                    json.dumps(after, ensure_ascii=False),
                ),
            )

    def _record_and_apply_exclusion_delta(
        self,
        conn: sqlite3.Connection,
        *,
        merge_operation_id: int,
        loser_person_ids: list[int],
        winner_person_id: int,
        now: str,
    ) -> None:
        for loser_person_id in loser_person_ids:
            rows = conn.execute(
                """
                SELECT id, face_observation_id
                FROM person_face_exclusion
                WHERE person_id=?
                  AND active=1
                ORDER BY id
                """,
                (loser_person_id,),
            ).fetchall()
            for row in rows:
                loser_exclusion_id = int(row[0])
                face_observation_id = int(row[1])
                winner_row = conn.execute(
                    """
                    SELECT id, active
                    FROM person_face_exclusion
                    WHERE person_id=?
                      AND face_observation_id=?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (winner_person_id, face_observation_id),
                ).fetchone()

                before = {
                    "loser_row_id": loser_exclusion_id,
                    "loser_active": 1,
                    "winner_row_id": int(winner_row[0]) if winner_row is not None else None,
                    "winner_active": int(winner_row[1]) if winner_row is not None else None,
                }

                conn.execute(
                    """
                    UPDATE person_face_exclusion
                    SET active=0,
                        updated_at=?
                    WHERE id=?
                    """,
                    (now, loser_exclusion_id),
                )

                winner_row_id: int
                if winner_row is None:
                    cursor = conn.execute(
                        """
                        INSERT INTO person_face_exclusion(
                          person_id,
                          face_observation_id,
                          reason,
                          active,
                          created_at,
                          updated_at
                        )
                        VALUES (?, ?, 'manual_exclude', 1, ?, ?)
                        """,
                        (winner_person_id, face_observation_id, now, now),
                    )
                    winner_row_id = int(cursor.lastrowid)
                else:
                    winner_row_id = int(winner_row[0])
                    if int(winner_row[1]) == 0:
                        conn.execute(
                            """
                            UPDATE person_face_exclusion
                            SET active=1,
                                reason='manual_exclude',
                                updated_at=?
                            WHERE id=?
                            """,
                            (now, winner_row_id),
                        )

                after = {
                    "loser_row_id": loser_exclusion_id,
                    "loser_active": 0,
                    "winner_row_id": winner_row_id,
                    "winner_active": 1,
                }

                conn.execute(
                    """
                    INSERT INTO merge_operation_exclusion_delta(
                      merge_operation_id,
                      person_id,
                      face_observation_id,
                      before_exclusion_json,
                      after_exclusion_json
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        merge_operation_id,
                        loser_person_id,
                        face_observation_id,
                        json.dumps(before, ensure_ascii=False),
                        json.dumps(after, ensure_ascii=False),
                    ),
                )


def _row_to_person(row: sqlite3.Row | tuple[object, ...]) -> PersonView:
    return PersonView(
        id=int(row[0]),
        person_uuid=str(row[1]),
        display_name=str(row[2]) if row[2] is not None else None,
        is_named=bool(row[3]),
        status=str(row[4]),
        merged_into_person_id=int(row[5]) if row[5] is not None else None,
        created_at=str(row[6]),
        updated_at=str(row[7]),
    )


def _person_snapshot(row: sqlite3.Row | tuple[object, ...]) -> dict[str, object | None]:
    return {
        "id": int(row[0]),
        "person_uuid": str(row[1]),
        "display_name": str(row[2]) if row[2] is not None else None,
        "is_named": int(row[3]),
        "status": str(row[4]),
        "merged_into_person_id": int(row[5]) if row[5] is not None else None,
    }


def _assignment_snapshot(row: sqlite3.Row | tuple[object, ...]) -> dict[str, object | None]:
    return {
        "id": int(row[0]),
        "person_id": int(row[1]),
        "face_observation_id": int(row[2]),
        "assignment_run_id": int(row[3]),
        "assignment_source": str(row[4]),
        "active": int(row[5]),
        "confidence": float(row[6]) if row[6] is not None else None,
        "margin": float(row[7]) if row[7] is not None else None,
        "created_at": str(row[8]),
        "updated_at": str(row[9]),
    }
