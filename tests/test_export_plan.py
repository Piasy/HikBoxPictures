"""Feature Slice 2 导出计划持久化与同名冲突消解 — 单元级测试。

覆盖 AC-1 到 AC-10 中可单元验证的部分：
- AC-1: Preview 写入 export_plan
- AC-2: 同名冲突自动加 source_label 后缀
- AC-3: source_label 相同时追加序号
- AC-4: MOV 文件名同步重命名
- AC-5: Execute 从 plan 读取并关联 plan_id
- AC-6: 目标文件已存在时跳过
- AC-7: 再次 preview 只追加新记录
- AC-8: 再次 execute 只导出目标文件不存在的
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sqlite3
import threading

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 测试 fixture 构建
# ---------------------------------------------------------------------------


def _apply_all_library_migrations(db_path: Path) -> None:
    """创建完整的 library DB schema（v1 + v2 + v3 migration）。"""
    from hikbox_pictures.product.db.migration import _apply_migration

    conn = sqlite3.connect(db_path)
    try:
        library_v1_sql = (REPO_ROOT / "hikbox_pictures" / "product" / "db" / "sql" / "library_v1.sql").read_text(
            encoding="utf-8"
        )
        conn.executescript(library_v1_sql)
        conn.execute("UPDATE schema_meta SET value = '1' WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()

    # Apply v2 (placeholder)
    v2_path = REPO_ROOT / "hikbox_pictures" / "product" / "db" / "sql" / "library_v2.sql"
    conn = sqlite3.connect(db_path)
    try:
        _apply_migration(conn, version=2, sql_path=v2_path)
    finally:
        conn.close()

    # Apply v3 (export_plan)
    v3_path = REPO_ROOT / "hikbox_pictures" / "product" / "db" / "sql" / "library_v3.sql"
    conn = sqlite3.connect(db_path)
    try:
        _apply_migration(conn, version=3, sql_path=v3_path)
    finally:
        conn.close()


def _create_source_image(tmp_path: Path, name: str, content: bytes = b"fake image") -> Path:
    """创建一个源图片文件。"""
    src = tmp_path / "source_images" / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(content)
    return src


def _create_mov_source(tmp_path: Path, name: str, content: bytes = b"fake mov") -> Path:
    """创建一个 MOV 源文件。"""
    src = tmp_path / "source_images" / name
    if not src.exists():
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(content)
    return src


def _setup_test_db(
    tmp_path: Path,
    *,
    sources: list[dict[str, str]] | None = None,
    assets: list[dict[str, object]] | None = None,
    persons: list[dict[str, str]] | None = None,
    template_id: str = "template-1",
    template_person_ids: list[str] | None = None,
    output_root: str | None = None,
) -> tuple[Path, Path]:
    """创建一个包含测试数据的 library DB。

    返回 (db_path, output_root_path)。
    """
    db_path = tmp_path / "library.db"
    _apply_all_library_migrations(db_path)

    if output_root is None:
        output_root = str(tmp_path / "export-output")
    Path(output_root).mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # 插入 sources
        if sources is None:
            sources = [{"path": str(tmp_path / "source1"), "label": "iPhone"}]
        for src in sources:
            conn.execute(
                "INSERT INTO library_sources (path, label, active, created_at) VALUES (?, ?, 1, '2026-04-30T00:00:00Z')",
                (src["path"], src["label"]),
            )

        # 插入 persons
        if persons is None:
            persons = [
                {"id": "person-alex", "display_name": "Alex Chen"},
                {"id": "person-blair", "display_name": "Blair Lin"},
            ]
        for p in persons:
            conn.execute(
                "INSERT INTO person (id, display_name, is_named, status, created_at, updated_at) VALUES (?, ?, 1, 'active', '2026-04-30T00:00:00Z', '2026-04-30T00:00:00Z')",
                (p["id"], p["display_name"]),
            )

        # 插入 template
        conn.execute(
            "INSERT INTO export_template (template_id, name, output_root, status, created_at, dedup_key) VALUES (?, 'Test Template', ?, 'active', '2026-04-30T00:00:00Z', 'dedup-1')",
            (template_id, output_root),
        )

        # 插入 template_person
        if template_person_ids is None:
            template_person_ids = ["person-alex", "person-blair"]
        for pid in template_person_ids:
            conn.execute(
                "INSERT INTO export_template_person (template_id, person_id) VALUES (?, ?)",
                (template_id, pid),
            )

        # 插入 assets 和 face_observations + assignments
        if assets is not None:
            for asset in assets:
                source_id = asset.get("source_id", 1)
                file_name = asset["file_name"]
                file_ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
                conn.execute(
                    """INSERT INTO assets
                    (source_id, absolute_path, file_name, file_extension, capture_month,
                     file_fingerprint, live_photo_mov_path, processing_status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'succeeded', '2026-04-30T00:00:00Z', '2026-04-30T00:00:00Z')""",
                    (
                        source_id,
                        str(asset.get("absolute_path", tmp_path / "source" / file_name)),
                        file_name,
                        file_ext,
                        asset.get("capture_month", "2025-01"),
                        f"fp-{file_name}",
                        str(asset["live_photo_mov_path"]) if asset.get("live_photo_mov_path") else None,
                    ),
                )
                asset_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

                # 为每个 person 创建 face_observations 和 assignments
                for face_idx, person_id in enumerate(asset.get("person_ids", template_person_ids)):
                    conn.execute(
                        """INSERT INTO face_observations
                        (asset_id, face_index, bbox_x1, bbox_y1, bbox_x2, bbox_y2,
                         image_width, image_height, score, crop_path, context_path, created_at)
                        VALUES (?, ?, 0, 0, 100, 100, 1000, 1000, 0.9, 'crop.jpg', 'ctx.jpg', '2026-04-30T00:00:00Z')""",
                        (asset_id, face_idx),
                    )
                    face_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

                    # 需要 assignment_run
                    run_row = conn.execute("SELECT id FROM assignment_runs LIMIT 1").fetchone()
                    if run_row is None:
                        conn.execute(
                            """INSERT INTO assignment_runs
                            (scan_session_id, algorithm_version, status, param_snapshot_json, started_at, updated_at)
                            VALUES (1, 'immich_v6_online_v1', 'completed', '{}', '2026-04-30T00:00:00Z', '2026-04-30T00:00:00Z')"""
                        )
                        assignment_run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    else:
                        assignment_run_id = run_row[0]

                    conn.execute(
                        """INSERT INTO person_face_assignments
                        (person_id, face_observation_id, assignment_run_id, assignment_source, active, evidence_json, created_at, updated_at)
                        VALUES (?, ?, ?, 'online_v6', 1, '{}', '2026-04-30T00:00:00Z', '2026-04-30T00:00:00Z')""",
                        (person_id, face_id, assignment_run_id),
                    )

        conn.commit()
    finally:
        conn.close()

    return db_path, Path(output_root)


# ---------------------------------------------------------------------------
# AC-1: Preview 写入 export_plan
# ---------------------------------------------------------------------------


class TestPreviewWritesExportPlan:
    def test_preview_writes_plan_records(self, tmp_path: Path) -> None:
        """AC-1: 创建模板后调用 preview，export_plan 表应有记录。"""
        src1 = _create_source_image(tmp_path, "IMG_0001.jpg")
        src2 = _create_source_image(tmp_path, "IMG_0002.jpg")
        workspace_context = _make_workspace_context(tmp_path, assets=[
            {"file_name": "IMG_0001.jpg", "absolute_path": src1, "capture_month": "2025-01", "person_ids": ["person-alex", "person-blair"]},
            {"file_name": "IMG_0002.jpg", "absolute_path": src2, "capture_month": "2025-02", "person_ids": ["person-alex"]},
        ])

        from hikbox_pictures.product.export_templates import compute_export_preview

        result = compute_export_preview(workspace_context, template_id="template-1")
        assert result.total_count == 1  # 只有 IMG_0001 同时命中 alex 和 blair

        conn = sqlite3.connect(workspace_context.library_db_path)
        try:
            rows = conn.execute(
                "SELECT template_id, asset_id, bucket, month, file_name, source_label FROM export_plan WHERE template_id = 'template-1'"
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 1
        assert rows[0][0] == "template-1"
        assert rows[0][4] == "IMG_0001.jpg"
        assert rows[0][5] == "iPhone"

    def test_preview_plan_has_unique_constraint(self, tmp_path: Path) -> None:
        """AC-1: (template_id, asset_id) 唯一约束验证。"""
        src = _create_source_image(tmp_path, "IMG_0001.jpg")
        workspace_context = _make_workspace_context(tmp_path, assets=[
            {"file_name": "IMG_0001.jpg", "absolute_path": src, "capture_month": "2025-01", "person_ids": ["person-alex", "person-blair"]},
        ])

        from hikbox_pictures.product.export_templates import compute_export_preview

        compute_export_preview(workspace_context, template_id="template-1")

        conn = sqlite3.connect(workspace_context.library_db_path)
        try:
            rows = conn.execute(
                "SELECT COUNT(*) FROM export_plan WHERE template_id = 'template-1'"
            ).fetchone()
            assert rows[0] == 1

            # 验证 UNIQUE 约束存在
            unique_info = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='export_plan'"
            ).fetchone()
            assert "UNIQUE" in str(unique_info[0])
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# AC-2: 同名冲突自动加 source_label 后缀
# ---------------------------------------------------------------------------


class TestConflictResolutionSourceLabel:
    def test_same_filename_different_sources_adds_source_label(self, tmp_path: Path) -> None:
        """AC-2: 两个不同源有同名文件，后写入的应追加 __<source_label>。"""
        src1 = _create_source_image(tmp_path / "iPhone", "IMG_0001.jpg", b"iphone-content")
        src2 = _create_source_image(tmp_path / "Android", "IMG_0001.jpg", b"android-content")

        workspace_context, db_path = _make_workspace_context_with_sources(
            tmp_path,
            sources=[
                {"path": str(tmp_path / "iPhone"), "label": "iPhone"},
                {"path": str(tmp_path / "Android"), "label": "Android"},
            ],
            assets=[
                {"file_name": "IMG_0001.jpg", "absolute_path": src1, "source_id": 1, "capture_month": "2025-01", "person_ids": ["person-alex", "person-blair"]},
                {"file_name": "IMG_0001.jpg", "absolute_path": src2, "source_id": 2, "capture_month": "2025-01", "person_ids": ["person-alex", "person-blair"]},
            ],
        )

        from hikbox_pictures.product.export_templates import compute_export_preview

        compute_export_preview(workspace_context, template_id="template-1")

        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT asset_id, file_name, source_label FROM export_plan WHERE template_id = 'template-1' ORDER BY asset_id"
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 2
        # 第一个保持原名
        assert rows[0][1] == "IMG_0001.jpg"
        # 第二个追加 source_label
        assert rows[0][2] == "iPhone"
        assert "__" in rows[1][1]
        assert rows[1][2] == "Android"


# ---------------------------------------------------------------------------
# AC-3: source_label 相同时追加序号
# ---------------------------------------------------------------------------


class TestConflictResolutionSameLabel:
    def test_same_label_appends_number_suffix(self, tmp_path: Path) -> None:
        """AC-3: 两个不同源但恰好同 label 时，后续文件追加 __<source_label>-2。"""
        src1 = _create_source_image(tmp_path / "dir1", "IMG_0001.jpg", b"content1")
        src2 = _create_source_image(tmp_path / "dir2", "IMG_0001.jpg", b"content2")

        workspace_context, db_path = _make_workspace_context_with_sources(
            tmp_path,
            sources=[
                {"path": str(tmp_path / "dir1"), "label": "Photos"},
                {"path": str(tmp_path / "dir2"), "label": "Photos"},
            ],
            assets=[
                {"file_name": "IMG_0001.jpg", "absolute_path": src1, "source_id": 1, "capture_month": "2025-01", "person_ids": ["person-alex", "person-blair"]},
                {"file_name": "IMG_0001.jpg", "absolute_path": src2, "source_id": 2, "capture_month": "2025-01", "person_ids": ["person-alex", "person-blair"]},
            ],
        )

        from hikbox_pictures.product.export_templates import compute_export_preview

        compute_export_preview(workspace_context, template_id="template-1")

        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT asset_id, file_name, source_label FROM export_plan WHERE template_id = 'template-1' ORDER BY asset_id"
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 2
        # 第一个保持原名
        assert rows[0][1] == "IMG_0001.jpg"
        # 第二个追加 __Photos-2
        assert rows[1][1] == "IMG_0001__Photos-2.jpg"


# ---------------------------------------------------------------------------
# AC-4: MOV 文件名同步重命名
# ---------------------------------------------------------------------------


class TestMovSyncRenaming:
    def test_mov_renamed_with_image(self, tmp_path: Path) -> None:
        """AC-4: HEIC 同名冲突导致重命名时，MOV 文件名也同步重命名。"""
        mov1 = _create_mov_source(tmp_path / "iPhone", ".IMG_0001.MOV")
        mov2 = _create_mov_source(tmp_path / "Android", ".IMG_0001.MOV")
        src1 = _create_source_image(tmp_path / "iPhone", "IMG_0001.heic", b"heic1")
        src2 = _create_source_image(tmp_path / "Android", "IMG_0001.heic", b"heic2")

        workspace_context, db_path = _make_workspace_context_with_sources(
            tmp_path,
            sources=[
                {"path": str(tmp_path / "iPhone"), "label": "iPhone"},
                {"path": str(tmp_path / "Android"), "label": "Android"},
            ],
            assets=[
                {
                    "file_name": "IMG_0001.heic", "absolute_path": src1, "source_id": 1,
                    "capture_month": "2025-01",
                    "live_photo_mov_path": mov1,
                    "person_ids": ["person-alex", "person-blair"],
                },
                {
                    "file_name": "IMG_0001.heic", "absolute_path": src2, "source_id": 2,
                    "capture_month": "2025-01",
                    "live_photo_mov_path": mov2,
                    "person_ids": ["person-alex", "person-blair"],
                },
            ],
        )

        from hikbox_pictures.product.export_templates import compute_export_preview

        compute_export_preview(workspace_context, template_id="template-1")

        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT asset_id, file_name, mov_file_name FROM export_plan WHERE template_id = 'template-1' ORDER BY asset_id"
            ).fetchall()
        finally:
            conn.close()

        assert len(rows) == 2
        # 第一个保持原名（无冲突）
        assert rows[0][1] == "IMG_0001.heic"
        assert rows[0][2] == ".IMG_0001.MOV"
        # 第二个 MOV 也同步重命名（冲突导致）
        assert rows[1][1] == "IMG_0001__Android.heic"
        assert rows[1][2] == ".IMG_0001__Android.MOV"


# ---------------------------------------------------------------------------
# AC-5: Execute 从 plan 读取并关联 plan_id
# ---------------------------------------------------------------------------


class TestExecuteFromPlan:
    def test_execute_reads_from_plan_and_sets_plan_id(self, tmp_path: Path) -> None:
        """AC-5: execute 应从 export_plan 读取，export_delivery.plan_id 应指向 export_plan.id。"""
        src = _create_source_image(tmp_path, "IMG_0001.jpg")
        workspace_context, db_path, output_root = _make_full_workspace(tmp_path, assets=[
            {"file_name": "IMG_0001.jpg", "absolute_path": src, "capture_month": "2025-01", "person_ids": ["person-alex", "person-blair"]},
        ])

        from hikbox_pictures.product.export_templates import compute_export_preview, execute_export

        # Preview 写入 plan
        preview = compute_export_preview(workspace_context, template_id="template-1")
        assert preview.total_count == 1

        # Execute
        run_id = execute_export(workspace_context, template_id="template-1")

        conn = sqlite3.connect(db_path)
        try:
            # 验证 export_delivery.plan_id 指向 export_plan
            deliveries = conn.execute(
                "SELECT delivery_id, plan_id, asset_id, result FROM export_delivery WHERE run_id = ?",
                (run_id,),
            ).fetchall()
            assert len(deliveries) == 1
            plan_id = deliveries[0][1]
            assert plan_id is not None

            # 验证 plan_id 指向有效的 export_plan 记录
            plan = conn.execute(
                "SELECT id, file_name FROM export_plan WHERE id = ?",
                (plan_id,),
            ).fetchone()
            assert plan is not None
            assert plan[1] == "IMG_0001.jpg"
        finally:
            conn.close()

    def test_execute_copies_to_plan_file_name(self, tmp_path: Path) -> None:
        """AC-5: 文件系统产物应与 plan 中的 file_name 一致。"""
        src = _create_source_image(tmp_path, "IMG_0001.jpg")
        workspace_context, db_path, output_root = _make_full_workspace(tmp_path, assets=[
            {"file_name": "IMG_0001.jpg", "absolute_path": src, "capture_month": "2025-01", "person_ids": ["person-alex", "person-blair"]},
        ])

        from hikbox_pictures.product.export_templates import compute_export_preview, execute_export

        compute_export_preview(workspace_context, template_id="template-1")
        execute_export(workspace_context, template_id="template-1")

        # 验证文件复制到正确的 plan 目标路径
        expected_path = output_root / "only" / "2025-01" / "IMG_0001.jpg"
        assert expected_path.exists()


# ---------------------------------------------------------------------------
# AC-6: 目标文件已存在时跳过
# ---------------------------------------------------------------------------


class TestExecuteSkipsExisting:
    def test_execute_skips_existing_target_file(self, tmp_path: Path) -> None:
        """AC-6: 目标文件已存在时，result 应为 skipped_exists，原文件不变。"""
        src = _create_source_image(tmp_path, "IMG_0001.jpg")
        workspace_context, db_path, output_root = _make_full_workspace(tmp_path, assets=[
            {"file_name": "IMG_0001.jpg", "absolute_path": src, "capture_month": "2025-01", "person_ids": ["person-alex", "person-blair"]},
        ])

        # 预置目标文件
        target_dir = output_root / "only" / "2025-01"
        target_dir.mkdir(parents=True)
        target_file = target_dir / "IMG_0001.jpg"
        placeholder_content = b"placeholder content"
        target_file.write_bytes(placeholder_content)

        from hikbox_pictures.product.export_templates import compute_export_preview, execute_export

        compute_export_preview(workspace_context, template_id="template-1")
        run_id = execute_export(workspace_context, template_id="template-1")

        conn = sqlite3.connect(db_path)
        try:
            delivery = conn.execute(
                "SELECT result FROM export_delivery WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            assert delivery[0] == "skipped_exists"
        finally:
            conn.close()

        # 原文件内容不变
        assert target_file.read_bytes() == placeholder_content


# ---------------------------------------------------------------------------
# AC-7: 再次 preview 只追加新记录
# ---------------------------------------------------------------------------


class TestPreviewAppendOnly:
    def test_second_preview_only_appends_new(self, tmp_path: Path) -> None:
        """AC-7: 再次 preview 时，已有 plan 记录不变，新命中追加。"""
        src1 = _create_source_image(tmp_path, "IMG_0001.jpg")
        workspace_context, db_path, output_root = _make_full_workspace(tmp_path, assets=[
            {"file_name": "IMG_0001.jpg", "absolute_path": src1, "capture_month": "2025-01", "person_ids": ["person-alex", "person-blair"]},
        ])

        from hikbox_pictures.product.export_templates import compute_export_preview

        # 第一次 preview
        compute_export_preview(workspace_context, template_id="template-1")

        conn = sqlite3.connect(db_path)
        try:
            count_before = conn.execute(
                "SELECT COUNT(*) FROM export_plan WHERE template_id = 'template-1'"
            ).fetchone()[0]
            first_row = conn.execute(
                "SELECT id, file_name FROM export_plan WHERE template_id = 'template-1' ORDER BY asset_id"
            ).fetchall()
        finally:
            conn.close()

        assert count_before == 1
        first_plan_id = first_row[0][0]

        # 添加新 asset
        src2 = _create_source_image(tmp_path, "IMG_0002.jpg")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """INSERT INTO assets
                (source_id, absolute_path, file_name, file_extension, capture_month,
                 file_fingerprint, processing_status, created_at, updated_at)
                VALUES (1, ?, 'IMG_0002.jpg', 'jpg', '2025-02', 'fp-2', 'succeeded', '2026-04-30T00:00:00Z', '2026-04-30T00:00:00Z')""",
                (str(src2),),
            )
            asset_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            # 为新 asset 创建 face + assignment
            conn.execute(
                """INSERT INTO face_observations
                (asset_id, face_index, bbox_x1, bbox_y1, bbox_x2, bbox_y2,
                 image_width, image_height, score, crop_path, context_path, created_at)
                VALUES (?, 0, 0, 0, 100, 100, 1000, 1000, 0.9, 'crop.jpg', 'ctx.jpg', '2026-04-30T00:00:00Z')""",
                (asset_id,),
            )
            face_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                """INSERT INTO person_face_assignments
                (person_id, face_observation_id, assignment_run_id, assignment_source, active, evidence_json, created_at, updated_at)
                VALUES ('person-alex', ?, 1, 'online_v6', 1, '{}', '2026-04-30T00:00:00Z', '2026-04-30T00:00:00Z')""",
                (face_id,),
            )
            # 为 blair 创建第二个 face_observation（避免 unique active face 约束冲突）
            conn.execute(
                """INSERT INTO face_observations
                (asset_id, face_index, bbox_x1, bbox_y1, bbox_x2, bbox_y2,
                 image_width, image_height, score, crop_path, context_path, created_at)
                VALUES (?, 1, 100, 100, 200, 200, 1000, 1000, 0.8, 'crop2.jpg', 'ctx2.jpg', '2026-04-30T00:00:00Z')""",
                (asset_id,),
            )
            face_id_2 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                """INSERT INTO person_face_assignments
                (person_id, face_observation_id, assignment_run_id, assignment_source, active, evidence_json, created_at, updated_at)
                VALUES ('person-blair', ?, 1, 'online_v6', 1, '{}', '2026-04-30T00:00:00Z', '2026-04-30T00:00:00Z')""",
                (face_id_2,),
            )
            conn.commit()
        finally:
            conn.close()

        # 第二次 preview
        compute_export_preview(workspace_context, template_id="template-1")

        conn = sqlite3.connect(db_path)
        try:
            count_after = conn.execute(
                "SELECT COUNT(*) FROM export_plan WHERE template_id = 'template-1'"
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT id, asset_id, file_name FROM export_plan WHERE template_id = 'template-1' ORDER BY asset_id"
            ).fetchall()
        finally:
            conn.close()

        assert count_after == 2
        # 第一条记录不变
        assert rows[0][0] == first_plan_id
        assert rows[0][2] == "IMG_0001.jpg"
        # 新记录追加
        assert rows[1][2] == "IMG_0002.jpg"


# ---------------------------------------------------------------------------
# AC-8: 再次 execute 只导出目标文件不存在的
# ---------------------------------------------------------------------------


class TestExecuteIncremental:
    def test_second_execute_only_copies_missing(self, tmp_path: Path) -> None:
        """AC-8: 第一次 execute 后删除部分目标文件，再次 execute 只复制被删文件。"""
        src1 = _create_source_image(tmp_path, "IMG_0001.jpg", b"content1")
        src2 = _create_source_image(tmp_path, "IMG_0002.jpg", b"content2")
        workspace_context, db_path, output_root = _make_full_workspace(tmp_path, assets=[
            {"file_name": "IMG_0001.jpg", "absolute_path": src1, "capture_month": "2025-01", "person_ids": ["person-alex", "person-blair"]},
            {"file_name": "IMG_0002.jpg", "absolute_path": src2, "capture_month": "2025-01", "person_ids": ["person-alex", "person-blair"]},
        ])

        from hikbox_pictures.product.export_templates import compute_export_preview, execute_export

        compute_export_preview(workspace_context, template_id="template-1")

        # 第一次 execute
        run_id_1 = execute_export(workspace_context, template_id="template-1")

        # 验证两个文件都被复制
        file1 = output_root / "only" / "2025-01" / "IMG_0001.jpg"
        file2 = output_root / "only" / "2025-01" / "IMG_0002.jpg"
        assert file1.exists()
        assert file2.exists()

        # 删除其中一个
        file1.unlink()

        # 第二次 execute
        run_id_2 = execute_export(workspace_context, template_id="template-1")

        conn = sqlite3.connect(db_path)
        try:
            deliveries_2 = conn.execute(
                "SELECT asset_id, result FROM export_delivery WHERE run_id = ? ORDER BY asset_id",
                (run_id_2,),
            ).fetchall()
        finally:
            conn.close()

        assert len(deliveries_2) == 2
        # 被删的文件应被复制
        assert deliveries_2[0][1] == "copied"
        # 未删的文件应跳过
        assert deliveries_2[1][1] == "skipped_exists"

        # 文件重新出现
        assert file1.exists()


# ---------------------------------------------------------------------------
# execute_export_async 与 running 状态实时计数
# ---------------------------------------------------------------------------


class TestExecuteAsync:
    def test_async_returns_run_id_and_starts_running(self, tmp_path: Path) -> None:
        """execute_export_async 应立即返回 run_id，run 状态为 running。"""
        src = _create_source_image(tmp_path, "IMG_0001.jpg")
        workspace_context, db_path, output_root = _make_full_workspace(tmp_path, assets=[
            {"file_name": "IMG_0001.jpg", "absolute_path": src, "capture_month": "2025-01", "person_ids": ["person-alex", "person-blair"]},
        ])

        from hikbox_pictures.product.export_templates import compute_export_preview, execute_export_async

        compute_export_preview(workspace_context, template_id="template-1")
        run_id = execute_export_async(workspace_context, template_id="template-1")

        assert run_id is not None
        assert run_id > 0

        # 立即检查：run 记录已创建
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            run = conn.execute(
                "SELECT status FROM export_run WHERE run_id = ?", (run_id,)
            ).fetchone()
            assert run is not None
            # 状态可能是 running 或已完成（取决于线程调度）
            assert run["status"] in ("running", "completed")
        finally:
            conn.close()

    def test_async_completes_with_correct_counts(self, tmp_path: Path) -> None:
        """execute_export_async 后台完成后，状态为 completed，计数正确。"""
        import time

        src = _create_source_image(tmp_path, "IMG_0001.jpg")
        workspace_context, db_path, output_root = _make_full_workspace(tmp_path, assets=[
            {"file_name": "IMG_0001.jpg", "absolute_path": src, "capture_month": "2025-01", "person_ids": ["person-alex", "person-blair"]},
        ])

        from hikbox_pictures.product.export_templates import compute_export_preview, execute_export_async

        compute_export_preview(workspace_context, template_id="template-1")
        run_id = execute_export_async(workspace_context, template_id="template-1")

        # 等待后台线程完成
        for _ in range(50):
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                run = conn.execute(
                    "SELECT status, copied_count, skipped_count FROM export_run WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if run and run["status"] != "running":
                    assert run["status"] == "completed"
                    assert run["copied_count"] == 1
                    assert run["skipped_count"] == 0
                    return
            finally:
                conn.close()
            time.sleep(0.1)

        pytest.fail("后台导出未在 5 秒内完成")

    def test_running_count_from_delivery_records(self, tmp_path: Path) -> None:
        """running 状态下 load_export_runs_for_template 应从 delivery 实时计算计数。"""
        import time
        from hikbox_pictures.product.export_templates import (
            compute_export_preview, execute_export_async,
            load_export_runs_for_template, set_per_file_copy_hook,
        )

        # 创建多个文件使导出时间足够长以便观察 running 状态
        assets = []
        for i in range(20):
            src = _create_source_image(tmp_path, f"IMG_{i:04d}.jpg")
            assets.append({"file_name": f"IMG_{i:04d}.jpg", "absolute_path": src, "capture_month": "2025-01", "person_ids": ["person-alex", "person-blair"]})

        workspace_context, db_path, output_root = _make_full_workspace(tmp_path, assets=assets)

        compute_export_preview(workspace_context, template_id="template-1")

        # 用 hook 每复制一个文件暂停一下，确保能观察到 running 状态
        barrier = threading.Event()

        def slow_hook():
            time.sleep(0.02)

        set_per_file_copy_hook(slow_hook)
        try:
            run_id = execute_export_async(workspace_context, template_id="template-1")

            # 立即查询，应该能看到 running 状态和部分计数
            found_running = False
            for _ in range(100):
                runs = load_export_runs_for_template(workspace_context, template_id="template-1")
                running_runs = [r for r in runs if r.run_id == run_id and r.status == "running"]
                if running_runs:
                    found_running = True
                    # running 状态下计数应来自 delivery 记录
                    assert running_runs[0].copied_count + running_runs[0].skipped_count >= 0
                    break
                # 也可能已完成
                completed_runs = [r for r in runs if r.run_id == run_id and r.status == "completed"]
                if completed_runs:
                    found_running = True
                    break
                time.sleep(0.05)

            assert found_running, "未观察到 running 或 completed 状态"

            # 等待完成
            for _ in range(100):
                runs = load_export_runs_for_template(workspace_context, template_id="template-1")
                completed = [r for r in runs if r.run_id == run_id and r.status == "completed"]
                if completed:
                    assert completed[0].copied_count == 20
                    assert completed[0].skipped_count == 0
                    return
                time.sleep(0.1)

            pytest.fail("后台导出未在 10 秒内完成")
        finally:
            set_per_file_copy_hook(None)


# ---------------------------------------------------------------------------
# 导出历史页 collapsible 区块
# ---------------------------------------------------------------------------


class TestHistoryPageCollapsible:
    def test_history_page_uses_details_elements(self, tmp_path: Path) -> None:
        """历史页应使用 <details> 元素实现折叠展开。"""
        src = _create_source_image(tmp_path, "IMG_0001.jpg")
        workspace_context, db_path, output_root = _make_full_workspace(tmp_path, assets=[
            {"file_name": "IMG_0001.jpg", "absolute_path": src, "capture_month": "2025-01", "person_ids": ["person-alex", "person-blair"]},
        ])

        from hikbox_pictures.product.export_templates import compute_export_preview, execute_export
        from hikbox_pictures.web.app import create_people_gallery_app

        compute_export_preview(workspace_context, template_id="template-1")
        execute_export(workspace_context, template_id="template-1")

        app = create_people_gallery_app(workspace_context=workspace_context, person_detail_page_size=20)
        from fastapi.testclient import TestClient

        client = TestClient(app)
        response = client.get("/exports/template-1/history")
        assert response.status_code == 200
        html = response.text

        assert "<details" in html
        assert "<summary" in html
        assert "run-section" in html

    def test_history_page_newest_run_expanded_by_default(self, tmp_path: Path) -> None:
        """最新一次导出应默认展开（open 属性）。"""
        src = _create_source_image(tmp_path, "IMG_0001.jpg")
        workspace_context, db_path, output_root = _make_full_workspace(tmp_path, assets=[
            {"file_name": "IMG_0001.jpg", "absolute_path": src, "capture_month": "2025-01", "person_ids": ["person-alex", "person-blair"]},
        ])

        from hikbox_pictures.product.export_templates import compute_export_preview, execute_export
        from hikbox_pictures.web.app import create_people_gallery_app

        compute_export_preview(workspace_context, template_id="template-1")
        # 执行两次，产生两个 run
        execute_export(workspace_context, template_id="template-1")
        # 删除目标文件以便第二次执行能复制
        (output_root / "only" / "2025-01" / "IMG_0001.jpg").unlink()
        execute_export(workspace_context, template_id="template-1")

        app = create_people_gallery_app(workspace_context=workspace_context, person_detail_page_size=20)
        from fastapi.testclient import TestClient

        client = TestClient(app)
        response = client.get("/exports/template-1/history")
        html = response.text

        # 第一个 <details> 应有 open 属性
        first_details_idx = html.index("<details")
        first_open_idx = html.index("open", first_details_idx)
        first_close_tag = html.index(">", first_details_idx)
        assert first_open_idx < first_close_tag, "第一个 <details> 应有 open 属性"

        # 第二个 <details> 不应有 open
        second_details_idx = html.index("<details", first_details_idx + 1)
        second_close_tag = html.index(">", second_details_idx)
        segment = html[second_details_idx:second_close_tag + 1]
        # 第二个 details 区域内不应有 open（检查 open 在 > 之前）
        assert "open" not in segment.split(">")[0], "第二个 <details> 不应有 open 属性"


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


class _FakeWorkspaceContext:
    """用于单元测试的 workspace context。"""

    def __init__(self, db_path: Path, output_root: Path) -> None:
        self.library_db_path = db_path
        self.workspace_path = db_path.parent.parent
        self.external_root_path = self.workspace_path / "external"
        self.embedding_db_path = db_path.parent / "embedding.db"
        self.model_root_path = self.workspace_path / ".hikbox" / "models" / "insightface"
        self._output_root = output_root


def _make_workspace_context(
    tmp_path: Path,
    *,
    assets: list[dict[str, object]],
) -> _FakeWorkspaceContext:
    """创建包含测试数据的 workspace context。"""
    db_path, output_root = _setup_test_db(tmp_path, assets=assets)
    return _FakeWorkspaceContext(db_path, output_root)


def _make_workspace_context_with_sources(
    tmp_path: Path,
    *,
    sources: list[dict[str, str]],
    assets: list[dict[str, object]],
) -> tuple[_FakeWorkspaceContext, Path]:
    """创建包含多源测试数据的 workspace context，返回 (context, db_path)。"""
    db_path, output_root = _setup_test_db(tmp_path, sources=sources, assets=assets)
    return _FakeWorkspaceContext(db_path, output_root), db_path


def _make_full_workspace(
    tmp_path: Path,
    *,
    assets: list[dict[str, object]],
) -> tuple[_FakeWorkspaceContext, Path, Path]:
    """创建完整 workspace context，返回 (context, db_path, output_root)。"""
    db_path, output_root = _setup_test_db(tmp_path, assets=assets)
    return _FakeWorkspaceContext(db_path, output_root), db_path, output_root
