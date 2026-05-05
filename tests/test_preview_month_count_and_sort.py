"""导出模板预览页：月份总照片数 — 单元级测试。

覆盖：
- PreviewMonthBucket.total_count 字段正确反映该月 only + group 总照片数
"""

from __future__ import annotations

from pathlib import Path
import sqlite3

from hikbox_pictures.product.db.migration import migrate_to_latest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 测试 fixture
# ---------------------------------------------------------------------------


def _apply_all_library_migrations(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        library_v1_sql = (REPO_ROOT / "hikbox_pictures" / "product" / "db" / "sql" / "library_v1.sql").read_text(
            encoding="utf-8"
        )
        conn.executescript(library_v1_sql)
    finally:
        conn.close()
    migrate_to_latest(db_path=db_path, db_name="library")


def _create_source_image(tmp_path: Path, name: str) -> Path:
    src = tmp_path / "source_images" / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"fake image")
    return src


class _FakeWorkspaceContext:
    def __init__(self, db_path: Path) -> None:
        self.library_db_path = db_path
        self.workspace_path = db_path.parent.parent
        self.external_root_path = self.workspace_path / "external"
        self.embedding_db_path = db_path.parent / "embedding.db"
        self.model_root_path = self.workspace_path / ".hikbox" / "models" / "insightface"


def _setup_db_with_assets(
    tmp_path: Path,
    *,
    assets: list[dict[str, object]],
) -> _FakeWorkspaceContext:
    db_path = tmp_path / "library.db"
    _apply_all_library_migrations(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "INSERT INTO library_sources (path, label, active, created_at) VALUES (?, 'iPhone', 1, '2026-05-01T00:00:00Z')",
            (str(tmp_path / "source1"),),
        )

        for p_id, p_name in [("person-alex", "Alex Chen"), ("person-blair", "Blair Lin")]:
            conn.execute(
                "INSERT INTO person (id, display_name, is_named, status, created_at, updated_at) VALUES (?, ?, 1, 'active', '2026-05-01T00:00:00Z', '2026-05-01T00:00:00Z')",
                (p_id, p_name),
            )

        conn.execute(
            "INSERT INTO export_template (template_id, name, output_root, status, created_at, dedup_key) VALUES ('tmpl-1', 'Test', ?, 'active', '2026-05-01T00:00:00Z', 'dedup-1')",
            (str(tmp_path / "export-output"),),
        )
        for pid in ["person-alex", "person-blair"]:
            conn.execute(
                "INSERT INTO export_template_person (template_id, person_id) VALUES ('tmpl-1', ?)",
                (pid,),
            )

        conn.execute(
            """INSERT INTO assignment_runs
            (scan_session_id, algorithm_version, status, param_snapshot_json, started_at, updated_at)
            VALUES (1, 'online_v6', 'completed', '{}', '2026-05-01T00:00:00Z', '2026-05-01T00:00:00Z')"""
        )
        assignment_run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        for asset in assets:
            file_name = asset["file_name"]
            file_ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
            conn.execute(
                """INSERT INTO assets
                (source_id, absolute_path, file_name, file_extension, capture_month,
                 live_photo_mov_path, processing_status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, NULL, 'succeeded', '2026-05-01T00:00:00Z', '2026-05-01T00:00:00Z')""",
                (
                    1,
                    str(asset.get("absolute_path", tmp_path / "source" / file_name)),
                    file_name,
                    file_ext,
                    asset["capture_month"],
                ),
            )
            asset_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            for face_idx, person_id in enumerate(asset.get("person_ids", ["person-alex", "person-blair"])):
                conn.execute(
                    """INSERT INTO face_observations
                    (asset_id, face_index, bbox_x1, bbox_y1, bbox_x2, bbox_y2,
                     image_width, image_height, score, crop_path, context_path, created_at)
                    VALUES (?, ?, 0, 0, 100, 100, 1000, 1000, 0.9, 'crop.jpg', 'ctx.jpg', '2026-05-01T00:00:00Z')""",
                    (asset_id, face_idx),
                )
                face_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    """INSERT INTO person_face_assignments
                    (person_id, face_observation_id, assignment_run_id, assignment_source, active, evidence_json, created_at, updated_at)
                    VALUES (?, ?, ?, 'online_v6', 1, '{}', '2026-05-01T00:00:00Z', '2026-05-01T00:00:00Z')""",
                    (person_id, face_id, assignment_run_id),
                )

        conn.commit()
    finally:
        conn.close()

    return _FakeWorkspaceContext(db_path)


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


class TestMonthBucketTotalCount:
    def test_single_month_total_count(self, tmp_path: Path) -> None:
        """同一个月有 3 张 only 照片时，total_count = 3。"""
        src1 = _create_source_image(tmp_path, "IMG_0001.jpg")
        src2 = _create_source_image(tmp_path, "IMG_0002.jpg")
        src3 = _create_source_image(tmp_path, "IMG_0003.jpg")
        workspace_context = _setup_db_with_assets(tmp_path, assets=[
            {"file_name": "IMG_0001.jpg", "absolute_path": src1, "capture_month": "2025-01", "person_ids": ["person-alex", "person-blair"]},
            {"file_name": "IMG_0002.jpg", "absolute_path": src2, "capture_month": "2025-01", "person_ids": ["person-alex", "person-blair"]},
            {"file_name": "IMG_0003.jpg", "absolute_path": src3, "capture_month": "2025-01", "person_ids": ["person-alex", "person-blair"]},
        ])

        from hikbox_pictures.product.export_templates import compute_export_preview
        result = compute_export_preview(workspace_context, template_id="tmpl-1")

        assert len(result.month_buckets) == 1
        assert result.month_buckets[0].total_count == 3

    def test_multi_month_total_counts(self, tmp_path: Path) -> None:
        """多个月份各有不同数量的照片时，total_count 各自正确。"""
        src1 = _create_source_image(tmp_path, "IMG_0001.jpg")
        src2 = _create_source_image(tmp_path, "IMG_0002.jpg")
        src3 = _create_source_image(tmp_path, "IMG_0003.jpg")
        workspace_context = _setup_db_with_assets(tmp_path, assets=[
            {"file_name": "IMG_0001.jpg", "absolute_path": src1, "capture_month": "2025-01", "person_ids": ["person-alex", "person-blair"]},
            {"file_name": "IMG_0002.jpg", "absolute_path": src2, "capture_month": "2025-02", "person_ids": ["person-alex", "person-blair"]},
            {"file_name": "IMG_0003.jpg", "absolute_path": src3, "capture_month": "2025-02", "person_ids": ["person-alex", "person-blair"]},
        ])

        from hikbox_pictures.product.export_templates import compute_export_preview
        result = compute_export_preview(workspace_context, template_id="tmpl-1")

        assert len(result.month_buckets) == 2
        assert result.month_buckets[0].month == "2025-01"
        assert result.month_buckets[0].total_count == 1
        assert result.month_buckets[1].month == "2025-02"
        assert result.month_buckets[1].total_count == 2
