from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.db.migrator import apply_migrations
from hikbox_pictures.repositories import (
    AssetRepo,
    ExportRepo,
    OpsEventRepo,
    PersonRepo,
    ReviewRepo,
    ScanRepo,
    SourceRepo,
)
from hikbox_pictures.workspace import WorkspacePaths, ensure_workspace_layout


@dataclass
class SeedWorkspace:
    root: Path
    paths: WorkspacePaths
    conn: sqlite3.Connection
    source_repo: SourceRepo
    scan_repo: ScanRepo
    asset_repo: AssetRepo
    person_repo: PersonRepo
    review_repo: ReviewRepo
    export_repo: ExportRepo
    ops_event_repo: OpsEventRepo
    export_template_id: int
    export_live_photo_asset_id: int | None
    media_photo_id: int | None
    media_observation_id: int | None

    def counts(self) -> dict[str, int]:
        tables = (
            "library_source",
            "person",
            "review_item",
            "export_template",
        )
        result: dict[str, int] = {}
        for table in tables:
            row = self.conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
            result[table] = int(row["c"])
        return result

    def close(self) -> None:
        self.conn.close()

    def latest_resumable_session(self) -> dict[str, object] | None:
        row = self.scan_repo.latest_resumable_session()
        return dict(row) if row is not None else None

    def person_display_name(self, person_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT display_name FROM person WHERE id = ?",
            (int(person_id),),
        ).fetchone()
        if row is None:
            return None
        return str(row["display_name"])

    def seed_source_assets(self, source_id: int, paths: list[str]) -> list[int]:
        asset_ids: list[int] = []
        for path in paths:
            asset_ids.append(self.asset_repo.add_photo_asset(source_id, path, processing_status="discovered"))
        self.conn.commit()
        return asset_ids

    def create_assignment(
        self,
        *,
        person_id: int,
        locked: bool = False,
        assignment_source: str = "manual",
    ) -> int:
        source_rows = self.source_repo.list_sources(active=True)
        if not source_rows:
            raise RuntimeError("缺少可用 source，无法创建 assignment 测试数据")
        source_id = int(source_rows[0]["id"])
        row = self.conn.execute("SELECT COUNT(*) AS c FROM photo_asset").fetchone()
        sequence = int(row["c"]) + 1
        asset_id = self.asset_repo.add_photo_asset(
            source_id,
            f"/tmp/assignment-seed-{sequence}.jpg",
            processing_status="assignment_done",
        )
        observation_id = self.asset_repo.ensure_face_observation(asset_id)
        cursor = self.conn.execute(
            """
            INSERT INTO person_face_assignment(
                person_id,
                face_observation_id,
                assignment_source,
                confidence,
                locked,
                active
            )
            VALUES (?, ?, ?, 1.0, ?, 1)
            """,
            (
                int(person_id),
                int(observation_id),
                assignment_source,
                1 if locked else 0,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def get_assignment(self, assignment_id: int) -> dict[str, object] | None:
        row = self.conn.execute(
            """
            SELECT id, person_id, face_observation_id, assignment_source, confidence, locked, active, updated_at
            FROM person_face_assignment
            WHERE id = ?
            """,
            (int(assignment_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_review_item(self, review_id: int) -> dict[str, object] | None:
        row = self.conn.execute(
            """
            SELECT id, review_type, status, resolved_at
            FROM review_item
            WHERE id = ?
            """,
            (int(review_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_person_row(self, person_id: int) -> dict[str, object] | None:
        row = self.conn.execute(
            """
            SELECT id, display_name, status, merged_into_person_id
            FROM person
            WHERE id = ?
            """,
            (int(person_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def break_crop_for_observation(self, observation_id: int) -> None:
        row = self.conn.execute(
            "SELECT crop_path FROM face_observation WHERE id = ?",
            (int(observation_id),),
        ).fetchone()
        if row is None:
            raise LookupError(f"observation {observation_id} 不存在")
        crop_path = row["crop_path"]
        if crop_path:
            Path(str(crop_path)).unlink(missing_ok=True)

    def crop_exists(self, observation_id: int) -> bool:
        row = self.conn.execute(
            "SELECT crop_path FROM face_observation WHERE id = ?",
            (int(observation_id),),
        ).fetchone()
        if row is None:
            return False
        crop_path = row["crop_path"]
        if not crop_path:
            return False
        return Path(str(crop_path)).exists()

    def break_original_for_photo(self, photo_id: int) -> None:
        row = self.conn.execute(
            "SELECT primary_path FROM photo_asset WHERE id = ?",
            (int(photo_id),),
        ).fetchone()
        if row is None:
            raise LookupError(f"photo {photo_id} 不存在")
        Path(str(row["primary_path"])).unlink(missing_ok=True)

    def inject_broken_image_for_photo(self, photo_id: int) -> None:
        row = self.conn.execute(
            "SELECT primary_path FROM photo_asset WHERE id = ?",
            (int(photo_id),),
        ).fetchone()
        if row is None:
            raise LookupError(f"photo {photo_id} 不存在")
        target = Path(str(row["primary_path"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"not-an-image")

    def count_ops_event(self, event_type: str) -> int:
        return self.ops_event_repo.count_by_event_type(event_type)


def build_seed_workspace(
    root: Path,
    *,
    seed_export_assets: bool = False,
    seed_media_assets: bool = False,
) -> SeedWorkspace:
    paths = ensure_workspace_layout(root)
    conn = connect_db(paths.db_path)
    apply_migrations(conn)

    source_repo = SourceRepo(conn)
    scan_repo = ScanRepo(conn)
    asset_repo = AssetRepo(conn)
    person_repo = PersonRepo(conn)
    review_repo = ReviewRepo(conn)
    export_repo = ExportRepo(conn)
    ops_event_repo = OpsEventRepo(conn)

    source_a = source_repo.add_source("iCloud", "/data/a", root_fingerprint="fp-a", active=True)
    source_b = source_repo.add_source("NAS", "/data/b", root_fingerprint="fp-b", active=True)

    _ = scan_repo.create_session(mode="initial", status="completed", started=True)
    resumable_session = scan_repo.create_session(mode="incremental", status="paused", started=True)
    scan_repo.create_session_source(resumable_session, source_a, status="paused")
    scan_repo.create_session_source(resumable_session, source_b, status="pending")

    person_a = person_repo.create_person("人物A", status="active", confirmed=True, ignored=False)
    person_b = person_repo.create_person("人物B", status="active", confirmed=True, ignored=False)
    person_c = person_repo.create_person("人物C", status="active", confirmed=False, ignored=False)

    review_repo.create_review_item("new_person", payload_json="{}", priority=30, status="open", primary_person_id=person_a)
    review_repo.create_review_item(
        "possible_merge",
        payload_json="{}",
        priority=20,
        status="open",
        primary_person_id=person_a,
        secondary_person_id=person_b,
    )
    review_repo.create_review_item(
        "possible_split",
        payload_json="{}",
        priority=10,
        status="open",
        primary_person_id=person_c,
    )
    review_repo.create_review_item(
        "low_confidence_assignment",
        payload_json="{}",
        priority=5,
        status="open",
        primary_person_id=person_b,
    )

    template_id = export_repo.create_template(
        name="家庭模板",
        output_root=str(paths.exports_dir / "family"),
        include_group=True,
        export_live_mov=True,
        enabled=True,
    )
    export_repo.add_template_person(template_id=template_id, person_id=person_a, position=0)
    export_repo.add_template_person(template_id=template_id, person_id=person_b, position=1)

    export_live_photo_asset_id: int | None = None
    media_photo_id: int | None = None
    media_observation_id: int | None = None
    if seed_export_assets:
        seed_assets_dir = paths.root / "seed-assets"
        seed_assets_dir.mkdir(parents=True, exist_ok=True)

        only_primary_1 = seed_assets_dir / "IMG_ONLY_1.jpg"
        only_primary_1.write_bytes(b"only-1")
        only_primary_2 = seed_assets_dir / "IMG_ONLY_2.HEIC"
        only_primary_2.write_bytes(b"only-2")
        only_live_mov_2 = seed_assets_dir / ".IMG_ONLY_2_123456.MOV"
        only_live_mov_2.write_bytes(b"live-2")
        group_primary_1 = seed_assets_dir / "IMG_GROUP_1.jpg"
        group_primary_1.write_bytes(b"group-1")
        miss_primary_1 = seed_assets_dir / "IMG_MISS_1.jpg"
        miss_primary_1.write_bytes(b"miss-1")

        asset_only_1 = asset_repo.add_photo_asset(
            source_a,
            str(only_primary_1),
            processing_status="assignment_done",
        )
        conn.execute(
            """
            UPDATE photo_asset
            SET capture_datetime = ?,
                capture_month = ?,
                primary_fingerprint = ?,
                is_heic = 0
            WHERE id = ?
            """,
            ("2025-04-01T08:00:00+08:00", "2025-04", "fp-only-1", asset_only_1),
        )

        asset_only_2 = asset_repo.add_photo_asset(
            source_a,
            str(only_primary_2),
            processing_status="assignment_done",
        )
        conn.execute(
            """
            UPDATE photo_asset
            SET capture_datetime = ?,
                capture_month = ?,
                primary_fingerprint = ?,
                is_heic = 1,
                live_mov_path = ?,
                live_mov_fingerprint = ?
            WHERE id = ?
            """,
            (
                "2025-04-02T08:00:00+08:00",
                "2025-04",
                "fp-only-2",
                str(only_live_mov_2),
                "fp-live-2",
                asset_only_2,
            ),
        )
        export_live_photo_asset_id = asset_only_2

        asset_group_1 = asset_repo.add_photo_asset(
            source_a,
            str(group_primary_1),
            processing_status="assignment_done",
        )
        conn.execute(
            """
            UPDATE photo_asset
            SET capture_datetime = ?,
                capture_month = ?,
                primary_fingerprint = ?,
                is_heic = 0
            WHERE id = ?
            """,
            ("2025-04-03T08:00:00+08:00", "2025-04", "fp-group-1", asset_group_1),
        )

        asset_miss_1 = asset_repo.add_photo_asset(
            source_a,
            str(miss_primary_1),
            processing_status="assignment_done",
        )
        conn.execute(
            """
            UPDATE photo_asset
            SET capture_datetime = ?,
                capture_month = ?,
                primary_fingerprint = ?,
                is_heic = 0
            WHERE id = ?
            """,
            ("2025-04-04T08:00:00+08:00", "2025-04", "fp-miss-1", asset_miss_1),
        )

        def _insert_face_observation(photo_asset_id: int, face_area_ratio: float | None) -> int:
            cursor = conn.execute(
                """
                INSERT INTO face_observation(
                    photo_asset_id,
                    bbox_top,
                    bbox_right,
                    bbox_bottom,
                    bbox_left,
                    face_area_ratio,
                    active
                )
                VALUES (?, 0.0, 1.0, 1.0, 0.0, ?, 1)
                """,
                (int(photo_asset_id), face_area_ratio),
            )
            return int(cursor.lastrowid)

        def _assign(face_observation_id: int, person_id: int) -> None:
            conn.execute(
                """
                INSERT INTO person_face_assignment(
                    person_id,
                    face_observation_id,
                    assignment_source,
                    confidence,
                    locked,
                    active
                )
                VALUES (?, ?, 'manual', 1.0, 0, 1)
                """,
                (int(person_id), int(face_observation_id)),
            )

        obs_only_1_a = _insert_face_observation(asset_only_1, 0.24)
        obs_only_1_b = _insert_face_observation(asset_only_1, 0.20)
        _assign(obs_only_1_a, person_a)
        _assign(obs_only_1_b, person_b)

        obs_only_2_a = _insert_face_observation(asset_only_2, 0.22)
        obs_only_2_b = _insert_face_observation(asset_only_2, 0.18)
        _assign(obs_only_2_a, person_a)
        _assign(obs_only_2_b, person_b)
        _insert_face_observation(asset_only_2, 0.03)

        obs_group_1_a = _insert_face_observation(asset_group_1, 0.23)
        obs_group_1_b = _insert_face_observation(asset_group_1, 0.21)
        obs_group_1_c = _insert_face_observation(asset_group_1, 0.08)
        _assign(obs_group_1_a, person_a)
        _assign(obs_group_1_b, person_b)
        _assign(obs_group_1_c, person_c)

        obs_miss_1_a = _insert_face_observation(asset_miss_1, 0.25)
        _assign(obs_miss_1_a, person_a)

    if seed_media_assets:
        media_dir = paths.artifacts_dir / "media-test-assets"
        media_dir.mkdir(parents=True, exist_ok=True)
        original_path = media_dir / "photo-1.jpg"
        crop_path = media_dir / "crop-1.jpg"
        Image.new("RGB", (48, 48), color=(180, 120, 90)).save(original_path, format="JPEG")
        Image.new("RGB", (16, 16), color=(90, 140, 200)).save(crop_path, format="JPEG")

        media_photo_id = asset_repo.add_photo_asset(
            source_a,
            str(original_path),
            processing_status="assignment_done",
        )
        cursor = conn.execute(
            """
            INSERT INTO face_observation(
                photo_asset_id,
                bbox_top,
                bbox_right,
                bbox_bottom,
                bbox_left,
                crop_path,
                active
            )
            VALUES (?, 0.1, 0.9, 0.9, 0.1, ?, 1)
            """,
            (int(media_photo_id), str(crop_path)),
        )
        media_observation_id = int(cursor.lastrowid)

    ops_event_repo.append_event(
        level="info",
        component="seed",
        event_type="seed_ready",
        message="seed 数据已初始化",
        run_kind="scan",
        run_id=str(resumable_session),
    )

    conn.commit()

    return SeedWorkspace(
        root=paths.root,
        paths=paths,
        conn=conn,
        source_repo=source_repo,
        scan_repo=scan_repo,
        asset_repo=asset_repo,
        person_repo=person_repo,
        review_repo=review_repo,
        export_repo=export_repo,
        ops_event_repo=ops_event_repo,
        export_template_id=template_id,
        export_live_photo_asset_id=export_live_photo_asset_id,
        media_photo_id=media_photo_id,
        media_observation_id=media_observation_id,
    )
