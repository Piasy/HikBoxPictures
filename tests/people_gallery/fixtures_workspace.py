from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageFilter

try:
    import sqlite3
except ModuleNotFoundError:
    import pysqlite3 as sqlite3  # type: ignore[no-redef]

from hikbox_pictures.db.connection import connect_db
from hikbox_pictures.db.migrator import apply_migrations
from hikbox_pictures.ann import AnnIndexStore
from hikbox_pictures.repositories import (
    AssetRepo,
    ExportRepo,
    OpsEventRepo,
    PersonRepo,
    ReviewRepo,
    ScanRepo,
    SourceRepo,
)
from hikbox_pictures.services.prototype_service import PrototypeService
from hikbox_pictures.workspace import WorkspacePaths, init_workspace_layout

_IMAGE_FACTORY_PATH = Path(__file__).with_name("image_factory.py")
_IMAGE_FACTORY_SPEC = spec_from_file_location("people_gallery_image_factory", _IMAGE_FACTORY_PATH)
if _IMAGE_FACTORY_SPEC is None or _IMAGE_FACTORY_SPEC.loader is None:
    raise RuntimeError(f"无法加载图片工厂夹具文件: {_IMAGE_FACTORY_PATH}")
_IMAGE_FACTORY_MODULE = module_from_spec(_IMAGE_FACTORY_SPEC)
_IMAGE_FACTORY_SPEC.loader.exec_module(_IMAGE_FACTORY_MODULE)
write_number_jpeg = _IMAGE_FACTORY_MODULE.write_number_jpeg


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
                diagnostic_json,
                locked,
                active
            )
            VALUES (?, ?, ?, '{}', ?, 1)
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
            SELECT id, person_id, face_observation_id, assignment_source, diagnostic_json, threshold_profile_id, locked, active, updated_at
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
    external_root: Path | None = None,
) -> SeedWorkspace:
    resolved_external_root = root / ".hikbox" if external_root is None else external_root
    paths = init_workspace_layout(root, resolved_external_root)
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
                    diagnostic_json,
                    locked,
                    active
                )
                VALUES (?, ?, 'manual', '{}', 0, 1)
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


def build_empty_workspace(root: Path, *, external_root: Path | None = None) -> Path:
    resolved_external_root = root / ".hikbox" if external_root is None else external_root
    paths = init_workspace_layout(root, resolved_external_root)
    conn = connect_db(paths.db_path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    return paths.root


def create_number_image_dataset(
    dataset_dir: Path,
    *,
    names: list[str] | None = None,
) -> list[Path]:
    """创建用于 e2e 的数字图片数据集。"""
    effective_names = names or ["001.jpg", "002.jpg", "003.jpg"]
    dataset_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for name in effective_names:
        file_path = dataset_dir / name
        write_number_jpeg(file_path, text=file_path.stem)
        created.append(file_path)
    return created


def inject_mock_embeddings_for_assets(
    workspace: Path,
    *,
    dataset_dir: Path,
    person_specs: list[dict[str, Any]],
    template_name: str = "甲乙模板",
    include_group: bool = True,
    export_live_mov: bool = False,
) -> dict[str, Any]:
    """
    向工作区注入 mock observation/embedding/assignment，并创建可直接导出的模板。

    person_specs 每项支持字段：
    - file_name: str
    - display_name: str
    - vector: list[float]
    - assignment_source: str（默认 manual）
    - locked: bool（默认 False）
    - bbox: tuple[top, right, bottom, left]（默认 0.1,0.9,0.9,0.1）
    - face_area_ratio: float（默认 0.22）
    """
    if not person_specs:
        raise ValueError("person_specs 不能为空")

    paths = init_workspace_layout(workspace, workspace / ".hikbox")
    conn = connect_db(paths.db_path)
    apply_migrations(conn)
    source_repo = SourceRepo(conn)
    asset_repo = AssetRepo(conn)
    person_repo = PersonRepo(conn)
    review_repo = ReviewRepo(conn)
    export_repo = ExportRepo(conn)
    try:
        source_root = str(dataset_dir.resolve())
        source_row = conn.execute(
            """
            SELECT id
            FROM library_source
            WHERE root_path = ?
              AND active = 1
            LIMIT 1
            """,
            (source_root,),
        ).fetchone()
        if source_row is None:
            source_id = source_repo.add_source(
                "MockDigits",
                source_root,
                root_fingerprint=f"fp-{abs(hash(source_root))}",
                active=True,
            )
        else:
            source_id = int(source_row["id"])

        person_ids_by_name: dict[str, int] = {}
        template_person_ids: list[int] = []
        touched_asset_ids: list[int] = []
        spec_occurrences: dict[tuple[str, str], int] = {}

        for index, spec in enumerate(person_specs, start=1):
            file_name = str(spec["file_name"]).strip()
            display_name = str(spec["display_name"]).strip()
            if not file_name or not display_name:
                raise ValueError("person_specs 缺少 file_name/display_name")

            primary_path = str((dataset_dir / file_name).resolve())
            path_row = conn.execute(
                """
                SELECT id
                FROM photo_asset
                WHERE library_source_id = ?
                  AND primary_path = ?
                LIMIT 1
                """,
                (int(source_id), primary_path),
            ).fetchone()
            if path_row is None:
                asset_id = asset_repo.add_photo_asset(
                    source_id,
                    primary_path,
                    processing_status="assignment_done",
                )
            else:
                asset_id = int(path_row["id"])

            capture_day = 10 + index
            conn.execute(
                """
                UPDATE photo_asset
                SET processing_status = 'assignment_done',
                    capture_datetime = ?,
                    capture_month = '2025-04',
                    primary_fingerprint = ?,
                    is_heic = 0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (f"2025-04-{capture_day:02d}T08:00:00+08:00", f"fp-mock-{asset_id}", int(asset_id)),
            )

            bbox = spec.get("bbox", (0.1, 0.9, 0.9, 0.1))
            if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
                raise ValueError("bbox 必须是四元组/四元素列表 (top, right, bottom, left)")

            occurrence_key = (file_name, display_name)
            occurrence = int(spec_occurrences.get(occurrence_key, 0)) + 1
            spec_occurrences[occurrence_key] = occurrence
            marker_payload = f"{source_root}|{file_name}|{display_name}|{occurrence}"
            marker_digest = sha256(marker_payload.encode("utf-8")).hexdigest()[:24]
            detector_key = "mock_embedding_fixture"
            detector_version = f"mk-{marker_digest}"
            crop_path = paths.artifacts_dir / "mock-crops" / f"{detector_version}.jpg"
            write_number_jpeg(crop_path, text=f"C{asset_id}")
            observation_row = conn.execute(
                """
                SELECT id
                FROM face_observation
                WHERE photo_asset_id = ?
                  AND detector_key = ?
                  AND detector_version = ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (int(asset_id), detector_key, detector_version),
            ).fetchone()
            if observation_row is None:
                cursor = conn.execute(
                    """
                    INSERT INTO face_observation(
                        photo_asset_id,
                        bbox_top,
                        bbox_right,
                        bbox_bottom,
                        bbox_left,
                        face_area_ratio,
                        crop_path,
                        detector_key,
                        detector_version,
                        active
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        int(asset_id),
                        float(bbox[0]),
                        float(bbox[1]),
                        float(bbox[2]),
                        float(bbox[3]),
                        float(spec.get("face_area_ratio", 0.22)),
                        str(crop_path),
                        detector_key,
                        detector_version,
                    ),
                )
                observation_id = int(cursor.lastrowid)
            else:
                observation_id = int(observation_row["id"])
                conn.execute(
                    """
                    UPDATE face_observation
                    SET bbox_top = ?,
                        bbox_right = ?,
                        bbox_bottom = ?,
                        bbox_left = ?,
                        face_area_ratio = ?,
                        crop_path = ?,
                        active = 1
                    WHERE id = ?
                    """,
                    (
                        float(bbox[0]),
                        float(bbox[1]),
                        float(bbox[2]),
                        float(bbox[3]),
                        float(spec.get("face_area_ratio", 0.22)),
                        str(crop_path),
                        int(observation_id),
                    ),
                )

            vector_raw = spec.get("vector")
            if not isinstance(vector_raw, list) or not vector_raw:
                raise ValueError("vector 必须是非空 list[float]")
            vector = np.asarray(vector_raw, dtype=np.float32)
            conn.execute(
                """
                INSERT INTO face_embedding(
                    face_observation_id,
                    feature_type,
                    model_key,
                    dimension,
                    vector_blob,
                    normalized
                )
                VALUES (?, 'face', 'pipeline-stub-v1', ?, ?, 1)
                ON CONFLICT(face_observation_id, feature_type)
                DO UPDATE SET
                    model_key = excluded.model_key,
                    dimension = excluded.dimension,
                    vector_blob = excluded.vector_blob,
                    normalized = excluded.normalized,
                    generated_at = CURRENT_TIMESTAMP
                """,
                (int(observation_id), int(vector.size), vector.tobytes()),
            )

            person_id = person_ids_by_name.get(display_name)
            if person_id is None:
                person_row = conn.execute(
                    """
                    SELECT id
                    FROM person
                    WHERE display_name = ?
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    (display_name,),
                ).fetchone()
                if person_row is None:
                    person_id = person_repo.create_person(display_name, status="active", confirmed=True, ignored=False)
                else:
                    person_id = int(person_row["id"])
                person_ids_by_name[display_name] = int(person_id)
                template_person_ids.append(int(person_id))

            assignment_source = str(spec.get("assignment_source", "manual"))
            locked = 1 if bool(spec.get("locked", False)) else 0
            conn.execute(
                """
                UPDATE person_face_assignment
                SET active = 0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE face_observation_id = ?
                  AND person_id <> ?
                  AND active = 1
                """,
                (int(observation_id), int(person_id)),
            )
            assignment_row = conn.execute(
                """
                SELECT id
                FROM person_face_assignment
                WHERE face_observation_id = ?
                  AND person_id = ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (int(observation_id), int(person_id)),
            ).fetchone()
            if assignment_row is None:
                conn.execute(
                    """
                    INSERT INTO person_face_assignment(
                        person_id,
                        face_observation_id,
                        assignment_source,
                        diagnostic_json,
                        locked,
                        active
                    )
                    VALUES (?, ?, ?, '{}', ?, 1)
                    """,
                    (
                        int(person_id),
                        int(observation_id),
                        assignment_source,
                        locked,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE person_face_assignment
                    SET assignment_source = ?,
                        diagnostic_json = '{}',
                        locked = ?,
                        active = 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (assignment_source, locked, int(assignment_row["id"])),
                )
            touched_asset_ids.append(int(asset_id))

        template_output_root = str(paths.exports_dir / "mock")
        template_row = conn.execute(
            """
            SELECT id
            FROM export_template
            WHERE name = ?
              AND output_root = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (template_name, template_output_root),
        ).fetchone()
        if template_row is None:
            template_id = export_repo.create_template(
                name=template_name,
                output_root=template_output_root,
                include_group=include_group,
                export_live_mov=export_live_mov,
                enabled=True,
            )
        else:
            template_id = int(template_row["id"])
            conn.execute(
                """
                UPDATE export_template
                SET include_group = ?,
                    export_live_mov = ?,
                    enabled = 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    1 if include_group else 0,
                    1 if export_live_mov else 0,
                    int(template_id),
                ),
            )
            conn.execute("DELETE FROM export_template_person WHERE template_id = ?", (int(template_id),))
        for position, person_id in enumerate(template_person_ids):
            export_repo.add_template_person(template_id=template_id, person_id=person_id, position=position)

        review_payload = json.dumps(
            {
                "mock_marker": source_root,
                "template_name": template_name,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        review_row = conn.execute(
            """
            SELECT id
            FROM review_item
            WHERE review_type = 'new_person'
              AND status = 'open'
              AND payload_json = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (review_payload,),
        ).fetchone()
        if review_row is None:
            review_id = review_repo.create_review_item(
                "new_person",
                payload_json=review_payload,
                priority=35,
                status="open",
                primary_person_id=int(template_person_ids[0]) if template_person_ids else None,
            )
        else:
            review_id = int(review_row["id"])

        conn.commit()
        return {
            "template_id": int(template_id),
            "person_ids": template_person_ids,
            "person_ids_by_name": person_ids_by_name,
            "asset_ids": touched_asset_ids,
            "source_id": int(source_id),
            "review_id": int(review_id),
            "review_payload": review_payload,
        }
    finally:
        conn.close()


def build_seed_workspace_with_mock_embeddings(
    root: Path,
    *,
    names: list[str] | None = None,
    person_specs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """可复用的一站式 helper：创建数字图并注入 mock embeddings。"""
    dataset_dir = root / "mock-digits"
    created = create_number_image_dataset(dataset_dir, names=names)
    default_specs: list[dict[str, Any]] = [
        {"file_name": "001.jpg", "display_name": "人物甲", "vector": [0.11, 0.12, 0.13, 0.14], "locked": True},
        {"file_name": "001.jpg", "display_name": "人物乙", "vector": [0.21, 0.22, 0.23, 0.24], "locked": False},
        {"file_name": "002.jpg", "display_name": "人物甲", "vector": [0.31, 0.32, 0.33, 0.34], "locked": True},
    ]
    result = inject_mock_embeddings_for_assets(
        root,
        dataset_dir=dataset_dir,
        person_specs=person_specs if person_specs is not None else default_specs,
        template_name="甲乙模板",
    )
    result["dataset_dir"] = dataset_dir
    result["created_images"] = created
    return result


_IDENTITY_PROFILE_NON_SYSTEM_COLUMNS: tuple[str, ...] = (
    "profile_name",
    "profile_version",
    "quality_formula_version",
    "embedding_feature_type",
    "embedding_model_key",
    "embedding_distance_metric",
    "embedding_schema_version",
    "quality_area_weight",
    "quality_sharpness_weight",
    "quality_pose_weight",
    "area_log_p10",
    "area_log_p90",
    "sharpness_log_p10",
    "sharpness_log_p90",
    "pose_score_p10",
    "pose_score_p90",
    "low_quality_threshold",
    "high_quality_threshold",
    "trusted_seed_quality_threshold",
    "bootstrap_edge_accept_threshold",
    "bootstrap_edge_candidate_threshold",
    "bootstrap_margin_threshold",
    "bootstrap_min_cluster_size",
    "bootstrap_min_distinct_photo_count",
    "bootstrap_min_high_quality_count",
    "bootstrap_seed_min_count",
    "bootstrap_seed_max_count",
    "assignment_auto_min_quality",
    "assignment_auto_distance_threshold",
    "assignment_auto_margin_threshold",
    "assignment_review_distance_threshold",
    "assignment_require_photo_conflict_free",
    "trusted_min_quality",
    "trusted_centroid_distance_threshold",
    "trusted_margin_threshold",
    "trusted_block_exact_duplicate",
    "trusted_block_burst_duplicate",
    "burst_time_window_seconds",
    "possible_merge_distance_threshold",
    "possible_merge_margin_threshold",
)


def ensure_identity_threshold_profile_table(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'identity_threshold_profile'
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("缺少 identity_threshold_profile 表，请先执行数据库 migration。")

    columns = {
        str(item["name"])
        for item in conn.execute("PRAGMA table_info(identity_threshold_profile)").fetchall()
    }
    required = set(_IDENTITY_PROFILE_NON_SYSTEM_COLUMNS) | {"id", "active", "created_at", "updated_at"}
    missing = sorted(required - columns)
    if missing:
        raise RuntimeError(f"identity_threshold_profile 表缺少字段: {missing}")


def get_workspace_embedding_binding(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT DISTINCT feature_type, model_key
        FROM face_embedding
        WHERE feature_type IS NOT NULL
          AND model_key IS NOT NULL
        ORDER BY feature_type ASC, model_key ASC
        """
    ).fetchall()
    if not rows:
        raise ValueError("缺少可用 face_embedding，无法推导 workspace embedding 绑定")
    if len(rows) != 1:
        raise ValueError("face_embedding 绑定不唯一，无法创建 identity profile seed")
    row = rows[0]
    return {
        "embedding_feature_type": str(row["feature_type"]),
        "embedding_model_key": str(row["model_key"]),
        "embedding_distance_metric": "cosine",
        "embedding_schema_version": "face_embedding.v1",
    }


def build_identity_profile_candidate(
    conn: sqlite3.Connection,
    *,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    binding = get_workspace_embedding_binding(conn)
    candidate: dict[str, Any] = {
        "profile_name": "默认阈值档",
        "profile_version": "v1",
        "quality_formula_version": "quality.v1",
        "embedding_feature_type": binding["embedding_feature_type"],
        "embedding_model_key": binding["embedding_model_key"],
        "embedding_distance_metric": binding["embedding_distance_metric"],
        "embedding_schema_version": binding["embedding_schema_version"],
        "quality_area_weight": 0.6,
        "quality_sharpness_weight": 0.4,
        "quality_pose_weight": 0.0,
        "area_log_p10": -3.1,
        "area_log_p90": -1.4,
        "sharpness_log_p10": 2.0,
        "sharpness_log_p90": 3.0,
        "pose_score_p10": None,
        "pose_score_p90": None,
        "low_quality_threshold": 0.45,
        "high_quality_threshold": 0.75,
        "trusted_seed_quality_threshold": 0.85,
        "bootstrap_edge_accept_threshold": 0.8,
        "bootstrap_edge_candidate_threshold": 0.88,
        "bootstrap_margin_threshold": 0.28,
        "bootstrap_min_cluster_size": 3,
        "bootstrap_min_distinct_photo_count": 3,
        "bootstrap_min_high_quality_count": 3,
        "bootstrap_seed_min_count": 3,
        "bootstrap_seed_max_count": 8,
        "assignment_auto_min_quality": 0.75,
        "assignment_auto_distance_threshold": 0.88,
        "assignment_auto_margin_threshold": 0.35,
        "assignment_review_distance_threshold": 0.98,
        "assignment_require_photo_conflict_free": 1,
        "trusted_min_quality": 0.85,
        "trusted_centroid_distance_threshold": 0.88,
        "trusted_margin_threshold": 0.35,
        "trusted_block_exact_duplicate": 1,
        "trusted_block_burst_duplicate": 1,
        "burst_time_window_seconds": 3,
        "possible_merge_distance_threshold": None,
        "possible_merge_margin_threshold": None,
    }
    if overrides:
        candidate.update(overrides)
    return candidate


def seed_active_identity_threshold_profile(
    conn: sqlite3.Connection,
    *,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_identity_threshold_profile_table(conn)
    candidate = build_identity_profile_candidate(conn, overrides=overrides)
    columns = ", ".join(_IDENTITY_PROFILE_NON_SYSTEM_COLUMNS)
    placeholders = ", ".join("?" for _ in _IDENTITY_PROFILE_NON_SYSTEM_COLUMNS)
    values = tuple(candidate[column] for column in _IDENTITY_PROFILE_NON_SYSTEM_COLUMNS)
    conn.execute("UPDATE identity_threshold_profile SET active = 0")
    cursor = conn.execute(
        f"""
        INSERT INTO identity_threshold_profile(
            {columns},
            active,
            activated_at
        )
        VALUES ({placeholders}, 1, CURRENT_TIMESTAMP)
        """,
        values,
    )
    conn.commit()
    binding = get_workspace_embedding_binding(conn)
    return {
        "active_profile_id": int(cursor.lastrowid),
        "candidate_profile": dict(candidate),
        "workspace_embedding_binding": binding,
    }


@dataclass
class IdentityRealWorkspace:
    root: Path
    paths: WorkspacePaths
    conn: sqlite3.Connection
    profile_id: int
    observation_ids: list[int]
    photo_ids: list[int]
    _pick_cursor: int = 0

    def close(self) -> None:
        self.conn.close()

    def pick_observation_with_crop(self) -> int:
        if not self.observation_ids:
            raise RuntimeError("当前工作区没有可用 observation")
        index = self._pick_cursor % len(self.observation_ids)
        self._pick_cursor += 1
        return int(self.observation_ids[index])

    def pick_observation_and_photo(self) -> tuple[int, int]:
        if not self.observation_ids or not self.photo_ids:
            raise RuntimeError("当前工作区缺少 observation/photo 数据")
        return int(self.observation_ids[0]), int(self.photo_ids[0])

    def break_crop_for_observation(self, observation_id: int) -> None:
        row = self.conn.execute(
            """
            SELECT crop_path
            FROM face_observation
            WHERE id = ?
            """,
            (int(observation_id),),
        ).fetchone()
        if row is None:
            raise LookupError(f"observation {observation_id} 不存在")
        crop_path = row["crop_path"]
        if crop_path:
            Path(str(crop_path)).unlink(missing_ok=True)

    def break_original_for_photo(self, photo_id: int) -> None:
        row = self.conn.execute(
            """
            SELECT primary_path
            FROM photo_asset
            WHERE id = ?
            """,
            (int(photo_id),),
        ).fetchone()
        if row is None:
            raise LookupError(f"photo {photo_id} 不存在")
        Path(str(row["primary_path"])).unlink(missing_ok=True)

    def get_observation(self, observation_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT id, photo_asset_id, face_area_ratio, sharpness_score, quality_score, crop_path, pose_score
            FROM face_observation
            WHERE id = ?
            """,
            (int(observation_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_profile(self, profile_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM identity_threshold_profile
            WHERE id = ?
            """,
            (int(profile_id),),
        ).fetchone()
        return dict(row) if row is not None else None


def _write_checker_image(target: Path, *, size: int = 160, cell: int = 8) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas = np.zeros((size, size, 3), dtype=np.uint8)
    for row in range(size):
        for col in range(size):
            checker = ((row // cell) + (col // cell)) % 2
            value = 240 if checker == 0 else 16
            canvas[row, col] = [value, value, value]
    Image.fromarray(canvas, mode="RGB").save(target, format="JPEG", quality=95)


def _write_blurry_checker_image(target: Path, *, size: int = 160, cell: int = 8) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    sharp_path = target.with_name(f"{target.stem}_sharp{target.suffix}")
    _write_checker_image(sharp_path, size=size, cell=cell)
    with Image.open(sharp_path) as image:
        blurred = image.filter(ImageFilter.GaussianBlur(radius=4.0))
        blurred.save(target, format="JPEG", quality=95)
    sharp_path.unlink(missing_ok=True)


def _crop_from_original(original_path: Path, crop_path: Path, *, left: int, top: int, right: int, bottom: int) -> None:
    crop_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(original_path) as image:
        image.crop((left, top, right, bottom)).convert("RGB").save(crop_path, format="JPEG", quality=95)


def build_identity_real_workspace(root: Path) -> IdentityRealWorkspace:
    paths = init_workspace_layout(root, root / ".hikbox")
    conn = connect_db(paths.db_path)
    apply_migrations(conn)
    source_repo = SourceRepo(conn)
    asset_repo = AssetRepo(conn)

    source_root = paths.root / "identity-real-input"
    source_root.mkdir(parents=True, exist_ok=True)
    photo_a_path = source_root / "sharp-a.jpg"
    photo_b_path = source_root / "blurry-b.jpg"
    _write_checker_image(photo_a_path)
    _write_blurry_checker_image(photo_b_path)

    source_id = source_repo.add_source(
        "identity-real-source",
        str(source_root.resolve()),
        root_fingerprint="fp-identity-real",
        active=True,
    )
    photo_a_id = asset_repo.add_photo_asset(source_id, str(photo_a_path.resolve()), processing_status="faces_done")
    photo_b_id = asset_repo.add_photo_asset(source_id, str(photo_b_path.resolve()), processing_status="faces_done")

    crops_dir = paths.artifacts_dir / "face-crops" / "seed"
    crop_a = crops_dir / "obs-a.jpg"
    crop_b = crops_dir / "obs-b.jpg"
    _crop_from_original(photo_a_path, crop_a, left=24, top=24, right=136, bottom=136)
    _crop_from_original(photo_b_path, crop_b, left=24, top=24, right=136, bottom=136)

    observation_ids: list[int] = []
    for photo_id, crop_path, area_ratio in (
        (photo_a_id, crop_a, 0.49),
        (photo_b_id, crop_b, 0.21),
    ):
        cursor = conn.execute(
            """
            INSERT INTO face_observation(
                photo_asset_id,
                bbox_top,
                bbox_right,
                bbox_bottom,
                bbox_left,
                face_area_ratio,
                crop_path,
                detector_key,
                detector_version,
                active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                int(photo_id),
                0.15,
                0.85,
                0.85,
                0.15,
                float(area_ratio),
                str(crop_path.resolve()),
                "fixture",
                "identity-real-v1",
            ),
        )
        observation_id = int(cursor.lastrowid)
        observation_ids.append(observation_id)
        vector = np.asarray([0.11, 0.22, 0.33, 0.44], dtype=np.float32)
        conn.execute(
            """
            INSERT INTO face_embedding(
                face_observation_id,
                feature_type,
                model_key,
                dimension,
                vector_blob,
                normalized
            )
            VALUES (?, 'face', 'pipeline-stub-v1', ?, ?, 1)
            ON CONFLICT(face_observation_id, feature_type)
            DO UPDATE SET
                model_key = excluded.model_key,
                dimension = excluded.dimension,
                vector_blob = excluded.vector_blob,
                normalized = excluded.normalized,
                generated_at = CURRENT_TIMESTAMP
            """,
            (observation_id, int(vector.size), vector.tobytes()),
        )

    seed = seed_active_identity_threshold_profile(
        conn,
        overrides={
            "area_log_p10": -4.0,
            "area_log_p90": -1.0,
            "sharpness_log_p10": 0.1,
            "sharpness_log_p90": 5.0,
        },
    )

    return IdentityRealWorkspace(
        root=paths.root,
        paths=paths,
        conn=conn,
        profile_id=int(seed["active_profile_id"]),
        observation_ids=observation_ids,
        photo_ids=[int(photo_a_id), int(photo_b_id)],
    )


class _FailOncePrototypeService(PrototypeService):
    def __init__(
        self,
        conn: sqlite3.Connection,
        person_repo: PersonRepo,
        ann_index_store: AnnIndexStore,
        *,
        fail_next_ann_sync: bool,
    ) -> None:
        super().__init__(conn, person_repo, ann_index_store)
        self._fail_next_ann_sync = bool(fail_next_ann_sync)

    def sync_person_ann_entry(self, *, person_id: int, model_key: str | None = None) -> int:
        if self._fail_next_ann_sync:
            self._fail_next_ann_sync = False
            raise RuntimeError("注入故障：ANN 同步失败")
        return super().sync_person_ann_entry(person_id=person_id, model_key=model_key)


@dataclass
class IdentitySeedWorkspace:
    root: Path
    paths: WorkspacePaths
    conn: sqlite3.Connection
    source_id: int
    profile_id: int
    model_key: str
    person_repo: PersonRepo
    _fail_ann_sync_once: bool = False

    def close(self) -> None:
        self.conn.close()

    def parse_json(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            return payload
        if payload is None:
            return {}
        return json.loads(str(payload))

    def fail_next_ann_sync(self) -> None:
        self._fail_ann_sync_once = True

    def new_bootstrap_service(self) -> Any:
        from hikbox_pictures.repositories.identity_repo import IdentityRepo
        from hikbox_pictures.services.identity_bootstrap_service import IdentityBootstrapService

        ann_store = AnnIndexStore(self.paths.artifacts_dir / "ann" / "prototype_index.npz")
        prototype_service = _FailOncePrototypeService(
            self.conn,
            self.person_repo,
            ann_store,
            fail_next_ann_sync=self._fail_ann_sync_once,
        )
        self._fail_ann_sync_once = False
        return IdentityBootstrapService(
            self.conn,
            identity_repo=IdentityRepo(self.conn),
            person_repo=self.person_repo,
            prototype_service=prototype_service,
        )

    def insert_observation_with_embedding(
        self,
        *,
        vector: list[float],
        quality_score: float,
        photo_label: str,
        area_ratio: float = 0.22,
    ) -> dict[str, int]:
        photo_path = self.paths.root / "identity-seed-input" / f"{photo_label}.jpg"
        photo_path.parent.mkdir(parents=True, exist_ok=True)
        write_number_jpeg(photo_path, text=photo_label[:8] if photo_label else "seed")
        existing = self.conn.execute(
            """
            SELECT id
            FROM photo_asset
            WHERE library_source_id = ?
              AND primary_path = ?
            LIMIT 1
            """,
            (int(self.source_id), str(photo_path.resolve())),
        ).fetchone()
        if existing is None:
            asset_id = self.conn.execute(
                """
                INSERT INTO photo_asset(
                    library_source_id,
                    primary_path,
                    processing_status,
                    capture_month
                )
                VALUES (?, ?, 'assignment_done', '2026-04')
                """,
                (
                    int(self.source_id),
                    str(photo_path.resolve()),
                ),
            ).lastrowid
            assert asset_id is not None
        else:
            asset_id = int(existing["id"])

        obs_id = self.conn.execute(
            """
            INSERT INTO face_observation(
                photo_asset_id,
                bbox_top,
                bbox_right,
                bbox_bottom,
                bbox_left,
                face_area_ratio,
                sharpness_score,
                quality_score,
                crop_path,
                detector_key,
                detector_version,
                active
            )
            VALUES (?, 0.1, 0.9, 0.9, 0.1, ?, ?, ?, ?, 'fixture', 'identity-seed-v1', 1)
            """,
            (
                int(asset_id),
                float(area_ratio),
                float(quality_score + 0.2),
                float(quality_score),
                str(photo_path.resolve()),
            ),
        ).lastrowid
        assert obs_id is not None
        vector_array = np.asarray(vector, dtype=np.float32)
        self.conn.execute(
            """
            INSERT INTO face_embedding(
                face_observation_id,
                feature_type,
                model_key,
                dimension,
                vector_blob,
                normalized
            )
            VALUES (?, 'face', ?, ?, ?, 1)
            """,
            (int(obs_id), self.model_key, int(vector_array.size), vector_array.tobytes()),
        )
        self.conn.commit()
        return {"asset_id": int(asset_id), "observation_id": int(obs_id)}

    def seed_edge_rule_challenge_case(self) -> None:
        # cluster-A: 预期 materialized（3 人、3 图、seed 足够）
        self.insert_observation_with_embedding(
            vector=[0.00, 0.00, 0.00, 0.00],
            quality_score=0.98,
            photo_label="edge-materialize-a",
        )
        self.insert_observation_with_embedding(
            vector=[0.021, 0.00, 0.00, 0.00],
            quality_score=0.97,
            photo_label="edge-materialize-b",
        )
        self.insert_observation_with_embedding(
            vector=[-0.079, 0.00, 0.00, 0.00],
            quality_score=0.96,
            photo_label="edge-materialize-c",
        )

        # cluster-B: 预期 review_pending（dedup 后 seed 不足）
        self.insert_observation_with_embedding(
            vector=[1.00, 0.00, 0.00, 0.00],
            quality_score=0.99,
            photo_label="edge-pending-photo-a",
        )
        self.insert_observation_with_embedding(
            vector=[1.00, 0.00, 0.00, 0.00],
            quality_score=0.98,
            photo_label="edge-pending-photo-b",
        )
        self.insert_observation_with_embedding(
            vector=[1.079, 0.00, 0.00, 0.00],
            quality_score=0.97,
            photo_label="edge-pending-photo-a",
        )
        self.insert_observation_with_embedding(
            vector=[1.05, 0.00, 0.00, 0.00],
            quality_score=0.96,
            photo_label="edge-pending-photo-c",
        )

        # cluster-C: 显式制造 photo_conflict reject
        self.insert_observation_with_embedding(
            vector=[2.00, 0.00, 0.00, 0.00],
            quality_score=0.95,
            photo_label="edge-photo-conflict",
        )
        self.insert_observation_with_embedding(
            vector=[2.03, 0.00, 0.00, 0.00],
            quality_score=0.94,
            photo_label="edge-photo-conflict",
        )

        self.conn.commit()

    def seed_materialize_happy_case(self) -> None:
        # 构造稳定通过 gate 的 3 节点 cluster，保证 materialize。
        self.insert_observation_with_embedding(
            vector=[0.00, 0.00, 0.00, 0.00],
            quality_score=0.98,
            photo_label="mat-a",
        )
        self.insert_observation_with_embedding(
            vector=[0.021, 0.00, 0.00, 0.00],
            quality_score=0.96,
            photo_label="mat-b",
        )
        self.insert_observation_with_embedding(
            vector=[-0.079, 0.00, 0.00, 0.00],
            quality_score=0.95,
            photo_label="mat-c",
        )
        self.conn.commit()

    def seed_bootstrap_dedup_collision_case(self) -> None:
        # 4 节点同簇：exact + burst 双重去重后 seed 不足，触发 review_pending。
        self.insert_observation_with_embedding(
            vector=[0.70, 0.10, 0.00, 0.00],
            quality_score=0.99,
            photo_label="dedup-photo-a",
        )
        self.insert_observation_with_embedding(
            vector=[0.70, 0.10, 0.00, 0.00],
            quality_score=0.98,
            photo_label="dedup-photo-b",
        )
        self.insert_observation_with_embedding(
            vector=[0.779, 0.10, 0.00, 0.00],
            quality_score=0.97,
            photo_label="dedup-photo-a",
        )
        self.insert_observation_with_embedding(
            vector=[0.75, 0.10, 0.00, 0.00],
            quality_score=0.96,
            photo_label="dedup-photo-c",
        )
        self.conn.commit()

def build_identity_seed_workspace(root: Path) -> IdentitySeedWorkspace:
    paths = init_workspace_layout(root, root / ".hikbox")
    conn = connect_db(paths.db_path)
    apply_migrations(conn)
    source_repo = SourceRepo(conn)
    person_repo = PersonRepo(conn)
    source_root = paths.root / "identity-seed-source"
    source_root.mkdir(parents=True, exist_ok=True)
    source_id = source_repo.add_source(
        "identity-seed",
        str(source_root.resolve()),
        root_fingerprint="fp-identity-seed",
        active=True,
    )

    warmup_photo = paths.root / "identity-seed-input" / "warmup.jpg"
    warmup_photo.parent.mkdir(parents=True, exist_ok=True)
    write_number_jpeg(warmup_photo, text="warmup")
    warmup_asset_id = conn.execute(
        """
        INSERT INTO photo_asset(
            library_source_id,
            primary_path,
            processing_status
        )
        VALUES (?, ?, 'assignment_done')
        """,
        (int(source_id), str(warmup_photo.resolve())),
    ).lastrowid
    assert warmup_asset_id is not None
    warmup_obs_id = conn.execute(
        """
        INSERT INTO face_observation(
            photo_asset_id,
            bbox_top,
            bbox_right,
            bbox_bottom,
            bbox_left,
            face_area_ratio,
            sharpness_score,
            quality_score,
            crop_path,
            detector_key,
            detector_version,
            active
        )
        VALUES (?, 0.1, 0.9, 0.9, 0.1, 0.3, 1.2, 0.9, ?, 'fixture', 'warmup-v1', 1)
        """,
        (int(warmup_asset_id), str(warmup_photo.resolve())),
    ).lastrowid
    assert warmup_obs_id is not None
    model_key = "pipeline-stub-v1"
    warmup_vector = np.asarray([0.01, 0.01, 0.01, 0.01], dtype=np.float32)
    conn.execute(
        """
        INSERT INTO face_embedding(
            face_observation_id,
            feature_type,
            model_key,
            dimension,
            vector_blob,
            normalized
        )
        VALUES (?, 'face', ?, ?, ?, 1)
        """,
        (int(warmup_obs_id), model_key, int(warmup_vector.size), warmup_vector.tobytes()),
    )

    profile_seed = seed_active_identity_threshold_profile(
        conn,
        overrides={
            "bootstrap_edge_accept_threshold": 0.08,
            "bootstrap_edge_candidate_threshold": 0.16,
            "bootstrap_margin_threshold": 0.02,
            "bootstrap_min_cluster_size": 3,
            "bootstrap_min_distinct_photo_count": 2,
            "bootstrap_min_high_quality_count": 3,
            "bootstrap_seed_min_count": 3,
            "bootstrap_seed_max_count": 4,
            "trusted_seed_quality_threshold": 0.9,
            "high_quality_threshold": 0.85,
        },
    )
    conn.execute("DELETE FROM face_observation WHERE id = ?", (int(warmup_obs_id),))
    conn.execute("DELETE FROM photo_asset WHERE id = ?", (int(warmup_asset_id),))
    conn.commit()

    return IdentitySeedWorkspace(
        root=paths.root,
        paths=paths,
        conn=conn,
        source_id=int(source_id),
        profile_id=int(profile_seed["active_profile_id"]),
        model_key=model_key,
        person_repo=person_repo,
    )
