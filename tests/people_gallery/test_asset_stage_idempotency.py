from __future__ import annotations

import sys
import threading
from typing import Any
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import numpy as np
import pytest

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_seed_workspace = _MODULE.build_seed_workspace

from hikbox_pictures.ann import AnnIndexStore
from hikbox_pictures.services.asset_stage_runner import AssetStageRunner
from hikbox_pictures.deepface_engine import embedding_to_blob
from hikbox_pictures.db.connection import connect_db
from tests.people_gallery.real_image_helper import copy_raw_face_image


def _write_real_photo(tmp_path: Path, name: str, *, index: int) -> str:
    return str(copy_raw_face_image(tmp_path / name, index=index))


def _count_face_rows(ws, asset_id: int) -> tuple[int, int]:
    observation_row = ws.conn.execute(
        "SELECT COUNT(*) AS c FROM face_observation WHERE photo_asset_id = ? AND active = 1",
        (asset_id,),
    ).fetchone()
    embedding_row = ws.conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM face_embedding e
        JOIN face_observation o ON o.id = e.face_observation_id
        WHERE o.photo_asset_id = ? AND o.active = 1
        """,
        (asset_id,),
    ).fetchone()
    return int(observation_row["c"]), int(embedding_row["c"])


def test_asset_stage_monotonic_and_idempotent(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        session_id = ws.scan_repo.create_session(mode="incremental", status="running", started=True)
        source_id = int(ws.source_repo.list_sources(active=True)[0]["id"])
        session_source_id = ws.scan_repo.create_session_source(session_id, source_id, status="running")

        first_asset_id = ws.asset_repo.add_photo_asset(
            source_id,
            _write_real_photo(tmp_path, "a.jpg", index=0),
            processing_status="discovered",
        )
        second_asset_id = ws.asset_repo.add_photo_asset(
            source_id,
            _write_real_photo(tmp_path, "b.jpg", index=1),
            processing_status="discovered",
        )
        ws.conn.commit()

        runner = AssetStageRunner(ws.conn)

        runner.run_stage(session_source_id, "faces")
        assert ws.asset_repo.get_asset(first_asset_id)["processing_status"] == "discovered"
        assert ws.asset_repo.get_asset(second_asset_id)["processing_status"] == "discovered"

        runner.run_stage(session_source_id, "metadata")
        first = ws.asset_repo.get_asset(first_asset_id)
        second = ws.asset_repo.get_asset(second_asset_id)
        assert first is not None and second is not None
        assert first["processing_status"] == "metadata_done"
        assert second["processing_status"] == "metadata_done"

        runner.run_stage(session_source_id, "metadata")
        first = ws.asset_repo.get_asset(first_asset_id)
        second = ws.asset_repo.get_asset(second_asset_id)
        assert first is not None and second is not None
        assert first["processing_status"] == "metadata_done"
        assert second["processing_status"] == "metadata_done"

        runner.run_stage(session_source_id, "faces")
        first = ws.asset_repo.get_asset(first_asset_id)
        second = ws.asset_repo.get_asset(second_asset_id)
        assert first is not None and second is not None
        assert first["processing_status"] == "faces_done"
        assert second["processing_status"] == "faces_done"

        obs_count, emb_count = _count_face_rows(ws, first_asset_id)
        assert obs_count == 1
        assert emb_count == 0

        runner.run_stage(session_source_id, "faces")
        obs_count, emb_count = _count_face_rows(ws, first_asset_id)
        assert obs_count == 1
        assert emb_count == 0

        runner.run_stage(session_source_id, "embeddings")
        first = ws.asset_repo.get_asset(first_asset_id)
        second = ws.asset_repo.get_asset(second_asset_id)
        assert first is not None and second is not None
        assert first["processing_status"] == "embeddings_done"
        assert second["processing_status"] == "embeddings_done"

        obs_count, emb_count = _count_face_rows(ws, first_asset_id)
        assert obs_count == 1
        assert emb_count == 1

        runner.run_stage(session_source_id, "embeddings")
        obs_count, emb_count = _count_face_rows(ws, first_asset_id)
        assert obs_count == 1
        assert emb_count == 1

        runner.run_stage(session_source_id, "assignment")
        first = ws.asset_repo.get_asset(first_asset_id)
        second = ws.asset_repo.get_asset(second_asset_id)
        assert first is not None and second is not None
        assert first["processing_status"] == "assignment_done"
        assert second["processing_status"] == "assignment_done"

        runner.run_stage(session_source_id, "assignment")
        first = ws.asset_repo.get_asset(first_asset_id)
        second = ws.asset_repo.get_asset(second_asset_id)
        assert first is not None and second is not None
        assert first["processing_status"] == "assignment_done"
        assert second["processing_status"] == "assignment_done"
    finally:
        ws.close()


def test_run_stage_rollback_when_mid_stage_failed(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        session_id = ws.scan_repo.create_session(mode="incremental", status="running", started=True)
        source_id = int(ws.source_repo.list_sources(active=True)[0]["id"])
        session_source_id = ws.scan_repo.create_session_source(session_id, source_id, status="running")

        first_asset_id = ws.asset_repo.add_photo_asset(
            source_id,
            _write_real_photo(tmp_path, "r-a.jpg", index=0),
            processing_status="discovered",
        )
        second_asset_id = ws.asset_repo.add_photo_asset(
            source_id,
            _write_real_photo(tmp_path, "r-b.jpg", index=1),
            processing_status="discovered",
        )
        ws.conn.commit()

        runner = AssetStageRunner(ws.conn)
        runner.run_stage(session_source_id, "metadata")

        class _FailOnSecondFaceRunner(AssetStageRunner):
            def __init__(self, conn):
                super().__init__(conn)
                self._calls = 0

            def _run_faces_stage(self, asset_id: int, scan_session_id: int) -> None:
                self._calls += 1
                if self._calls == 2:
                    raise RuntimeError("faces 阶段注入异常")
                super()._run_faces_stage(asset_id, scan_session_id)

        failing_runner = _FailOnSecondFaceRunner(ws.conn)
        with pytest.raises(RuntimeError, match="faces 阶段注入异常"):
            failing_runner.run_stage(session_source_id, "faces")

        first = ws.asset_repo.get_asset(first_asset_id)
        second = ws.asset_repo.get_asset(second_asset_id)
        assert first is not None and second is not None
        assert first["processing_status"] == "faces_done"
        assert second["processing_status"] == "metadata_done"

        first_obs_count, first_emb_count = _count_face_rows(ws, first_asset_id)
        second_obs_count, second_emb_count = _count_face_rows(ws, second_asset_id)
        assert first_obs_count >= 1
        assert second_obs_count == 0
        assert first_emb_count == 0
        assert second_emb_count == 0

        source_state = ws.scan_repo.get_session_source(session_source_id)
        assert source_state is not None
        assert source_state["discovered_count"] == 2
        assert source_state["metadata_done_count"] == 2
        assert source_state["faces_done_count"] == 1
        assert source_state["embeddings_done_count"] == 0
        assert source_state["assignment_done_count"] == 0
    finally:
        ws.close()


def test_ensure_face_embedding_repeat_write_keeps_stable_id(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        source_id = int(ws.source_repo.list_sources(active=True)[0]["id"])
        first_asset_id = ws.asset_repo.add_photo_asset(source_id, "/tmp/e-a.jpg", processing_status="faces_done")
        second_asset_id = ws.asset_repo.add_photo_asset(source_id, "/tmp/e-b.jpg", processing_status="faces_done")
        ws.conn.commit()

        first_observation_id = ws.asset_repo.ensure_face_observation(first_asset_id)
        second_observation_id = ws.asset_repo.ensure_face_observation(second_asset_id)

        first_embedding_id = ws.asset_repo.ensure_face_embedding(
            first_observation_id,
            vector_blob=embedding_to_blob(np.asarray([1.0, 0.0, 0.0, 1.0], dtype=np.float32)),
            dimension=4,
        )
        _ = ws.asset_repo.ensure_face_embedding(
            second_observation_id,
            vector_blob=embedding_to_blob(np.asarray([0.0, 1.0, 1.0, 0.0], dtype=np.float32)),
            dimension=4,
        )
        repeated_embedding_id = ws.asset_repo.ensure_face_embedding(
            first_observation_id,
            vector_blob=embedding_to_blob(np.asarray([1.0, 0.0, 0.0, 1.0], dtype=np.float32)),
            dimension=4,
        )

        assert repeated_embedding_id == first_embedding_id
        row = ws.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM face_embedding
            WHERE face_observation_id = ?
              AND feature_type = 'face'
            """,
            (first_observation_id,),
        ).fetchone()
        assert row is not None
        assert int(row["c"]) == 1
    finally:
        ws.close()


def test_concurrent_assignment_stage_is_idempotent(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        session_id = ws.scan_repo.create_session(mode="incremental", status="running", started=True)
        source_id = int(ws.source_repo.list_sources(active=True)[0]["id"])
        session_source_id = ws.scan_repo.create_session_source(session_id, source_id, status="running")
        asset_id = ws.asset_repo.add_photo_asset(
            source_id,
            _write_real_photo(tmp_path, "concurrent-a.jpg", index=0),
            processing_status="discovered",
        )
        ws.conn.commit()

        runner = AssetStageRunner(ws.conn)
        runner.run_stage(session_source_id, "metadata")
        runner.run_stage(session_source_id, "faces")
        runner.run_stage(session_source_id, "embeddings")
        observation_row = ws.conn.execute(
            """
            SELECT e.vector_blob
            FROM face_embedding AS e
            JOIN face_observation AS o
              ON o.id = e.face_observation_id
            WHERE o.photo_asset_id = ?
              AND o.active = 1
              AND e.feature_type = 'face'
            ORDER BY e.id ASC
            LIMIT 1
            """,
            (asset_id,),
        ).fetchone()
        assert observation_row is not None
        vector_blob = observation_row["vector_blob"]
        assert isinstance(vector_blob, (bytes, bytearray, memoryview))
        ws.person_repo.replace_centroid_prototype(
            person_id=1,
            vector_blob=bytes(vector_blob),
            model_key="MockArcFace@retinaface",
        )
        ann_store = AnnIndexStore(ws.paths.artifacts_dir / "ann" / "prototype_index.npz")
        ann_store.rebuild_from_prototypes(
            ws.person_repo.list_active_prototypes(
                prototype_type="centroid",
                model_key="MockArcFace@retinaface",
            )
        )
        ws.conn.commit()

        err_a: list[str] = []
        err_b: list[str] = []

        def _run_assignment(bucket: list[str]) -> None:
            conn = connect_db(ws.paths.db_path)
            try:
                AssetStageRunner(conn).run_stage(session_source_id, "assignment")
            except Exception as exc:  # pragma: no cover - 失败分支由断言兜底
                bucket.append(str(exc))
            finally:
                conn.close()

        t_a = threading.Thread(target=_run_assignment, args=(err_a,))
        t_b = threading.Thread(target=_run_assignment, args=(err_b,))
        t_a.start()
        t_b.start()
        t_a.join()
        t_b.join()

        assert err_a == []
        assert err_b == []

        row = ws.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM person_face_assignment p
            JOIN face_observation o ON o.id = p.face_observation_id
            WHERE o.photo_asset_id = ?
              AND p.active = 1
            """,
            (asset_id,),
        ).fetchone()
        assert row is not None
        assert int(row["c"]) == 1
    finally:
        ws.close()


def test_run_stage_rejects_existing_outer_transaction(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        session_id = ws.scan_repo.create_session(mode="incremental", status="running", started=True)
        source_id = int(ws.source_repo.list_sources(active=True)[0]["id"])
        session_source_id = ws.scan_repo.create_session_source(session_id, source_id, status="running")
        asset_id = ws.asset_repo.add_photo_asset(
            source_id,
            _write_real_photo(tmp_path, "tx-reject.jpg", index=0),
            processing_status="discovered",
        )
        ws.conn.commit()

        ws.conn.execute("BEGIN")
        try:
            with pytest.raises(RuntimeError, match="不支持在外部事务中调用"):
                AssetStageRunner(ws.conn).run_stage(session_source_id, "metadata")
        finally:
            ws.conn.rollback()

        asset = ws.asset_repo.get_asset(asset_id)
        assert asset is not None
        assert asset["processing_status"] == "discovered"
    finally:
        ws.close()


def test_run_stage_rolls_back_when_commit_failed(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        session_id = ws.scan_repo.create_session(mode="incremental", status="running", started=True)
        source_id = int(ws.source_repo.list_sources(active=True)[0]["id"])
        session_source_id = ws.scan_repo.create_session_source(session_id, source_id, status="running")
        asset_id = ws.asset_repo.add_photo_asset(
            source_id,
            _write_real_photo(tmp_path, "commit-fail.jpg", index=0),
            processing_status="discovered",
        )
        ws.conn.commit()

        class _CommitFailConn:
            def __init__(self, conn: Any) -> None:
                self._conn = conn

            @property
            def in_transaction(self) -> bool:
                return bool(self._conn.in_transaction)

            def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
                return self._conn.execute(sql, params)

            def rollback(self) -> None:
                self._conn.rollback()

            def commit(self) -> None:
                raise RuntimeError("注入 commit 失败")

        failing_runner = AssetStageRunner(_CommitFailConn(ws.conn))  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="注入 commit 失败"):
            failing_runner.run_stage(session_source_id, "metadata")

        asset = ws.asset_repo.get_asset(asset_id)
        assert asset is not None
        assert asset["processing_status"] == "discovered"
    finally:
        ws.close()


def test_concurrent_embeddings_stage_is_idempotent(tmp_path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        session_id = ws.scan_repo.create_session(mode="incremental", status="running", started=True)
        source_id = int(ws.source_repo.list_sources(active=True)[0]["id"])
        session_source_id = ws.scan_repo.create_session_source(session_id, source_id, status="running")
        asset_id = ws.asset_repo.add_photo_asset(
            source_id,
            _write_real_photo(tmp_path, "concurrent-embedding.jpg", index=0),
            processing_status="discovered",
        )
        ws.conn.commit()

        runner = AssetStageRunner(ws.conn)
        runner.run_stage(session_source_id, "metadata")
        runner.run_stage(session_source_id, "faces")

        err_a: list[str] = []
        err_b: list[str] = []

        def _run_embeddings(bucket: list[str]) -> None:
            conn = connect_db(ws.paths.db_path)
            try:
                AssetStageRunner(conn).run_stage(session_source_id, "embeddings")
            except Exception as exc:  # pragma: no cover - 失败分支由断言兜底
                bucket.append(str(exc))
            finally:
                conn.close()

        t_a = threading.Thread(target=_run_embeddings, args=(err_a,))
        t_b = threading.Thread(target=_run_embeddings, args=(err_b,))
        t_a.start()
        t_b.start()
        t_a.join()
        t_b.join()

        assert err_a == []
        assert err_b == []

        obs_row = ws.conn.execute(
            "SELECT id FROM face_observation WHERE photo_asset_id = ? AND active = 1 ORDER BY id ASC LIMIT 1",
            (asset_id,),
        ).fetchone()
        assert obs_row is not None
        obs_id = int(obs_row["id"])
        emb_row = ws.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM face_embedding
            WHERE face_observation_id = ?
              AND feature_type = 'face'
            """,
            (obs_id,),
        ).fetchone()
        assert emb_row is not None
        assert int(emb_row["c"]) == 1
    finally:
        ws.close()
