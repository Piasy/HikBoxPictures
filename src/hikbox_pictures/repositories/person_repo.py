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
        source_run_id: int | None = None,
        source_cluster_id: int | None = None,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO person_face_assignment(
                person_id,
                face_observation_id,
                assignment_source,
                diagnostic_json,
                threshold_profile_id,
                source_run_id,
                source_cluster_id,
                locked,
                active
            )
            VALUES (?, ?, 'bootstrap', ?, ?, ?, ?, 0, 1)
            """,
            (
                int(person_id),
                int(face_observation_id),
                str(diagnostic_json),
                int(threshold_profile_id),
                int(source_run_id) if source_run_id is not None else None,
                int(source_cluster_id) if source_cluster_id is not None else None,
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
        source_run_id: int | None = None,
        source_cluster_id: int | None = None,
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
                source_run_id,
                source_cluster_id,
                active,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
            """,
            (
                int(person_id),
                int(face_observation_id),
                str(trust_source),
                float(trust_score),
                float(quality_score_snapshot),
                int(threshold_profile_id),
                int(source_auto_cluster_id) if source_auto_cluster_id is not None else None,
                int(source_run_id) if source_run_id is not None else None,
                int(source_cluster_id) if source_cluster_id is not None else None,
            ),
        )
        return int(cursor.lastrowid)

    def create_person_cluster_origin(
        self,
        *,
        person_id: int,
        origin_cluster_id: int,
        source_run_id: int,
        origin_kind: str = "bootstrap_materialize",
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO person_cluster_origin(
                person_id,
                origin_cluster_id,
                source_run_id,
                origin_kind,
                active
            )
            VALUES (?, ?, ?, ?, 1)
            """,
            (
                int(person_id),
                int(origin_cluster_id),
                int(source_run_id),
                str(origin_kind),
            ),
        )
        return int(cursor.lastrowid)

    def apply_person_publish_plan(
        self,
        *,
        person_id: int,
        publish_plan: dict[str, Any],
        source_run_id: int,
        source_cluster_id: int,
    ) -> None:
        cover_observation_id = publish_plan.get("cover_observation_id")
        threshold_profile_id = int(publish_plan.get("threshold_profile_id") or 0)
        if threshold_profile_id <= 0:
            raise ValueError("publish_plan 缺少 threshold_profile_id")
        assignments = publish_plan.get("assignments") or []
        trusted_seeds = publish_plan.get("trusted_seeds") or []
        self.create_person_cluster_origin(
            person_id=int(person_id),
            origin_cluster_id=int(source_cluster_id),
            source_run_id=int(source_run_id),
            origin_kind="bootstrap_materialize",
        )
        self.set_cover_observation(
            person_id=int(person_id),
            cover_observation_id=(int(cover_observation_id) if cover_observation_id is not None else None),
        )
        for assignment in assignments:
            self.create_bootstrap_assignment(
                person_id=int(person_id),
                face_observation_id=int(assignment["face_observation_id"]),
                threshold_profile_id=threshold_profile_id,
                diagnostic_json='{"decision_kind":"run_activation_publish"}',
                source_run_id=int(source_run_id),
                source_cluster_id=int(source_cluster_id),
            )
        for seed in trusted_seeds:
            self.create_trusted_sample(
                person_id=int(person_id),
                face_observation_id=int(seed["face_observation_id"]),
                trust_source=str(seed.get("trust_source") or "bootstrap_seed"),
                trust_score=float(seed.get("trust_score") or 1.0),
                quality_score_snapshot=float(seed.get("quality_score_snapshot") or 0.0),
                threshold_profile_id=threshold_profile_id,
                source_auto_cluster_id=None,
                source_run_id=int(source_run_id),
                source_cluster_id=int(source_cluster_id),
            )

    def retire_bootstrap_people(self, *, source_run_id: int) -> dict[str, Any]:
        run_id = int(source_run_id)
        assignment_ids = self._select_ids(
            """
            SELECT id
            FROM person_face_assignment
            WHERE source_run_id = ?
              AND active = 1
            """,
            (run_id,),
        )
        trusted_ids = self._select_ids(
            """
            SELECT id
            FROM person_trusted_sample
            WHERE source_run_id = ?
              AND active = 1
            """,
            (run_id,),
        )
        origin_ids = self._select_ids(
            """
            SELECT id
            FROM person_cluster_origin
            WHERE source_run_id = ?
              AND active = 1
            """,
            (run_id,),
        )
        person_ids = self._select_ids(
            """
            SELECT DISTINCT person_id AS id
            FROM person_cluster_origin
            WHERE source_run_id = ?
            """,
            (run_id,),
        )
        prototype_ids = self._select_ids_for_persons(
            """
            SELECT id
            FROM person_prototype
            WHERE person_id IN ({placeholders})
              AND active = 1
            """,
            person_ids=person_ids,
        )
        person_state_rows = self.conn.execute(
            """
            SELECT id, status, ignored
            FROM person
            WHERE id IN (
                SELECT DISTINCT person_id
                FROM person_cluster_origin
                WHERE source_run_id = ?
            )
              AND status = 'active'
              AND ignored = 0
            ORDER BY id ASC
            """,
            (run_id,),
        ).fetchall()
        person_state = [
            {
                "person_id": int(row["id"]),
                "status": str(row["status"]),
                "ignored": int(row["ignored"]),
            }
            for row in person_state_rows
        ]

        self._set_active_by_ids(table="person_face_assignment", ids=assignment_ids, active=0)
        self._set_active_by_ids(table="person_trusted_sample", ids=trusted_ids, active=0)
        self._set_active_by_ids(table="person_cluster_origin", ids=origin_ids, active=0)
        self._set_active_by_ids(table="person_prototype", ids=prototype_ids, active=0)
        if person_state:
            self.conn.execute(
                """
                UPDATE person
                SET status = 'ignored',
                    ignored = 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id IN ({placeholders})
                """.format(placeholders=", ".join("?" for _ in person_state)),
                tuple(int(item["person_id"]) for item in person_state),
            )

        return {
            "source_run_id": run_id,
            "assignment_ids": assignment_ids,
            "trusted_sample_ids": trusted_ids,
            "origin_ids": origin_ids,
            "prototype_ids": prototype_ids,
            "person_state": person_state,
        }

    def restore_bootstrap_people(
        self,
        *,
        source_run_id: int,
        live_snapshot: dict[str, Any] | None = None,
    ) -> None:
        if live_snapshot is None:
            self.conn.execute(
                """
                UPDATE person_cluster_origin
                SET active = 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE source_run_id = ?
                """,
                (int(source_run_id),),
            )
            self.conn.execute(
                """
                UPDATE person_face_assignment
                SET active = 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE source_run_id = ?
                """,
                (int(source_run_id),),
            )
            self.conn.execute(
                """
                UPDATE person_trusted_sample
                SET active = 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE source_run_id = ?
                """,
                (int(source_run_id),),
            )
            self.conn.execute(
                """
                UPDATE person_prototype
                SET active = 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id IN (
                    SELECT MAX(pp.id)
                    FROM person_prototype AS pp
                    WHERE pp.person_id IN (
                        SELECT pco.person_id
                        FROM person_cluster_origin AS pco
                        WHERE pco.source_run_id = ?
                    )
                      AND pp.prototype_type = 'centroid'
                    GROUP BY pp.person_id
                )
                """,
                (int(source_run_id),),
            )
            return

        self._set_active_by_ids(
            table="person_cluster_origin",
            ids=[int(value) for value in live_snapshot.get("origin_ids") or []],
            active=1,
        )
        self._set_active_by_ids(
            table="person_face_assignment",
            ids=[int(value) for value in live_snapshot.get("assignment_ids") or []],
            active=1,
        )
        self._set_active_by_ids(
            table="person_trusted_sample",
            ids=[int(value) for value in live_snapshot.get("trusted_sample_ids") or []],
            active=1,
        )
        self._set_active_by_ids(
            table="person_prototype",
            ids=[int(value) for value in live_snapshot.get("prototype_ids") or []],
            active=1,
        )
        for row in live_snapshot.get("person_state") or []:
            if not isinstance(row, dict):
                continue
            self.conn.execute(
                """
                UPDATE person
                SET status = ?,
                    ignored = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    str(row.get("status") or "active"),
                    int(row.get("ignored") or 0),
                    int(row.get("person_id")),
                ),
            )

    def _select_ids(self, sql: str, params: tuple[Any, ...]) -> list[int]:
        rows = self.conn.execute(sql, params).fetchall()
        return [int(row["id"]) for row in rows if row["id"] is not None]

    def _select_ids_for_persons(self, sql_template: str, *, person_ids: list[int]) -> list[int]:
        if not person_ids:
            return []
        sql = sql_template.format(placeholders=", ".join("?" for _ in person_ids))
        rows = self.conn.execute(sql, tuple(int(person_id) for person_id in person_ids)).fetchall()
        return [int(row["id"]) for row in rows if row["id"] is not None]

    def _set_active_by_ids(self, *, table: str, ids: list[int], active: int) -> None:
        if not ids:
            return
        allowed = {
            "person_face_assignment",
            "person_trusted_sample",
            "person_cluster_origin",
            "person_prototype",
        }
        if str(table) not in allowed:
            raise ValueError(f"非法 active 更新表名: {table}")
        placeholders = ", ".join("?" for _ in ids)
        self.conn.execute(
            f"""
            UPDATE {table}
            SET active = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id IN ({placeholders})
            """,
            (int(active), *(int(value) for value in ids)),
        )

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
