"""持久 cluster 真相层仓储。"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from hikbox_pictures.product.db.connection import connect_sqlite


@dataclass(frozen=True)
class ClusterRecord:
    id: int
    cluster_uuid: str
    person_id: int
    status: str
    rebuild_scope: str
    created_assignment_run_id: int
    updated_assignment_run_id: int
    member_count: int
    representative_count: int


@dataclass(frozen=True)
class ClusterMemberRecord:
    face_cluster_id: int
    face_observation_id: int
    assignment_run_id: int


@dataclass(frozen=True)
class ClusterRepFaceRecord:
    face_cluster_id: int
    face_observation_id: int
    rep_rank: int
    assignment_run_id: int


class ClusterRepository:
    """管理持久 cluster、成员与 representative face。"""

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        return connect_sqlite(self._db_path)

    def has_active_clusters(self, *, conn: sqlite3.Connection | None = None) -> bool:
        row = self._fetchone(
            "SELECT 1 FROM face_cluster WHERE status='active' LIMIT 1",
            (),
            conn=conn,
        )
        return row is not None

    def list_active_clusters(self, *, conn: sqlite3.Connection | None = None) -> list[ClusterRecord]:
        rows = self._fetchall(
            """
            SELECT
              c.id,
              c.cluster_uuid,
              c.person_id,
              c.status,
              c.rebuild_scope,
              c.created_assignment_run_id,
              c.updated_assignment_run_id,
              COUNT(DISTINCT m.face_observation_id) AS member_count,
              COUNT(DISTINCT r.face_observation_id) AS representative_count
            FROM face_cluster AS c
            LEFT JOIN face_cluster_member AS m ON m.face_cluster_id = c.id
            LEFT JOIN face_cluster_rep_face AS r ON r.face_cluster_id = c.id
            WHERE c.status='active'
            GROUP BY
              c.id,
              c.cluster_uuid,
              c.person_id,
              c.status,
              c.rebuild_scope,
              c.created_assignment_run_id,
              c.updated_assignment_run_id
            ORDER BY c.id ASC
            """,
            (),
            conn=conn,
        )
        return [
            ClusterRecord(
                id=int(row[0]),
                cluster_uuid=str(row[1]),
                person_id=int(row[2]),
                status=str(row[3]),
                rebuild_scope=str(row[4]),
                created_assignment_run_id=int(row[5]),
                updated_assignment_run_id=int(row[6]),
                member_count=int(row[7] or 0),
                representative_count=int(row[8] or 0),
            )
            for row in rows
        ]

    def list_cluster_members(self, cluster_id: int, *, conn: sqlite3.Connection | None = None) -> list[ClusterMemberRecord]:
        rows = self._fetchall(
            """
            SELECT face_cluster_id, face_observation_id, assignment_run_id
            FROM face_cluster_member
            WHERE face_cluster_id=?
            ORDER BY face_observation_id ASC
            """,
            (int(cluster_id),),
            conn=conn,
        )
        return [
            ClusterMemberRecord(
                face_cluster_id=int(row[0]),
                face_observation_id=int(row[1]),
                assignment_run_id=int(row[2]),
            )
            for row in rows
        ]

    def list_cluster_rep_faces(self, cluster_id: int, *, conn: sqlite3.Connection | None = None) -> list[ClusterRepFaceRecord]:
        rows = self._fetchall(
            """
            SELECT face_cluster_id, face_observation_id, rep_rank, assignment_run_id
            FROM face_cluster_rep_face
            WHERE face_cluster_id=?
            ORDER BY rep_rank ASC, face_observation_id ASC
            """,
            (int(cluster_id),),
            conn=conn,
        )
        raw_records = [
            ClusterRepFaceRecord(
                face_cluster_id=int(row[0]),
                face_observation_id=int(row[1]),
                rep_rank=int(row[2]),
                assignment_run_id=int(row[3]),
            )
            for row in rows
        ]
        if not raw_records:
            return raw_records
        active_rep_ids = set(
            self._filter_active_face_ids(
                conn=conn,
                face_ids=[item.face_observation_id for item in raw_records],
            )
        )
        filtered_records = [item for item in raw_records if item.face_observation_id in active_rep_ids]
        if filtered_records:
            return filtered_records

        active_member_ids = self._filter_active_face_ids(
            conn=conn,
            face_ids=[item.face_observation_id for item in self.list_cluster_members(cluster_id, conn=conn)],
        )
        if not active_member_ids:
            return []
        quality_by_id = self._load_face_quality_by_id(conn=conn, face_ids=active_member_ids)
        assignment_run_id = raw_records[0].assignment_run_id
        return [
            ClusterRepFaceRecord(
                face_cluster_id=int(cluster_id),
                face_observation_id=int(face_id),
                rep_rank=rank,
                assignment_run_id=int(assignment_run_id),
            )
            for rank, face_id in enumerate(self._pick_rep_faces(active_member_ids, quality_by_id), start=1)
        ]

    def find_active_cluster_by_person(self, person_id: int, *, conn: sqlite3.Connection | None = None) -> ClusterRecord | None:
        row = self._fetchone(
            """
            SELECT id, cluster_uuid, person_id, status, rebuild_scope, created_assignment_run_id, updated_assignment_run_id
            FROM face_cluster
            WHERE person_id=? AND status='active'
            ORDER BY id ASC
            LIMIT 1
            """,
            (int(person_id),),
            conn=conn,
        )
        if row is None:
            return None
        cluster_id = int(row[0])
        member_count = len(self.list_cluster_members(cluster_id, conn=conn))
        rep_count = len(self.list_cluster_rep_faces(cluster_id, conn=conn))
        return ClusterRecord(
            id=cluster_id,
            cluster_uuid=str(row[1]),
            person_id=int(row[2]),
            status=str(row[3]),
            rebuild_scope=str(row[4]),
            created_assignment_run_id=int(row[5]),
            updated_assignment_run_id=int(row[6]),
            member_count=member_count,
            representative_count=rep_count,
        )

    def replace_all_clusters(
        self,
        *,
        assignment_run_id: int,
        cluster_rows: list[dict[str, object]],
        person_id_by_temp_key: dict[str, int],
        face_quality_by_id: dict[int, float],
        conn: sqlite3.Connection,
        rebuild_scope: str,
    ) -> None:
        existing_clusters = self._load_active_cluster_snapshots(conn=conn)
        matched_cluster_ids: set[int] = set()
        for cluster_row in cluster_rows:
            person_temp_key = str(cluster_row.get("person_temp_key") or "")
            person_id = int(person_id_by_temp_key.get(person_temp_key, 0))
            member_face_ids = sorted(
                {
                    int(value)
                    for value in cluster_row.get("member_face_observation_ids", [])
                    if int(value) > 0
                }
            )
            valid_member_face_ids = self._filter_active_face_ids(conn=conn, face_ids=member_face_ids)
            reused_cluster_id = 0
            snapshot_key = (person_id, tuple(valid_member_face_ids))
            existing_candidates = existing_clusters.get(snapshot_key, [])
            while existing_candidates:
                candidate_cluster_id = int(existing_candidates.pop(0))
                if candidate_cluster_id not in matched_cluster_ids:
                    reused_cluster_id = candidate_cluster_id
                    matched_cluster_ids.add(reused_cluster_id)
                    break
            if reused_cluster_id > 0:
                self._refresh_cluster(
                    conn=conn,
                    cluster_id=reused_cluster_id,
                    assignment_run_id=assignment_run_id,
                    cluster_row=cluster_row,
                    face_quality_by_id=face_quality_by_id,
                    rebuild_scope=rebuild_scope,
                )
                continue
            self._insert_cluster(
                conn=conn,
                assignment_run_id=assignment_run_id,
                cluster_row=cluster_row,
                person_id_by_temp_key=person_id_by_temp_key,
                face_quality_by_id=face_quality_by_id,
                rebuild_scope=rebuild_scope,
            )
        stale_cluster_ids = [
            cluster_id
            for cluster_ids in existing_clusters.values()
            for cluster_id in cluster_ids
            if cluster_id not in matched_cluster_ids
        ]
        if stale_cluster_ids:
            placeholders = ", ".join("?" for _ in stale_cluster_ids)
            conn.execute(
                f"""
                UPDATE face_cluster
                SET status='replaced',
                    updated_assignment_run_id=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id IN ({placeholders})
                """,
                (int(assignment_run_id), *tuple(int(cluster_id) for cluster_id in stale_cluster_ids)),
            )

    def append_face_to_cluster(
        self,
        *,
        cluster_id: int,
        assignment_run_id: int,
        face_observation_id: int,
        face_quality_by_id: dict[int, float],
        conn: sqlite3.Connection,
    ) -> None:
        conn.execute(
            """
            INSERT INTO face_cluster_member(face_cluster_id, face_observation_id, assignment_run_id, created_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(face_cluster_id, face_observation_id) DO NOTHING
            """,
            (int(cluster_id), int(face_observation_id), int(assignment_run_id)),
        )
        all_face_ids = [item.face_observation_id for item in self.list_cluster_members(cluster_id, conn=conn)]
        active_face_ids = self._filter_active_face_ids(conn=conn, face_ids=all_face_ids)
        all_face_quality_by_id = self._load_face_quality_by_id(conn=conn, face_ids=all_face_ids)
        all_face_quality_by_id.update(
            {
                int(face_id): float(score)
                for face_id, score in face_quality_by_id.items()
                if int(face_id) > 0
            }
        )
        self._replace_rep_faces(
            conn=conn,
            cluster_id=cluster_id,
            assignment_run_id=assignment_run_id,
            face_ids=active_face_ids,
            face_quality_by_id=all_face_quality_by_id,
        )
        conn.execute(
            """
            UPDATE face_cluster
            SET updated_assignment_run_id=?,
                rebuild_scope='local',
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (int(assignment_run_id), int(cluster_id)),
        )

    def create_cluster_for_person(
        self,
        *,
        person_id: int,
        assignment_run_id: int,
        member_face_ids: list[int],
        representative_face_ids: list[int] | None,
        face_quality_by_id: dict[int, float],
        conn: sqlite3.Connection,
        rebuild_scope: str = "local",
    ) -> int:
        cursor = conn.execute(
            """
            INSERT INTO face_cluster(
              cluster_uuid, person_id, status, rebuild_scope,
              created_assignment_run_id, updated_assignment_run_id, created_at, updated_at
            ) VALUES (?, ?, 'active', ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                str(uuid.uuid4()),
                int(person_id),
                str(rebuild_scope),
                int(assignment_run_id),
                int(assignment_run_id),
            ),
        )
        cluster_id = int(cursor.lastrowid)
        cluster_row = {
            "member_face_observation_ids": sorted(
                {int(face_id) for face_id in member_face_ids if int(face_id) > 0}
            ),
            "representative_face_observation_ids": [] if representative_face_ids is None else sorted(
                {int(face_id) for face_id in representative_face_ids if int(face_id) > 0}
            ),
        }
        self._refresh_cluster(
            conn=conn,
            cluster_id=cluster_id,
            assignment_run_id=assignment_run_id,
            cluster_row=cluster_row,
            face_quality_by_id=face_quality_by_id,
            rebuild_scope=rebuild_scope,
        )
        return cluster_id

    def _insert_cluster(
        self,
        *,
        conn: sqlite3.Connection,
        assignment_run_id: int,
        cluster_row: dict[str, object],
        person_id_by_temp_key: dict[str, int],
        face_quality_by_id: dict[int, float],
        rebuild_scope: str,
    ) -> int:
        person_temp_key = str(cluster_row.get("person_temp_key") or "")
        person_id = int(person_id_by_temp_key.get(person_temp_key, 0))
        if person_id <= 0:
            raise ValueError(f"cluster 未找到对应人物: {person_temp_key}")
        cursor = conn.execute(
            """
            INSERT INTO face_cluster(
              cluster_uuid, person_id, status, rebuild_scope,
              created_assignment_run_id, updated_assignment_run_id, created_at, updated_at
            ) VALUES (?, ?, 'active', ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                str(uuid.uuid4()),
                person_id,
                str(rebuild_scope),
                int(assignment_run_id),
                int(assignment_run_id),
            ),
        )
        cluster_id = int(cursor.lastrowid)
        self._refresh_cluster(
            conn=conn,
            cluster_id=cluster_id,
            assignment_run_id=assignment_run_id,
            cluster_row=cluster_row,
            face_quality_by_id=face_quality_by_id,
            rebuild_scope=rebuild_scope,
        )
        return cluster_id

    def _refresh_cluster(
        self,
        *,
        conn: sqlite3.Connection,
        cluster_id: int,
        assignment_run_id: int,
        cluster_row: dict[str, object],
        face_quality_by_id: dict[int, float],
        rebuild_scope: str,
    ) -> None:
        conn.execute(
            """
            UPDATE face_cluster
            SET status='active',
                rebuild_scope=?,
                updated_assignment_run_id=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (str(rebuild_scope), int(assignment_run_id), int(cluster_id)),
        )
        conn.execute("DELETE FROM face_cluster_member WHERE face_cluster_id=?", (int(cluster_id),))
        member_face_ids = sorted(
            {int(value) for value in cluster_row.get("member_face_observation_ids", []) if int(value) > 0}
        )
        valid_member_face_ids = self._filter_active_face_ids(conn=conn, face_ids=member_face_ids)
        for face_id in valid_member_face_ids:
            conn.execute(
                """
                INSERT INTO face_cluster_member(face_cluster_id, face_observation_id, assignment_run_id, created_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (cluster_id, face_id, int(assignment_run_id)),
            )
        rep_face_ids = [
            int(value)
            for value in cluster_row.get("representative_face_observation_ids", [])
            if int(value) > 0
        ]
        if not rep_face_ids:
            rep_face_ids = self._pick_rep_faces(valid_member_face_ids, face_quality_by_id)
        self._replace_rep_faces(
            conn=conn,
            cluster_id=cluster_id,
            assignment_run_id=assignment_run_id,
            face_ids=rep_face_ids,
            face_quality_by_id=face_quality_by_id,
        )

    def _replace_rep_faces(
        self,
        *,
        conn: sqlite3.Connection,
        cluster_id: int,
        assignment_run_id: int,
        face_ids: list[int],
        face_quality_by_id: dict[int, float],
    ) -> None:
        conn.execute("DELETE FROM face_cluster_rep_face WHERE face_cluster_id=?", (int(cluster_id),))
        valid_face_ids = self._filter_active_face_ids(conn=conn, face_ids=face_ids)
        for rank, face_id in enumerate(self._pick_rep_faces(valid_face_ids, face_quality_by_id), start=1):
            conn.execute(
                """
                INSERT INTO face_cluster_rep_face(
                  face_cluster_id, face_observation_id, rep_rank, assignment_run_id, created_at
                ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (int(cluster_id), int(face_id), rank, int(assignment_run_id)),
            )

    def _pick_rep_faces(self, face_ids: list[int], face_quality_by_id: dict[int, float], *, top_k: int = 3) -> list[int]:
        unique_ids = sorted({int(face_id) for face_id in face_ids if int(face_id) > 0})
        ranked = sorted(
            unique_ids,
            key=lambda face_id: (-float(face_quality_by_id.get(face_id, 0.0)), face_id),
        )
        return ranked[:top_k]

    def _filter_existing_face_ids(self, *, conn: sqlite3.Connection, face_ids: list[int]) -> list[int]:
        unique_ids = sorted({int(face_id) for face_id in face_ids if int(face_id) > 0})
        if not unique_ids:
            return []
        placeholders = ", ".join("?" for _ in unique_ids)
        rows = conn.execute(
            f"SELECT id FROM face_observation WHERE id IN ({placeholders})",
            tuple(unique_ids),
        ).fetchall()
        return [int(row[0]) for row in rows]

    def _filter_active_face_ids(self, *, conn: sqlite3.Connection | None, face_ids: list[int]) -> list[int]:
        unique_ids = sorted({int(face_id) for face_id in face_ids if int(face_id) > 0})
        if not unique_ids:
            return []
        placeholders = ", ".join("?" for _ in unique_ids)
        managed_conn = conn is None
        db = conn or self.connect()
        try:
            rows = db.execute(
                f"""
                SELECT f.id
                FROM face_observation AS f
                INNER JOIN photo_asset AS p ON p.id = f.photo_asset_id
                WHERE f.id IN ({placeholders})
                  AND f.active = 1
                  AND p.asset_status = 'active'
                ORDER BY f.id ASC
                """,
                tuple(unique_ids),
            ).fetchall()
            return [int(row[0]) for row in rows]
        finally:
            if managed_conn:
                db.close()

    def _load_face_quality_by_id(self, *, conn: sqlite3.Connection, face_ids: list[int]) -> dict[int, float]:
        unique_ids = sorted({int(face_id) for face_id in face_ids if int(face_id) > 0})
        if not unique_ids:
            return {}
        placeholders = ", ".join("?" for _ in unique_ids)
        rows = conn.execute(
            f"SELECT id, quality_score FROM face_observation WHERE id IN ({placeholders})",
            tuple(unique_ids),
        ).fetchall()
        return {
            int(row[0]): float(row[1] or 0.0)
            for row in rows
        }

    def _fetchall(
        self,
        sql: str,
        params: tuple[object, ...],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> list[sqlite3.Row | tuple[object, ...]]:
        managed_conn = conn is None
        db = conn or self.connect()
        try:
            return db.execute(sql, params).fetchall()
        finally:
            if managed_conn:
                db.close()

    def _load_active_cluster_snapshots(self, *, conn: sqlite3.Connection) -> dict[tuple[int, tuple[int, ...]], list[int]]:
        rows = conn.execute(
            """
            SELECT id, person_id
            FROM face_cluster
            WHERE status='active'
            ORDER BY id ASC
            """
        ).fetchall()
        snapshots: dict[tuple[int, tuple[int, ...]], list[int]] = {}
        for row in rows:
            cluster_id = int(row[0])
            person_id = int(row[1])
            member_ids = tuple(
                sorted(
                    self._filter_active_face_ids(
                        conn=conn,
                        face_ids=[item.face_observation_id for item in self.list_cluster_members(cluster_id, conn=conn)],
                    )
                )
            )
            snapshots.setdefault((person_id, member_ids), []).append(cluster_id)
        return snapshots

    def _fetchone(
        self,
        sql: str,
        params: tuple[object, ...],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> sqlite3.Row | tuple[object, ...] | None:
        managed_conn = conn is None
        db = conn or self.connect()
        try:
            return db.execute(sql, params).fetchone()
        finally:
            if managed_conn:
                db.close()
