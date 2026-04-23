"""人物维护仓储层。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from hikbox_pictures.product.db.connection import connect_sqlite


@dataclass(frozen=True)
class PersonRecord:
    id: int
    person_uuid: str
    display_name: str | None
    is_named: bool
    status: str
    merged_into_person_id: int | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AssignmentRecord:
    id: int
    person_id: int
    face_observation_id: int
    assignment_run_id: int
    assignment_source: str
    active: bool
    confidence: float | None
    margin: float | None


@dataclass(frozen=True)
class ExclusionRecord:
    id: int
    person_id: int
    face_observation_id: int
    reason: str
    active: bool


@dataclass(frozen=True)
class MergeOperationRecord:
    id: int
    selected_person_ids: list[int]
    winner_person_id: int
    winner_person_uuid: str
    status: str
    created_at: str
    undone_at: str | None


@dataclass(frozen=True)
class DeltaRecord:
    person_id: int
    face_observation_id: int
    before_payload: dict[str, object]
    after_payload: dict[str, object]


@dataclass(frozen=True)
class ClusterSnapshotRecord:
    cluster_id: int
    person_id: int
    status: str
    rebuild_scope: str
    created_assignment_run_id: int
    updated_assignment_run_id: int
    member_face_ids: list[int]
    rep_face_ids: list[int]


class PeopleRepository:
    """`person` 及人物维护相关表访问。"""

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        return connect_sqlite(self._db_path)

    def get_person(self, person_id: int, *, conn: sqlite3.Connection) -> PersonRecord | None:
        row = conn.execute(
            """
            SELECT id, person_uuid, display_name, is_named, status, merged_into_person_id, created_at, updated_at
            FROM person
            WHERE id=?
            """,
            (int(person_id),),
        ).fetchone()
        if row is None:
            return None
        return _to_person_record(row)

    def rename_person(self, *, person_id: int, display_name: str, conn: sqlite3.Connection) -> PersonRecord | None:
        conn.execute(
            """
            UPDATE person
            SET display_name=?,
                is_named=1,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status='active'
            """,
            (display_name, int(person_id)),
        )
        if conn.total_changes <= 0:
            return None
        return self.get_person(person_id, conn=conn)

    def deactivate_assignment_for_person_face(
        self,
        *,
        person_id: int,
        face_observation_id: int,
        conn: sqlite3.Connection,
    ) -> None:
        conn.execute(
            """
            UPDATE person_face_assignment
            SET active=0,
                updated_at=CURRENT_TIMESTAMP
            WHERE person_id=?
              AND face_observation_id=?
              AND active=1
            """,
            (int(person_id), int(face_observation_id)),
        )

    def list_active_assignments_for_people(
        self,
        *,
        person_ids: list[int],
        conn: sqlite3.Connection,
    ) -> list[AssignmentRecord]:
        if not person_ids:
            return []
        placeholders = ", ".join("?" for _ in person_ids)
        rows = conn.execute(
            f"""
            SELECT id, person_id, face_observation_id, assignment_run_id, assignment_source, active, confidence, margin
            FROM person_face_assignment
            WHERE active=1
              AND person_id IN ({placeholders})
            ORDER BY id ASC
            """,
            tuple(int(person_id) for person_id in person_ids),
        ).fetchall()
        return [_to_assignment_record(row) for row in rows]

    def list_active_assignments_for_faces(
        self,
        *,
        face_observation_ids: list[int],
        conn: sqlite3.Connection,
    ) -> dict[int, AssignmentRecord]:
        unique_ids = sorted({int(face_id) for face_id in face_observation_ids if int(face_id) > 0})
        if not unique_ids:
            return {}
        placeholders = ", ".join("?" for _ in unique_ids)
        rows = conn.execute(
            f"""
            SELECT id, person_id, face_observation_id, assignment_run_id, assignment_source, active, confidence, margin
            FROM person_face_assignment
            WHERE active=1
              AND face_observation_id IN ({placeholders})
            ORDER BY face_observation_id ASC
            """,
            tuple(unique_ids),
        ).fetchall()
        return {
            int(record.face_observation_id): record
            for record in (_to_assignment_record(row) for row in rows)
        }

    def deactivate_assignment_by_id(self, *, assignment_id: int, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            UPDATE person_face_assignment
            SET active=0,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (int(assignment_id),),
        )

    def insert_assignment(
        self,
        *,
        person_id: int,
        face_observation_id: int,
        assignment_run_id: int,
        assignment_source: str,
        confidence: float | None,
        margin: float | None,
        conn: sqlite3.Connection,
    ) -> AssignmentRecord:
        cursor = conn.execute(
            """
            INSERT INTO person_face_assignment(
              person_id, face_observation_id, assignment_run_id, assignment_source,
              active, confidence, margin, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 1, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                int(person_id),
                int(face_observation_id),
                int(assignment_run_id),
                str(assignment_source),
                None if confidence is None else float(confidence),
                None if margin is None else float(margin),
            ),
        )
        return self.get_assignment_by_id(int(cursor.lastrowid), conn=conn)

    def get_assignment_by_id(self, assignment_id: int, *, conn: sqlite3.Connection) -> AssignmentRecord:
        row = conn.execute(
            """
            SELECT id, person_id, face_observation_id, assignment_run_id, assignment_source, active, confidence, margin
            FROM person_face_assignment
            WHERE id=?
            """,
            (int(assignment_id),),
        ).fetchone()
        return _to_assignment_record(row)

    def get_exclusion(self, *, person_id: int, face_observation_id: int, conn: sqlite3.Connection) -> ExclusionRecord | None:
        row = conn.execute(
            """
            SELECT id, person_id, face_observation_id, reason, active
            FROM person_face_exclusion
            WHERE person_id=? AND face_observation_id=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(person_id), int(face_observation_id)),
        ).fetchone()
        if row is None:
            return None
        return _to_exclusion_record(row)

    def list_active_exclusions_for_people(
        self,
        *,
        person_ids: list[int],
        conn: sqlite3.Connection,
    ) -> list[ExclusionRecord]:
        if not person_ids:
            return []
        placeholders = ", ".join("?" for _ in person_ids)
        rows = conn.execute(
            f"""
            SELECT id, person_id, face_observation_id, reason, active
            FROM person_face_exclusion
            WHERE active=1
              AND person_id IN ({placeholders})
            ORDER BY id ASC
            """,
            tuple(int(person_id) for person_id in person_ids),
        ).fetchall()
        return [_to_exclusion_record(row) for row in rows]

    def activate_or_create_exclusion(
        self,
        *,
        person_id: int,
        face_observation_id: int,
        conn: sqlite3.Connection,
    ) -> ExclusionRecord:
        existing = self.get_exclusion(person_id=person_id, face_observation_id=face_observation_id, conn=conn)
        if existing is not None:
            conn.execute(
                """
                UPDATE person_face_exclusion
                SET reason='manual_exclude',
                    active=1,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (existing.id,),
            )
            return self.get_exclusion(person_id=person_id, face_observation_id=face_observation_id, conn=conn)  # type: ignore[return-value]
        cursor = conn.execute(
            """
            INSERT INTO person_face_exclusion(
              person_id, face_observation_id, reason, active, created_at, updated_at
            ) VALUES (?, ?, 'manual_exclude', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (int(person_id), int(face_observation_id)),
        )
        row = conn.execute(
            """
            SELECT id, person_id, face_observation_id, reason, active
            FROM person_face_exclusion
            WHERE id=?
            """,
            (int(cursor.lastrowid),),
        ).fetchone()
        return _to_exclusion_record(row)

    def update_exclusion_active(self, *, exclusion_id: int, active: bool, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            UPDATE person_face_exclusion
            SET active=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (1 if active else 0, int(exclusion_id)),
        )

    def mark_faces_pending_reassign(self, *, face_observation_ids: list[int], conn: sqlite3.Connection) -> None:
        if not face_observation_ids:
            return
        placeholders = ", ".join("?" for _ in face_observation_ids)
        conn.execute(
            f"""
            UPDATE face_observation
            SET pending_reassign=1,
                updated_at=CURRENT_TIMESTAMP
            WHERE id IN ({placeholders})
            """,
            tuple(int(face_id) for face_id in face_observation_ids),
        )

    def set_face_pending_reassign(
        self,
        *,
        face_observation_id: int,
        pending_reassign: bool,
        conn: sqlite3.Connection,
    ) -> None:
        conn.execute(
            """
            UPDATE face_observation
            SET pending_reassign=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (1 if pending_reassign else 0, int(face_observation_id)),
        )

    def get_face_pending_reassign(self, *, face_observation_id: int, conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT pending_reassign FROM face_observation WHERE id=?",
            (int(face_observation_id),),
        ).fetchone()
        if row is None:
            raise KeyError(f"face_observation 不存在，id={face_observation_id}")
        return bool(int(row[0]))

    def count_active_samples(
        self,
        *,
        person_ids: list[int],
        conn: sqlite3.Connection,
    ) -> dict[int, int]:
        if not person_ids:
            return {}
        placeholders = ", ".join("?" for _ in person_ids)
        rows = conn.execute(
            f"""
            SELECT person_id, COUNT(*)
            FROM person_face_assignment
            WHERE active=1
              AND person_id IN ({placeholders})
            GROUP BY person_id
            """,
            tuple(int(person_id) for person_id in person_ids),
        ).fetchall()
        counts = {int(person_id): 0 for person_id in person_ids}
        for person_id, sample_count in rows:
            counts[int(person_id)] = int(sample_count)
        return counts

    def update_person_merge_status(
        self,
        *,
        person_id: int,
        status: str,
        merged_into_person_id: int | None,
        conn: sqlite3.Connection,
    ) -> PersonRecord:
        conn.execute(
            """
            UPDATE person
            SET status=?,
                merged_into_person_id=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (str(status), merged_into_person_id, int(person_id)),
        )
        return self.get_person(person_id, conn=conn)  # type: ignore[return-value]

    def create_merge_operation(
        self,
        *,
        selected_person_ids: list[int],
        winner_person_id: int,
        winner_person_uuid: str,
        conn: sqlite3.Connection,
    ) -> int:
        cursor = conn.execute(
            """
            INSERT INTO merge_operation(
              selected_person_ids_json, winner_person_id, winner_person_uuid, status, created_at, undone_at
            ) VALUES (?, ?, ?, 'applied', CURRENT_TIMESTAMP, NULL)
            """,
            (
                json.dumps([int(person_id) for person_id in selected_person_ids], ensure_ascii=False),
                int(winner_person_id),
                str(winner_person_uuid),
            ),
        )
        return int(cursor.lastrowid)

    def get_latest_applied_merge_operation(self, *, conn: sqlite3.Connection) -> MergeOperationRecord | None:
        row = conn.execute(
            """
            SELECT id, selected_person_ids_json, winner_person_id, winner_person_uuid, status, created_at, undone_at
            FROM merge_operation
            WHERE status='applied'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return _to_merge_operation_record(row)

    def mark_merge_operation_undone(self, *, merge_operation_id: int, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            UPDATE merge_operation
            SET status='undone',
                undone_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (int(merge_operation_id),),
        )

    def insert_person_delta(
        self,
        *,
        merge_operation_id: int,
        person_id: int,
        before_snapshot: dict[str, object],
        after_snapshot: dict[str, object],
        conn: sqlite3.Connection,
    ) -> None:
        conn.execute(
            """
            INSERT INTO merge_operation_person_delta(
              merge_operation_id, person_id, before_snapshot_json, after_snapshot_json
            ) VALUES (?, ?, ?, ?)
            """,
            (
                int(merge_operation_id),
                int(person_id),
                json.dumps(before_snapshot, ensure_ascii=False, sort_keys=True),
                json.dumps(after_snapshot, ensure_ascii=False, sort_keys=True),
            ),
        )

    def insert_assignment_delta(
        self,
        *,
        merge_operation_id: int,
        face_observation_id: int,
        before_snapshot: dict[str, object],
        after_snapshot: dict[str, object],
        conn: sqlite3.Connection,
    ) -> None:
        conn.execute(
            """
            INSERT INTO merge_operation_assignment_delta(
              merge_operation_id, face_observation_id, before_assignment_json, after_assignment_json
            ) VALUES (?, ?, ?, ?)
            """,
            (
                int(merge_operation_id),
                int(face_observation_id),
                json.dumps(before_snapshot, ensure_ascii=False, sort_keys=True),
                json.dumps(after_snapshot, ensure_ascii=False, sort_keys=True),
            ),
        )

    def insert_exclusion_delta(
        self,
        *,
        merge_operation_id: int,
        person_id: int,
        face_observation_id: int,
        before_snapshot: dict[str, object],
        after_snapshot: dict[str, object],
        conn: sqlite3.Connection,
    ) -> None:
        conn.execute(
            """
            INSERT INTO merge_operation_exclusion_delta(
              merge_operation_id, person_id, face_observation_id, before_exclusion_json, after_exclusion_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                int(merge_operation_id),
                int(person_id),
                int(face_observation_id),
                json.dumps(before_snapshot, ensure_ascii=False, sort_keys=True),
                json.dumps(after_snapshot, ensure_ascii=False, sort_keys=True),
            ),
        )

    def list_person_deltas(self, *, merge_operation_id: int, conn: sqlite3.Connection) -> list[tuple[int, dict[str, object], dict[str, object]]]:
        rows = conn.execute(
            """
            SELECT person_id, before_snapshot_json, after_snapshot_json
            FROM merge_operation_person_delta
            WHERE merge_operation_id=?
            ORDER BY id DESC
            """,
            (int(merge_operation_id),),
        ).fetchall()
        return [
            (int(person_id), json.loads(str(before_json)), json.loads(str(after_json)))
            for person_id, before_json, after_json in rows
        ]

    def list_active_cluster_ids_for_people(
        self,
        *,
        person_ids: list[int],
        conn: sqlite3.Connection,
    ) -> dict[int, list[int]]:
        if not person_ids:
            return {}
        placeholders = ", ".join("?" for _ in person_ids)
        rows = conn.execute(
            f"""
            SELECT id, person_id
            FROM face_cluster
            WHERE status='active'
              AND person_id IN ({placeholders})
            ORDER BY id ASC
            """,
            tuple(int(person_id) for person_id in person_ids),
        ).fetchall()
        cluster_ids_by_person = {int(person_id): [] for person_id in person_ids}
        for cluster_id, person_id in rows:
            cluster_ids_by_person[int(person_id)].append(int(cluster_id))
        return cluster_ids_by_person

    def list_active_cluster_snapshots_for_people(
        self,
        *,
        person_ids: list[int],
        conn: sqlite3.Connection,
    ) -> dict[int, list[ClusterSnapshotRecord]]:
        if not person_ids:
            return {}
        placeholders = ", ".join("?" for _ in person_ids)
        cluster_rows = conn.execute(
            f"""
            SELECT id, person_id, status, rebuild_scope, created_assignment_run_id, updated_assignment_run_id
            FROM face_cluster
            WHERE status='active'
              AND person_id IN ({placeholders})
            ORDER BY id ASC
            """,
            tuple(int(person_id) for person_id in person_ids),
        ).fetchall()
        snapshots_by_person = {int(person_id): [] for person_id in person_ids}
        for row in cluster_rows:
            cluster_id = int(row[0])
            snapshots_by_person[int(row[1])].append(
                ClusterSnapshotRecord(
                    cluster_id=cluster_id,
                    person_id=int(row[1]),
                    status=str(row[2]),
                    rebuild_scope=str(row[3]),
                    created_assignment_run_id=int(row[4]),
                    updated_assignment_run_id=int(row[5]),
                    member_face_ids=self._list_cluster_member_face_ids(cluster_id=cluster_id, conn=conn),
                    rep_face_ids=self._list_cluster_rep_face_ids(cluster_id=cluster_id, conn=conn),
                )
            )
        return snapshots_by_person

    def move_clusters_to_person(
        self,
        *,
        cluster_ids: list[int],
        person_id: int,
        conn: sqlite3.Connection,
    ) -> None:
        unique_cluster_ids = sorted({int(cluster_id) for cluster_id in cluster_ids if int(cluster_id) > 0})
        if not unique_cluster_ids:
            return
        placeholders = ", ".join("?" for _ in unique_cluster_ids)
        conn.execute(
            f"""
            UPDATE face_cluster
            SET person_id=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id IN ({placeholders})
            """,
            (int(person_id), *tuple(unique_cluster_ids)),
        )

    def prune_faces_from_active_clusters_for_person(
        self,
        *,
        person_id: int,
        face_observation_ids: list[int],
        conn: sqlite3.Connection,
    ) -> None:
        blocked_face_ids = {int(face_id) for face_id in face_observation_ids if int(face_id) > 0}
        if not blocked_face_ids:
            return
        snapshots_by_person = self.list_active_cluster_snapshots_for_people(person_ids=[person_id], conn=conn)
        for snapshot in snapshots_by_person.get(int(person_id), []):
            filtered_member_face_ids = [
                int(face_id)
                for face_id in snapshot.member_face_ids
                if int(face_id) not in blocked_face_ids
            ]
            filtered_rep_face_ids = [
                int(face_id)
                for face_id in snapshot.rep_face_ids
                if int(face_id) not in blocked_face_ids
            ]
            if filtered_member_face_ids == snapshot.member_face_ids and filtered_rep_face_ids == snapshot.rep_face_ids:
                continue
            if not filtered_rep_face_ids:
                quality_by_face_id = self._load_face_quality_by_id(conn=conn, face_ids=filtered_member_face_ids)
                filtered_rep_face_ids = _pick_rep_faces(filtered_member_face_ids, quality_by_face_id)
            self.restore_cluster_snapshot(
                snapshot=ClusterSnapshotRecord(
                    cluster_id=snapshot.cluster_id,
                    person_id=int(person_id),
                    status=snapshot.status,
                    rebuild_scope=snapshot.rebuild_scope,
                    created_assignment_run_id=snapshot.created_assignment_run_id,
                    updated_assignment_run_id=snapshot.updated_assignment_run_id,
                    member_face_ids=filtered_member_face_ids,
                    rep_face_ids=filtered_rep_face_ids,
                ),
                conn=conn,
            )

    def restore_cluster_snapshot(
        self,
        *,
        snapshot: ClusterSnapshotRecord,
        conn: sqlite3.Connection,
    ) -> None:
        conn.execute(
            """
            UPDATE face_cluster
            SET person_id=?,
                status=?,
                rebuild_scope=?,
                created_assignment_run_id=?,
                updated_assignment_run_id=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (
                int(snapshot.person_id),
                str(snapshot.status),
                str(snapshot.rebuild_scope),
                int(snapshot.created_assignment_run_id),
                int(snapshot.updated_assignment_run_id),
                int(snapshot.cluster_id),
            ),
        )
        conn.execute("DELETE FROM face_cluster_member WHERE face_cluster_id=?", (int(snapshot.cluster_id),))
        for face_id in snapshot.member_face_ids:
            conn.execute(
                """
                INSERT INTO face_cluster_member(face_cluster_id, face_observation_id, assignment_run_id, created_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (int(snapshot.cluster_id), int(face_id), int(snapshot.updated_assignment_run_id)),
            )
        conn.execute("DELETE FROM face_cluster_rep_face WHERE face_cluster_id=?", (int(snapshot.cluster_id),))
        for rank, face_id in enumerate(snapshot.rep_face_ids, start=1):
            conn.execute(
                """
                INSERT INTO face_cluster_rep_face(
                  face_cluster_id, face_observation_id, rep_rank, assignment_run_id, created_at
                ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (int(snapshot.cluster_id), int(face_id), rank, int(snapshot.updated_assignment_run_id)),
            )

    def list_active_exclusions_for_face_person_pairs(
        self,
        *,
        face_person_pairs: list[tuple[int, int]],
        conn: sqlite3.Connection,
    ) -> dict[tuple[int, int], ExclusionRecord]:
        normalized_pairs = sorted(
            {
                (int(face_id), int(person_id))
                for face_id, person_id in face_person_pairs
                if int(face_id) > 0 and int(person_id) > 0
            }
        )
        if not normalized_pairs:
            return {}
        clauses = " OR ".join("(face_observation_id=? AND person_id=?)" for _ in normalized_pairs)
        params: list[int] = []
        for face_id, person_id in normalized_pairs:
            params.extend([int(face_id), int(person_id)])
        rows = conn.execute(
            f"""
            SELECT id, person_id, face_observation_id, reason, active
            FROM person_face_exclusion
            WHERE active=1
              AND ({clauses})
            ORDER BY id ASC
            """,
            tuple(params),
        ).fetchall()
        return {
            (int(record.face_observation_id), int(record.person_id)): record
            for record in (_to_exclusion_record(row) for row in rows)
        }

    def _list_cluster_member_face_ids(self, *, cluster_id: int, conn: sqlite3.Connection) -> list[int]:
        rows = conn.execute(
            """
            SELECT face_observation_id
            FROM face_cluster_member
            WHERE face_cluster_id=?
            ORDER BY face_observation_id ASC
            """,
            (int(cluster_id),),
        ).fetchall()
        return [int(row[0]) for row in rows]

    def _list_cluster_rep_face_ids(self, *, cluster_id: int, conn: sqlite3.Connection) -> list[int]:
        rows = conn.execute(
            """
            SELECT face_observation_id
            FROM face_cluster_rep_face
            WHERE face_cluster_id=?
            ORDER BY rep_rank ASC, face_observation_id ASC
            """,
            (int(cluster_id),),
        ).fetchall()
        return [int(row[0]) for row in rows]

    def _load_face_quality_by_id(self, *, conn: sqlite3.Connection, face_ids: list[int]) -> dict[int, float]:
        unique_ids = sorted({int(face_id) for face_id in face_ids if int(face_id) > 0})
        if not unique_ids:
            return {}
        placeholders = ", ".join("?" for _ in unique_ids)
        rows = conn.execute(
            f"SELECT id, quality_score FROM face_observation WHERE id IN ({placeholders})",
            tuple(unique_ids),
        ).fetchall()
        return {int(row[0]): float(row[1] or 0.0) for row in rows}

    def list_assignment_deltas(self, *, merge_operation_id: int, conn: sqlite3.Connection) -> list[DeltaRecord]:
        rows = conn.execute(
            """
            SELECT face_observation_id, before_assignment_json, after_assignment_json
            FROM merge_operation_assignment_delta
            WHERE merge_operation_id=?
            ORDER BY id DESC
            """,
            (int(merge_operation_id),),
        ).fetchall()
        return [
            DeltaRecord(
                person_id=int(json.loads(str(before_json)).get("person_id") or json.loads(str(after_json)).get("person_id") or 0),
                face_observation_id=int(face_observation_id),
                before_payload=json.loads(str(before_json)),
                after_payload=json.loads(str(after_json)),
            )
            for face_observation_id, before_json, after_json in rows
        ]

    def list_exclusion_deltas(self, *, merge_operation_id: int, conn: sqlite3.Connection) -> list[DeltaRecord]:
        rows = conn.execute(
            """
            SELECT person_id, face_observation_id, before_exclusion_json, after_exclusion_json
            FROM merge_operation_exclusion_delta
            WHERE merge_operation_id=?
            ORDER BY id DESC
            """,
            (int(merge_operation_id),),
        ).fetchall()
        return [
            DeltaRecord(
                person_id=int(person_id),
                face_observation_id=int(face_observation_id),
                before_payload=json.loads(str(before_json)),
                after_payload=json.loads(str(after_json)),
            )
            for person_id, face_observation_id, before_json, after_json in rows
        ]


def _to_person_record(row: sqlite3.Row | tuple[object, ...]) -> PersonRecord:
    merged_into = row[5]
    return PersonRecord(
        id=int(row[0]),
        person_uuid=str(row[1]),
        display_name=None if row[2] is None else str(row[2]),
        is_named=bool(int(row[3])),
        status=str(row[4]),
        merged_into_person_id=None if merged_into is None else int(merged_into),
        created_at=str(row[6]),
        updated_at=str(row[7]),
    )


def _to_assignment_record(row: sqlite3.Row | tuple[object, ...]) -> AssignmentRecord:
    return AssignmentRecord(
        id=int(row[0]),
        person_id=int(row[1]),
        face_observation_id=int(row[2]),
        assignment_run_id=int(row[3]),
        assignment_source=str(row[4]),
        active=bool(int(row[5])),
        confidence=None if row[6] is None else float(row[6]),
        margin=None if row[7] is None else float(row[7]),
    )


def _to_exclusion_record(row: sqlite3.Row | tuple[object, ...]) -> ExclusionRecord:
    return ExclusionRecord(
        id=int(row[0]),
        person_id=int(row[1]),
        face_observation_id=int(row[2]),
        reason=str(row[3]),
        active=bool(int(row[4])),
    )


def _to_merge_operation_record(row: sqlite3.Row | tuple[object, ...]) -> MergeOperationRecord:
    return MergeOperationRecord(
        id=int(row[0]),
        selected_person_ids=[int(person_id) for person_id in json.loads(str(row[1]))],
        winner_person_id=int(row[2]),
        winner_person_uuid=str(row[3]),
        status=str(row[4]),
        created_at=str(row[5]),
        undone_at=None if row[6] is None else str(row[6]),
    )


def _pick_rep_faces(face_ids: list[int], face_quality_by_id: dict[int, float], *, top_k: int = 3) -> list[int]:
    unique_ids = sorted({int(face_id) for face_id in face_ids if int(face_id) > 0})
    ranked = sorted(
        unique_ids,
        key=lambda face_id: (-float(face_quality_by_id.get(face_id, 0.0)), face_id),
    )
    return ranked[:top_k]
