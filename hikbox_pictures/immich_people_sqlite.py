"""Immich 风格人物增量处理的 SQLite 持久层。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sqlite3

import numpy as np

from hikbox_pictures.immich_face_single_file import AssetRecord
from hikbox_pictures.immich_face_single_file import BoundingBox
from hikbox_pictures.immich_face_single_file import FaceRecord
from hikbox_pictures.immich_face_single_file import ImmichLikeFaceEngine
from hikbox_pictures.immich_face_single_file import PersonRecord
from hikbox_pictures.product.db.connection import connect_sqlite

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS immich_people_source (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  root_path TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS immich_people_asset (
  id TEXT PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES immich_people_source(id),
  image_path TEXT NOT NULL UNIQUE,
  file_name TEXT NOT NULL,
  extension TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS immich_people_person (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS immich_people_face (
  id TEXT PRIMARY KEY,
  asset_id TEXT NOT NULL REFERENCES immich_people_asset(id) ON DELETE CASCADE,
  person_id TEXT REFERENCES immich_people_person(id),
  bbox_x1 REAL NOT NULL,
  bbox_y1 REAL NOT NULL,
  bbox_x2 REAL NOT NULL,
  bbox_y2 REAL NOT NULL,
  image_width INTEGER NOT NULL,
  image_height INTEGER NOT NULL,
  embedding BLOB NOT NULL,
  score REAL NOT NULL,
  source_type TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_immich_people_asset_source_id
ON immich_people_asset(source_id);

CREATE INDEX IF NOT EXISTS idx_immich_people_face_asset_id
ON immich_people_face(asset_id);

CREATE INDEX IF NOT EXISTS idx_immich_people_face_person_id
ON immich_people_face(person_id);
"""


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


class ImmichPeopleSqliteStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()

    def initialize(self) -> None:
        conn = connect_sqlite(self.db_path)
        try:
            conn.executescript(SCHEMA_SQL)
            conn.commit()
        finally:
            conn.close()

    def load_into_engine(self, engine: ImmichLikeFaceEngine) -> None:
        self.initialize()
        conn = connect_sqlite(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            for row in conn.execute(
                """
                SELECT id, image_path, created_at, updated_at
                FROM immich_people_asset
                ORDER BY created_at, id
                """
            ):
                asset = AssetRecord(
                    id=str(row["id"]),
                    image_path=Path(str(row["image_path"])),
                    file_created_at=_parse_datetime(str(row["created_at"])),
                    faces_recognized_at=_parse_datetime(str(row["updated_at"])),
                )
                engine.assets[asset.id] = asset

            for row in conn.execute(
                """
                SELECT id, created_at
                FROM immich_people_person
                ORDER BY created_at, id
                """
            ):
                engine.people[str(row["id"])] = PersonRecord(
                    id=str(row["id"]),
                    created_at=_parse_datetime(str(row["created_at"])),
                )

            for row in conn.execute(
                """
                SELECT
                  id,
                  asset_id,
                  person_id,
                  bbox_x1,
                  bbox_y1,
                  bbox_x2,
                  bbox_y2,
                  image_width,
                  image_height,
                  embedding,
                  score,
                  source_type,
                  created_at
                FROM immich_people_face
                ORDER BY created_at, id
                """
            ):
                embedding = np.frombuffer(bytes(row["embedding"]), dtype=np.float32).copy()
                face = FaceRecord(
                    id=str(row["id"]),
                    asset_id=str(row["asset_id"]),
                    bounding_box=BoundingBox(
                        x1=float(row["bbox_x1"]),
                        y1=float(row["bbox_y1"]),
                        x2=float(row["bbox_x2"]),
                        y2=float(row["bbox_y2"]),
                    ),
                    image_width=int(row["image_width"]),
                    image_height=int(row["image_height"]),
                    embedding=embedding,
                    score=float(row["score"]),
                    source_type=str(row["source_type"]),
                    person_id=str(row["person_id"]) if row["person_id"] is not None else None,
                    created_at=_parse_datetime(str(row["created_at"])),
                )
                engine.faces[face.id] = face
                engine.face_search.upsert(face.id, face.embedding)
                asset = engine.assets.get(face.asset_id)
                if asset and face.id not in asset.face_ids:
                    asset.face_ids.append(face.id)
                if face.person_id:
                    person = engine.people.setdefault(face.person_id, PersonRecord(id=face.person_id))
                    if face.id not in person.face_ids:
                        person.face_ids.append(face.id)
        finally:
            conn.close()

    def persist_current_assets(
        self,
        *,
        input_root: Path,
        engine: ImmichLikeFaceEngine,
        asset_ids: list[str],
    ) -> None:
        self.initialize()
        conn = connect_sqlite(self.db_path)
        try:
            source_id = self._upsert_source(conn, source_root=input_root)
            now = _utcnow().isoformat()
            for asset_id in asset_ids:
                asset = engine.assets[asset_id]
                conn.execute(
                    """
                    INSERT INTO immich_people_asset(
                      id,
                      source_id,
                      image_path,
                      file_name,
                      extension,
                      created_at,
                      updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        asset.id,
                        source_id,
                        str(asset.image_path.resolve()),
                        asset.image_path.name,
                        asset.image_path.suffix.lower().lstrip("."),
                        asset.file_created_at.isoformat(),
                        now,
                    ),
                )
                for face_id in asset.face_ids:
                    face = engine.faces[face_id]
                    if face.person_id:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO immich_people_person(id, created_at)
                            VALUES (?, ?)
                            """,
                            (
                                face.person_id,
                                engine.people.get(face.person_id, PersonRecord(id=face.person_id)).created_at.isoformat(),
                            ),
                        )
                    conn.execute(
                        """
                        INSERT INTO immich_people_face(
                          id,
                          asset_id,
                          person_id,
                          bbox_x1,
                          bbox_y1,
                          bbox_x2,
                          bbox_y2,
                          image_width,
                          image_height,
                          embedding,
                          score,
                          source_type,
                          created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            face.id,
                            face.asset_id,
                            face.person_id,
                            float(face.bounding_box.x1),
                            float(face.bounding_box.y1),
                            float(face.bounding_box.x2),
                            float(face.bounding_box.y2),
                            int(face.image_width),
                            int(face.image_height),
                            np.asarray(face.embedding, dtype=np.float32).tobytes(),
                            float(face.score),
                            str(face.source_type),
                            face.created_at.isoformat(),
                        ),
                    )
            conn.commit()
        finally:
            conn.close()

    def _upsert_source(self, conn: sqlite3.Connection, *, source_root: Path) -> int:
        now = _utcnow().isoformat()
        resolved_root = str(source_root.expanduser().resolve())
        conn.execute(
            """
            INSERT INTO immich_people_source(root_path, created_at, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(root_path) DO UPDATE SET
              updated_at = excluded.updated_at
            """,
            (resolved_root, now, now),
        )
        row = conn.execute(
            "SELECT id FROM immich_people_source WHERE root_path = ?",
            (resolved_root,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"未能写入 source: {resolved_root}")
        return int(row[0])
