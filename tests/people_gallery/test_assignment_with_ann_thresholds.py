from __future__ import annotations

import json
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import numpy as np

from hikbox_pictures.ann import AnnIndexStore
from hikbox_pictures.deepface_engine import embedding_to_blob
from hikbox_pictures.services.asset_stage_runner import AssetStageRunner

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace_assignment_thresholds", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_seed_workspace = _MODULE.build_seed_workspace

_MODEL_KEY = "MockArcFace@retinaface"


def _insert_observation(conn, asset_id: int) -> int:
    cursor = conn.execute(
        """
        INSERT INTO face_observation(
            photo_asset_id,
            bbox_top,
            bbox_right,
            bbox_bottom,
            bbox_left,
            face_area_ratio,
            detector_key,
            detector_version,
            active
        )
        VALUES (?, 0.1, 0.9, 0.9, 0.1, 0.22, 'retinaface', 'MockArcFace', 1)
        """,
        (int(asset_id),),
    )
    return int(cursor.lastrowid)


def _insert_embedding(conn, observation_id: int, values: list[float]) -> None:
    vector = np.asarray(values, dtype=np.float32)
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
        (
            int(observation_id),
            _MODEL_KEY,
            int(vector.size),
            embedding_to_blob(vector),
        ),
    )


def test_assignment_stage_routes_to_auto_review_and_new_person(tmp_path: Path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        source_id = int(ws.source_repo.list_sources(active=True)[0]["id"])
        asset_id = ws.asset_repo.add_photo_asset(
            source_id,
            str((tmp_path / "assignment-thresholds.jpg").resolve()),
            processing_status="embeddings_done",
        )

        observation_auto = _insert_observation(ws.conn, asset_id)
        observation_review = _insert_observation(ws.conn, asset_id)
        observation_new = _insert_observation(ws.conn, asset_id)
        observation_locked = _insert_observation(ws.conn, asset_id)

        _insert_embedding(ws.conn, observation_auto, [0.02, 0.0, 0.0, 0.0])
        _insert_embedding(ws.conn, observation_review, [0.30, 0.0, 0.0, 0.0])
        _insert_embedding(ws.conn, observation_new, [0.60, 0.80, 0.0, 0.0])
        _insert_embedding(ws.conn, observation_locked, [0.01, 0.0, 0.0, 0.0])

        ws.person_repo.replace_centroid_prototype(
            person_id=1,
            vector_blob=embedding_to_blob(np.asarray([0.0, 0.0, 0.0, 0.0], dtype=np.float32)),
            model_key=_MODEL_KEY,
        )
        ws.person_repo.replace_centroid_prototype(
            person_id=2,
            vector_blob=embedding_to_blob(np.asarray([1.0, 0.0, 0.0, 0.0], dtype=np.float32)),
            model_key=_MODEL_KEY,
        )
        ws.asset_repo.create_assignment(
            person_id=2,
            face_observation_id=observation_locked,
            assignment_source="manual",
            confidence=1.0,
            locked=True,
        )

        ann_store = AnnIndexStore(ws.paths.artifacts_dir / "ann" / "prototype_index.npz")
        ann_store.rebuild_from_prototypes(
            ws.person_repo.list_active_prototypes(
                prototype_type="centroid",
                model_key=_MODEL_KEY,
            )
        )
        ws.conn.commit()

        session_id = ws.scan_repo.create_session(mode="incremental", status="running", started=True)
        session_source_id = ws.scan_repo.create_session_source(session_id, source_id, status="running")
        ws.conn.commit()

        result = AssetStageRunner(ws.conn).run_stage(session_source_id, "assignment")

        auto_assignment = ws.asset_repo.get_active_assignment_for_observation(observation_auto)
        locked_assignment = ws.asset_repo.get_active_assignment_for_observation(observation_locked)
        review_item = ws.conn.execute(
            """
            SELECT review_type, payload_json
            FROM review_item
            WHERE face_observation_id = ?
              AND status = 'open'
            ORDER BY id DESC
            LIMIT 1
            """,
            (observation_review,),
        ).fetchone()
        new_person_item = ws.conn.execute(
            """
            SELECT review_type, payload_json
            FROM review_item
            WHERE face_observation_id = ?
              AND status = 'open'
            ORDER BY id DESC
            LIMIT 1
            """,
            (observation_new,),
        ).fetchone()
        review_assignment = ws.asset_repo.get_active_assignment_for_observation(observation_review)
        new_person_assignment = ws.asset_repo.get_active_assignment_for_observation(observation_new)

        assert result["assignment_done_count"] >= 1
        assert auto_assignment is not None
        assert int(auto_assignment["person_id"]) == 1
        assert auto_assignment["assignment_source"] == "auto"
        assert locked_assignment is not None
        assert int(locked_assignment["person_id"]) == 2
        assert int(locked_assignment["locked"]) == 1
        assert review_item is not None
        assert review_item["review_type"] == "low_confidence_assignment"
        assert json.loads(str(review_item["payload_json"]))["candidates"][0]["person_id"] == 1
        assert new_person_item is not None
        assert new_person_item["review_type"] == "new_person"
        assert review_assignment is None
        assert new_person_assignment is None
        asset = ws.asset_repo.get_asset(asset_id)
        assert asset is not None
        assert asset["processing_status"] == "assignment_done"
    finally:
        ws.close()


def test_assignment_stage_survives_stale_ann_dimension_and_rebuilds_index(tmp_path: Path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        source_id = int(ws.source_repo.list_sources(active=True)[0]["id"])
        asset_id = ws.asset_repo.add_photo_asset(
            source_id,
            str((tmp_path / "assignment-stale-index.jpg").resolve()),
            processing_status="embeddings_done",
        )
        observation_id = _insert_observation(ws.conn, asset_id)
        _insert_embedding(ws.conn, observation_id, [0.01, 0.0, 0.0, 0.0])
        ws.person_repo.replace_centroid_prototype(
            person_id=1,
            vector_blob=embedding_to_blob(np.asarray([0.0, 0.0, 0.0, 0.0], dtype=np.float32)),
            model_key=_MODEL_KEY,
        )

        # 先写入错误维度的旧索引，模拟模型切换后遗留 artifact。
        stale_ann_store = AnnIndexStore(ws.paths.artifacts_dir / "ann" / "prototype_index.npz")
        stale_ann_store.rebuild_from_prototypes(
            [
                {
                    "person_id": 99,
                    "vector_blob": embedding_to_blob(np.asarray([0.0, 0.0], dtype=np.float32)),
                }
            ]
        )
        ws.conn.commit()

        session_id = ws.scan_repo.create_session(mode="incremental", status="running", started=True)
        session_source_id = ws.scan_repo.create_session_source(session_id, source_id, status="running")
        ws.conn.commit()

        result = AssetStageRunner(ws.conn).run_stage(session_source_id, "assignment")

        assignment = ws.asset_repo.get_active_assignment_for_observation(observation_id)
        reloaded_ann_store = AnnIndexStore(ws.paths.artifacts_dir / "ann" / "prototype_index.npz")
        recalled = reloaded_ann_store.search(np.asarray([0.0, 0.0, 0.0, 0.0], dtype=np.float32), 1)

        assert result["assignment_done_count"] >= 1
        assert assignment is not None
        assert int(assignment["person_id"]) == 1
        assert assignment["assignment_source"] == "auto"
        assert recalled
        assert int(recalled[0][0]) == 1
    finally:
        ws.close()


def test_assignment_stage_skips_excluded_person_candidates(tmp_path: Path) -> None:
    ws = build_seed_workspace(tmp_path)
    try:
        source_id = int(ws.source_repo.list_sources(active=True)[0]["id"])
        asset_id = ws.asset_repo.add_photo_asset(
            source_id,
            str((tmp_path / "assignment-excluded-person.jpg").resolve()),
            processing_status="embeddings_done",
        )
        observation_id = _insert_observation(ws.conn, asset_id)
        _insert_embedding(ws.conn, observation_id, [0.01, 0.0, 0.0, 0.0])
        ws.person_repo.replace_centroid_prototype(
            person_id=1,
            vector_blob=embedding_to_blob(np.asarray([0.0, 0.0, 0.0, 0.0], dtype=np.float32)),
            model_key=_MODEL_KEY,
        )
        ws.asset_repo.upsert_assignment_exclusion(
            person_id=1,
            face_observation_id=observation_id,
            assignment_id=None,
            reason="manual_exclude",
        )

        ann_store = AnnIndexStore(ws.paths.artifacts_dir / "ann" / "prototype_index.npz")
        ann_store.rebuild_from_prototypes(
            ws.person_repo.list_active_prototypes(
                prototype_type="centroid",
                model_key=_MODEL_KEY,
            )
        )
        ws.conn.commit()

        session_id = ws.scan_repo.create_session(mode="incremental", status="running", started=True)
        session_source_id = ws.scan_repo.create_session_source(session_id, source_id, status="running")
        ws.conn.commit()

        result = AssetStageRunner(ws.conn).run_stage(session_source_id, "assignment")

        assignment = ws.asset_repo.get_active_assignment_for_observation(observation_id)
        review_item = ws.conn.execute(
            """
            SELECT review_type, payload_json
            FROM review_item
            WHERE face_observation_id = ?
              AND status = 'open'
            ORDER BY id DESC
            LIMIT 1
            """,
            (observation_id,),
        ).fetchone()

        assert result["assignment_done_count"] >= 1
        assert assignment is None
        assert review_item is not None
        assert review_item["review_type"] == "new_person"
        assert json.loads(str(review_item["payload_json"]))["candidates"] == []
    finally:
        ws.close()
