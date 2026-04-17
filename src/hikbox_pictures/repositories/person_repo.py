from __future__ import annotations

from typing import Any

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]


class PersonRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._table_exists_cache: dict[str, bool] = {}
        self._person_columns_cache: set[str] | None = None

    def _has_table(self, table: str) -> bool:
        cached = self._table_exists_cache.get(table)
        if cached is not None:
            return cached
        row = self.conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table'
              AND name = ?
            LIMIT 1
            """,
            (str(table),),
        ).fetchone()
        exists = row is not None
        self._table_exists_cache[table] = exists
        return exists

    def _person_columns(self) -> set[str]:
        if self._person_columns_cache is None:
            rows = self.conn.execute("PRAGMA table_info(person)").fetchall()
            self._person_columns_cache = {str(row["name"]) for row in rows}
        return self._person_columns_cache

    def _has_person_column(self, column: str) -> bool:
        return str(column) in self._person_columns()

    def _person_origin_cluster_expr(self, person_alias: str) -> str:
        alias = str(person_alias)
        if self._has_person_column("origin_cluster_id"):
            return f"{alias}.origin_cluster_id"
        if self._has_table("person_cluster_origin"):
            return (
                "COALESCE(\n"
                "    (\n"
                "        SELECT pco.origin_cluster_id\n"
                "        FROM person_cluster_origin AS pco\n"
                f"        WHERE pco.person_id = {alias}.id\n"
                "          AND pco.active = 1\n"
                "        ORDER BY pco.id DESC\n"
                "        LIMIT 1\n"
                "    ),\n"
                "    (\n"
                "        SELECT pts.source_auto_cluster_id\n"
                "        FROM person_trusted_sample AS pts\n"
                f"        WHERE pts.person_id = {alias}.id\n"
                "          AND pts.active = 1\n"
                "          AND pts.source_auto_cluster_id IS NOT NULL\n"
                "        ORDER BY pts.id DESC\n"
                "        LIMIT 1\n"
                "    )\n"
                ")"
            )
        return "NULL"

    def _resolve_identity_cluster_run_id(self, *, origin_cluster_id: int) -> int | None:
        if not self._has_table("identity_cluster"):
            return None
        cluster_row = self.conn.execute(
            """
            SELECT run_id
            FROM identity_cluster
            WHERE id = ?
            LIMIT 1
            """,
            (int(origin_cluster_id),),
        ).fetchone()
        if cluster_row is None:
            return None
        run_id = cluster_row["run_id"]
        if run_id is None:
            return None
        return int(run_id)

    def _insert_person_cluster_origin_if_possible(
        self,
        *,
        person_id: int,
        origin_cluster_id: int | None,
    ) -> None:
        if origin_cluster_id is None:
            return
        if not self._has_table("person_cluster_origin"):
            return
        source_run_id = self._resolve_identity_cluster_run_id(
            origin_cluster_id=int(origin_cluster_id),
        )
        if source_run_id is None:
            return
        self.conn.execute(
            """
            INSERT INTO person_cluster_origin(
                person_id,
                origin_cluster_id,
                source_run_id,
                origin_kind,
                active
            )
            VALUES (?, ?, ?, 'bootstrap_materialize', 1)
            """,
            (
                int(person_id),
                int(origin_cluster_id),
                int(source_run_id),
            ),
        )

    def create_person(
        self,
        display_name: str,
        status: str = "active",
        confirmed: bool = False,
        ignored: bool = False,
        notes: str | None = None,
        cover_observation_id: int | None = None,
        origin_cluster_id: int | None = None,
    ) -> int:
        if self._has_person_column("origin_cluster_id"):
            cursor = self.conn.execute(
                """
                INSERT INTO person(
                    display_name,
                    status,
                    confirmed,
                    ignored,
                    notes,
                    cover_observation_id,
                    origin_cluster_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    display_name,
                    status,
                    1 if confirmed else 0,
                    1 if ignored else 0,
                    notes,
                    int(cover_observation_id) if cover_observation_id is not None else None,
                    int(origin_cluster_id) if origin_cluster_id is not None else None,
                ),
            )
            return int(cursor.lastrowid)

        cursor = self.conn.execute(
            """
            INSERT INTO person(
                display_name,
                status,
                confirmed,
                ignored,
                notes,
                cover_observation_id
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                display_name,
                status,
                1 if confirmed else 0,
                1 if ignored else 0,
                notes,
                int(cover_observation_id) if cover_observation_id is not None else None,
            ),
        )
        person_id = int(cursor.lastrowid)
        self._insert_person_cluster_origin_if_possible(
            person_id=person_id,
            origin_cluster_id=origin_cluster_id,
        )
        return person_id

    def get_person(self, person_id: int) -> dict[str, Any] | None:
        origin_expr = self._person_origin_cluster_expr("p")
        row = self.conn.execute(
            f"""
            SELECT p.id,
                   p.display_name,
                   p.cover_observation_id,
                   {origin_expr} AS origin_cluster_id,
                   p.status,
                   p.confirmed,
                   p.ignored,
                   p.notes,
                   p.merged_into_person_id,
                   p.created_at,
                   p.updated_at
            FROM person AS p
            WHERE p.id = ?
            """,
            (int(person_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def list_people(self) -> list[dict[str, Any]]:
        origin_expr = self._person_origin_cluster_expr("p")
        rows = self.conn.execute(
            f"""
            SELECT p.id,
                   p.display_name,
                   p.cover_observation_id,
                   {origin_expr} AS origin_cluster_id,
                   p.status,
                   p.confirmed,
                   p.ignored,
                   p.notes,
                   p.merged_into_person_id,
                   p.created_at,
                   p.updated_at
            FROM person AS p
            ORDER BY p.id ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def set_cover_observation(self, *, person_id: int, cover_observation_id: int | None) -> int:
        cursor = self.conn.execute(
            """
            UPDATE person
            SET cover_observation_id = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                int(cover_observation_id) if cover_observation_id is not None else None,
                int(person_id),
            ),
        )
        return int(cursor.rowcount)

    def create_anonymous_person(self, *, origin_cluster_id: int | None, sequence: int) -> int:
        return self.create_person(
            display_name=f"未命名人物-{int(sequence)}",
            status="active",
            confirmed=False,
            ignored=False,
            notes=None,
            origin_cluster_id=origin_cluster_id,
        )

    def next_anonymous_sequence(self) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM person
            WHERE display_name LIKE '未命名人物-%'
            """
        ).fetchone()
        return int(row["c"]) + 1

    def list_active_person_ids(self) -> list[int]:
        rows = self.conn.execute(
            """
            SELECT id
            FROM person
            WHERE status = 'active'
              AND ignored = 0
            ORDER BY id ASC
            """
        ).fetchall()
        return [int(row["id"]) for row in rows]

    def list_active_prototypes(
        self,
        *,
        prototype_type: str | None = None,
        model_key: str | None = None,
        person_id: int | None = None,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT pp.id,
                   pp.person_id,
                   pp.prototype_type,
                   pp.source_observation_id,
                   pp.model_key,
                   pp.vector_blob,
                   pp.quality_score,
                   pp.active,
                   pp.updated_at
            FROM person_prototype AS pp
            JOIN person AS p
              ON p.id = pp.person_id
            WHERE pp.active = 1
              AND p.status = 'active'
              AND p.ignored = 0
        """
        params: tuple[Any, ...]
        params_list: list[Any] = []
        if prototype_type is not None:
            sql += " AND pp.prototype_type = ?"
            params_list.append(str(prototype_type))
        if model_key is not None:
            sql += " AND pp.model_key = ?"
            params_list.append(str(model_key))
        if person_id is not None:
            sql += " AND pp.person_id = ?"
            params_list.append(int(person_id))
        params = tuple(params_list)
        sql += " ORDER BY pp.person_id ASC, pp.id ASC"
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def deactivate_active_centroid_prototypes(self, *, person_id: int, model_key: str | None = None) -> int:
        sql = """
            UPDATE person_prototype
            SET active = 0,
                updated_at = CURRENT_TIMESTAMP
            WHERE person_id = ?
              AND prototype_type = 'centroid'
              AND active = 1
        """
        params: list[Any] = [int(person_id)]
        if model_key is not None:
            sql += " AND model_key = ?"
            params.append(str(model_key))
        cursor = self.conn.execute(sql, tuple(params))
        return int(cursor.rowcount)

    def replace_centroid_prototype(
        self,
        *,
        person_id: int,
        vector_blob: bytes,
        model_key: str = "pipeline-stub-v1",
        quality_score: float | None = None,
        source_observation_id: int | None = None,
    ) -> int:
        self.deactivate_active_centroid_prototypes(
            person_id=int(person_id),
            model_key=model_key,
        )
        cursor = self.conn.execute(
            """
            INSERT INTO person_prototype(
                person_id,
                prototype_type,
                source_observation_id,
                model_key,
                vector_blob,
                quality_score,
                active,
                updated_at
            )
            VALUES (?, 'centroid', ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
            """,
            (
                int(person_id),
                int(source_observation_id) if source_observation_id is not None else None,
                model_key,
                vector_blob,
                quality_score,
            ),
        )
        return int(cursor.lastrowid)

    def create_bootstrap_assignment(
        self,
        *,
        person_id: int,
        face_observation_id: int,
        threshold_profile_id: int,
        diagnostic_json: str,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO person_face_assignment(
                person_id,
                face_observation_id,
                assignment_source,
                diagnostic_json,
                threshold_profile_id,
                locked,
                active
            )
            VALUES (?, ?, 'bootstrap', ?, ?, 0, 1)
            """,
            (
                int(person_id),
                int(face_observation_id),
                str(diagnostic_json),
                int(threshold_profile_id),
            ),
        )
        return int(cursor.lastrowid)

    def create_trusted_sample(
        self,
        *,
        person_id: int,
        face_observation_id: int,
        trust_source: str,
        trust_score: float,
        quality_score_snapshot: float,
        threshold_profile_id: int,
        source_auto_cluster_id: int | None,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO person_trusted_sample(
                person_id,
                face_observation_id,
                trust_source,
                trust_score,
                quality_score_snapshot,
                threshold_profile_id,
                source_auto_cluster_id,
                active,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
            """,
            (
                int(person_id),
                int(face_observation_id),
                str(trust_source),
                float(trust_score),
                float(quality_score_snapshot),
                int(threshold_profile_id),
                int(source_auto_cluster_id) if source_auto_cluster_id is not None else None,
            ),
        )
        return int(cursor.lastrowid)

    def mark_merged(self, source_person_id: int, target_person_id: int) -> int:
        cursor = self.conn.execute(
            """
            UPDATE person
            SET status = 'merged',
                merged_into_person_id = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND status = 'active'
            """,
            (int(target_person_id), int(source_person_id)),
        )
        return int(cursor.rowcount)

    def count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS c FROM person").fetchone()
        return int(row["c"])
