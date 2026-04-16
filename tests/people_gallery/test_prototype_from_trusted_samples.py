from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import numpy as np

from hikbox_pictures.ann import AnnIndexStore
from hikbox_pictures.deepface_engine import embedding_to_blob
from hikbox_pictures.repositories.person_repo import PersonRepo
from hikbox_pictures.services.prototype_service import PrototypeService

_FIXTURE_PATH = Path(__file__).with_name("fixtures_workspace.py")
_SPEC = spec_from_file_location("people_gallery_fixtures_workspace_prototype_from_trusted", _FIXTURE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"无法加载测试夹具文件: {_FIXTURE_PATH}")
_MODULE = module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
build_identity_seed_workspace = _MODULE.build_identity_seed_workspace


def test_rebuild_person_prototype_reads_person_trusted_sample_only(tmp_path: Path) -> None:
    ws = build_identity_seed_workspace(tmp_path)
    try:
        person_id = ws.person_repo.create_person("样本来源校验", confirmed=True, ignored=False)
        obs_trusted = ws.insert_observation_with_embedding(
            vector=[1.0, 0.0, 0.0, 0.0],
            quality_score=0.95,
            photo_label="trusted-photo",
        )
        obs_assignment_only = ws.insert_observation_with_embedding(
            vector=[0.0, 1.0, 0.0, 0.0],
            quality_score=0.92,
            photo_label="assignment-photo",
        )
        ws.person_repo.create_bootstrap_assignment(
            person_id=person_id,
            face_observation_id=obs_trusted["observation_id"],
            threshold_profile_id=ws.profile_id,
            diagnostic_json='{"decision_kind":"test_seed"}',
        )
        ws.person_repo.create_bootstrap_assignment(
            person_id=person_id,
            face_observation_id=obs_assignment_only["observation_id"],
            threshold_profile_id=ws.profile_id,
            diagnostic_json='{"decision_kind":"test_assignment_only"}',
        )
        ws.person_repo.create_trusted_sample(
            person_id=person_id,
            face_observation_id=obs_trusted["observation_id"],
            trust_source="bootstrap_seed",
            trust_score=1.0,
            quality_score_snapshot=0.95,
            threshold_profile_id=ws.profile_id,
            source_auto_cluster_id=None,
        )
        ws.conn.commit()

        service = PrototypeService(
            ws.conn,
            PersonRepo(ws.conn),
            AnnIndexStore(ws.paths.artifacts_dir / "ann" / "prototype_index.npz"),
        )
        rebuilt = service.rebuild_person_prototype(person_id=person_id, model_key=ws.model_key)

        assert rebuilt is True
        row = ws.conn.execute(
            """
            SELECT vector_blob, quality_score
            FROM person_prototype
            WHERE person_id = ?
              AND prototype_type = 'centroid'
              AND model_key = ?
              AND active = 1
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(person_id), ws.model_key),
        ).fetchone()
        assert row is not None
        assert float(row["quality_score"]) == 1.0
        vector = np.frombuffer(row["vector_blob"], dtype=np.float32).copy()
        expected = np.frombuffer(embedding_to_blob(np.asarray([1.0, 0.0, 0.0, 0.0], dtype=np.float32)), dtype=np.float32)
        assert np.allclose(vector, expected)
        assert np.allclose(vector, np.asarray([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
    finally:
        ws.close()
