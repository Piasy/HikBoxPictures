import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import sqlite3
from pathlib import Path

from hikbox_pictures.product.scan.assignment_stage import AssignmentStageService
from hikbox_pictures.product.scan.execution_service import DetectStageRunResult, ScanExecutionService
from hikbox_pictures.product.scan.session_service import ScanSessionRepository
from hikbox_pictures.product.scan.cluster_repository import ClusterRepository
from hikbox_pictures.product.scan.incremental_assignment_service import IncrementalAssignmentService
from hikbox_pictures.product.source.repository import SourceRepository
from hikbox_pictures.product.source.service import SourceService

from tests.product.task6_test_support import (
    create_task6_workspace,
    embedding_from_seed,
    fake_embedding_calculator_from_map,
    seed_face_observations,
    upsert_face_embeddings,
)


def test_scan_incremental_updates_existing_people_without_full_person_rebuild(tmp_path: Path, monkeypatch) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    observation_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (220, 180, 160), "quality_score": 0.92},
            {"asset_index": 0, "color": (218, 178, 158), "quality_score": 0.90},
        ],
    )
    shared_main = embedding_from_seed(601)
    calculator = fake_embedding_calculator_from_map(
        {
            "f1.png": (shared_main, embedding_from_seed(701), 1.1),
            "f2.png": (shared_main, embedding_from_seed(702), 1.1),
        }
    )

    def fake_full_run(*, faces, params):
        return {
            "faces": [
                {
                    "face_observation_id": observation_ids[0],
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "assignment_source": "hdbscan",
                    "probability": 0.95,
                },
                {
                    "face_observation_id": observation_ids[1],
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "assignment_source": "hdbscan",
                    "probability": 0.94,
                },
            ],
            "persons": [{"person_temp_key": "p0", "face_observation_ids": observation_ids}],
            "clusters": [
                {
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "member_face_observation_ids": observation_ids,
                    "representative_face_observation_ids": [observation_ids[0]],
                }
            ],
            "stats": {"person_count": 1, "assignment_count": 2},
        }

    monkeypatch.setattr("hikbox_pictures.product.scan.assignment_stage.run_frozen_v5_assignment", fake_full_run)
    stage = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    )
    baseline = stage.run_frozen_v5_assignment(
        scan_session_id=session_id,
        run_kind="scan_full",
        embedding_calculator=calculator,
    )
    assert baseline.assignment_count == 2
    ScanSessionRepository(layout.library_db).update_status(session_id, status="completed")

    incremental_session = ScanSessionRepository(layout.library_db).create_session(
        run_kind="scan_incremental",
        status="running",
        triggered_by="manual_cli",
    )
    conn = sqlite3.connect(layout.library_db)
    try:
        source_id = int(conn.execute("SELECT id FROM library_source ORDER BY id ASC LIMIT 1").fetchone()[0])
        conn.execute(
            """
            INSERT INTO scan_session_source(
              scan_session_id, library_source_id, stage_status_json, processed_assets, failed_assets, updated_at
            ) VALUES (?, ?, '{"discover":"done","metadata":"done","detect":"done","embed":"pending","cluster":"pending","assignment":"pending"}', 3, 0, CURRENT_TIMESTAMP)
            """,
            (incremental_session.id, source_id),
        )
        conn.commit()
    finally:
        conn.close()

    new_face_id = seed_face_observations(
        layout.library_db,
        runtime_root,
        [{"asset_index": 1, "color": (222, 182, 162), "quality_score": 0.91}],
    )[0]
    upsert_face_embeddings(
        layout.embedding_db,
        face_observation_id=new_face_id,
        main=shared_main,
        flip=embedding_from_seed(703),
    )

    service = ScanExecutionService(db_path=layout.library_db, output_root=runtime_root)

    monkeypatch.setattr(service._discover_service, "run", lambda scan_session_id: None)
    monkeypatch.setattr(service._metadata_service, "run", lambda scan_session_id: None)
    monkeypatch.setattr(
        ScanExecutionService,
        "run_detect_stage",
        lambda self, scan_session_id, runtime_defaults=None, detector=None: DetectStageRunResult(
            claimed_batches=0,
            acked_batches=0,
            interrupted=False,
        ),
    )

    result = service.run_session(
        scan_session_id=incremental_session.id,
        embedding_calculator=calculator,
    )

    repo = ClusterRepository(layout.library_db)
    clusters = repo.list_active_clusters()
    conn = sqlite3.connect(layout.library_db)
    try:
        person_count = int(conn.execute("SELECT COUNT(*) FROM person WHERE status='active'").fetchone()[0])
        assigned_person = int(
            conn.execute(
                "SELECT person_id FROM person_face_assignment WHERE face_observation_id=? AND active=1",
                (new_face_id,),
            ).fetchone()[0]
        )
        existing_person = int(
            conn.execute("SELECT person_id FROM person_face_assignment WHERE face_observation_id=? AND active=1", (observation_ids[0],)).fetchone()[0]
        )
    finally:
        conn.close()

    assert result.assignment_run_id > baseline.assignment_run_id
    assert person_count == 1
    assert assigned_person == existing_person
    assert len(clusters) == 1
    assert clusters[0].member_count == 3


def test_scan_incremental_snapshot_mismatch_uses_true_full_rebuild_scope(tmp_path: Path, monkeypatch) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    extra_source_root = tmp_path / "source-b"
    extra_source_root.mkdir(parents=True, exist_ok=True)
    (extra_source_root / "img_c.jpg").write_bytes(b"task6-source-b")
    extra_source = SourceService(SourceRepository(layout.library_db)).add_source(str(extra_source_root), label="src-b")

    conn = sqlite3.connect(layout.library_db)
    try:
        conn.execute(
            """
            INSERT INTO photo_asset(
              library_source_id, primary_path, primary_fingerprint, fingerprint_algo, file_size, mtime_ns, asset_status,
              created_at, updated_at
            ) VALUES (?, 'img_c.jpg', 'fp-c', 'sha256', 101, 201, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (extra_source.id,),
        )
        conn.execute(
            """
            INSERT INTO scan_session_source(
              scan_session_id, library_source_id, stage_status_json, processed_assets, failed_assets, updated_at
            ) VALUES (?, ?, '{"discover":"done","metadata":"done","detect":"done","embed":"pending","cluster":"pending","assignment":"pending"}', 1, 0, CURRENT_TIMESTAMP)
            """,
            (session_id, extra_source.id),
        )
        conn.commit()
    finally:
        conn.close()

    observation_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (220, 180, 160), "quality_score": 0.92},
            {"asset_index": 2, "color": (218, 178, 158), "quality_score": 0.91},
        ],
    )
    shared_main = embedding_from_seed(1601)
    calculator = fake_embedding_calculator_from_map(
        {
            "f1.png": (shared_main, embedding_from_seed(1701), 1.1),
            "f2.png": (shared_main, embedding_from_seed(1702), 1.1),
        }
    )
    seen_face_ids: list[list[int]] = []

    def fake_full_run(*, faces, params):
        seen_face_ids.append(sorted(int(face["face_observation_id"]) for face in faces))
        return {
            "faces": [
                {
                    "face_observation_id": observation_ids[0],
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "assignment_source": "hdbscan",
                    "probability": 0.95,
                },
                {
                    "face_observation_id": observation_ids[1],
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "assignment_source": "hdbscan",
                    "probability": 0.94,
                },
            ],
            "persons": [{"person_temp_key": "p0", "face_observation_ids": observation_ids}],
            "clusters": [
                {
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "member_face_observation_ids": observation_ids,
                    "representative_face_observation_ids": [observation_ids[0]],
                }
            ],
            "stats": {"person_count": 1, "assignment_count": 2},
        }

    monkeypatch.setattr("hikbox_pictures.product.scan.assignment_stage.run_frozen_v5_assignment", fake_full_run)
    stage = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    )
    baseline = stage.run_frozen_v5_assignment(
        scan_session_id=session_id,
        run_kind="scan_full",
        embedding_calculator=calculator,
    )
    assert baseline.assignment_count == 2
    assert seen_face_ids == [sorted(observation_ids)]
    ScanSessionRepository(layout.library_db).update_status(session_id, status="completed")

    incremental_session = ScanSessionRepository(layout.library_db).create_session(
        run_kind="scan_incremental",
        status="running",
        triggered_by="manual_cli",
    )
    conn = sqlite3.connect(layout.library_db)
    try:
        conn.execute(
            """
            INSERT INTO scan_session_source(
              scan_session_id, library_source_id, stage_status_json, processed_assets, failed_assets, updated_at
            ) VALUES (?, ?, '{"discover":"done","metadata":"done","detect":"done","embed":"pending","cluster":"pending","assignment":"pending"}', 1, 0, CURRENT_TIMESTAMP)
            """,
            (incremental_session.id, extra_source.id),
        )
        conn.commit()
    finally:
        conn.close()

    service = ScanExecutionService(db_path=layout.library_db, output_root=runtime_root)
    monkeypatch.setattr(service._discover_service, "run", lambda scan_session_id: None)
    monkeypatch.setattr(service._metadata_service, "run", lambda scan_session_id: None)
    monkeypatch.setattr(
        ScanExecutionService,
        "run_detect_stage",
        lambda self, scan_session_id, runtime_defaults=None, detector=None: DetectStageRunResult(
            claimed_batches=0,
            acked_batches=0,
            interrupted=False,
        ),
    )
    monkeypatch.setattr(
        "hikbox_pictures.product.scan.assignment_stage.build_frozen_v5_param_snapshot",
        lambda: {"algorithm_version": "frozen_v5", "review_override": "mismatch"},
    )

    result = service.run_session(
        scan_session_id=incremental_session.id,
        embedding_calculator=calculator,
    )

    repo = ClusterRepository(layout.library_db)
    active_clusters = repo.list_active_clusters()
    conn = sqlite3.connect(layout.library_db)
    try:
        active_person_count = int(conn.execute("SELECT COUNT(*) FROM person WHERE status='active'").fetchone()[0])
        active_assignment_count = int(conn.execute("SELECT COUNT(*) FROM person_face_assignment WHERE active=1").fetchone()[0])
        fallback_run_kind = str(
            conn.execute(
                "SELECT run_kind FROM assignment_run WHERE id=?",
                (result.assignment_run_id,),
            ).fetchone()[0]
        )
    finally:
        conn.close()

    assert result.assignment_run_id > baseline.assignment_run_id
    assert seen_face_ids[-1] == sorted(observation_ids)
    assert active_person_count == 1
    assert active_assignment_count == 2
    assert fallback_run_kind == "scan_full"
    assert len(active_clusters) == 1
    assert active_clusters[0].member_count == 2


def test_missing_asset_faces_are_excluded_from_assignment_inputs(tmp_path: Path, monkeypatch) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    observation_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (220, 180, 160), "quality_score": 0.92},
            {"asset_index": 1, "color": (218, 178, 158), "quality_score": 0.91},
        ],
    )
    calculator = fake_embedding_calculator_from_map(
        {
            "f1.png": (embedding_from_seed(1801), embedding_from_seed(1901), 1.1),
            "f2.png": (embedding_from_seed(1802), embedding_from_seed(1902), 1.1),
        }
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        conn.execute(
            """
            UPDATE photo_asset
            SET asset_status='missing', updated_at=CURRENT_TIMESTAMP
            WHERE id=(SELECT photo_asset_id FROM face_observation WHERE id=?)
            """,
            (observation_ids[0],),
        )
        conn.commit()
    finally:
        conn.close()

    captured_face_ids: list[int] = []

    def fake_full_run(*, faces, params):
        captured_face_ids.extend(sorted(int(face["face_observation_id"]) for face in faces))
        return {
            "faces": [
                {
                    "face_observation_id": observation_ids[1],
                    "cluster_label": 20,
                    "person_temp_key": "p1",
                    "assignment_source": "hdbscan",
                    "probability": 0.95,
                }
            ],
            "persons": [{"person_temp_key": "p1", "face_observation_ids": [observation_ids[1]]}],
            "clusters": [
                {
                    "cluster_label": 20,
                    "person_temp_key": "p1",
                    "member_face_observation_ids": [observation_ids[1]],
                    "representative_face_observation_ids": [observation_ids[1]],
                }
            ],
            "stats": {"person_count": 1, "assignment_count": 1},
        }

    monkeypatch.setattr("hikbox_pictures.product.scan.assignment_stage.run_frozen_v5_assignment", fake_full_run)
    stage = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    )

    stage.run_frozen_v5_assignment(
        scan_session_id=session_id,
        run_kind="scan_full",
        embedding_calculator=calculator,
    )

    assert captured_face_ids == [observation_ids[1]]


def test_scan_incremental_abort_during_assignment_interrupts_and_rolls_back(tmp_path: Path, monkeypatch) -> None:
    layout, session_id, runtime_root = create_task6_workspace(tmp_path)
    observation_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 0, "color": (220, 180, 160), "quality_score": 0.92},
            {"asset_index": 0, "color": (218, 178, 158), "quality_score": 0.90},
        ],
    )
    shared_main = embedding_from_seed(611)
    calculator = fake_embedding_calculator_from_map(
        {
            "f1.png": (shared_main, embedding_from_seed(711), 1.1),
            "f2.png": (shared_main, embedding_from_seed(712), 1.1),
        }
    )

    def fake_full_run(*, faces, params):
        return {
            "faces": [
                {
                    "face_observation_id": observation_ids[0],
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "assignment_source": "hdbscan",
                    "probability": 0.95,
                },
                {
                    "face_observation_id": observation_ids[1],
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "assignment_source": "hdbscan",
                    "probability": 0.94,
                },
            ],
            "persons": [{"person_temp_key": "p0", "face_observation_ids": observation_ids}],
            "clusters": [
                {
                    "cluster_label": 10,
                    "person_temp_key": "p0",
                    "member_face_observation_ids": observation_ids,
                    "representative_face_observation_ids": [observation_ids[0]],
                }
            ],
            "stats": {"person_count": 1, "assignment_count": 2},
        }

    monkeypatch.setattr("hikbox_pictures.product.scan.assignment_stage.run_frozen_v5_assignment", fake_full_run)
    stage = AssignmentStageService(
        library_db_path=layout.library_db,
        embedding_db_path=layout.embedding_db,
        output_root=runtime_root,
    )
    baseline = stage.run_frozen_v5_assignment(
        scan_session_id=session_id,
        run_kind="scan_full",
        embedding_calculator=calculator,
    )
    assert baseline.assignment_count == 2
    ScanSessionRepository(layout.library_db).update_status(session_id, status="completed")

    incremental_session = ScanSessionRepository(layout.library_db).create_session(
        run_kind="scan_incremental",
        status="running",
        triggered_by="manual_cli",
    )
    conn = sqlite3.connect(layout.library_db)
    try:
        source_id = int(conn.execute("SELECT id FROM library_source ORDER BY id ASC LIMIT 1").fetchone()[0])
        conn.execute(
            """
            INSERT INTO scan_session_source(
              scan_session_id, library_source_id, stage_status_json, processed_assets, failed_assets, updated_at
            ) VALUES (?, ?, '{"discover":"done","metadata":"done","detect":"done","embed":"pending","cluster":"pending","assignment":"pending"}', 4, 0, CURRENT_TIMESTAMP)
            """,
            (incremental_session.id, source_id),
        )
        conn.commit()
    finally:
        conn.close()

    new_face_ids = seed_face_observations(
        layout.library_db,
        runtime_root,
        [
            {"asset_index": 1, "color": (222, 182, 162), "quality_score": 0.91},
            {"asset_index": 1, "color": (221, 181, 161), "quality_score": 0.89},
        ],
    )
    for offset, face_id in enumerate(new_face_ids, start=1):
        upsert_face_embeddings(
            layout.embedding_db,
            face_observation_id=face_id,
            main=shared_main,
            flip=embedding_from_seed(713 + offset),
        )

    service = ScanExecutionService(db_path=layout.library_db, output_root=runtime_root)
    monkeypatch.setattr(service._discover_service, "run", lambda scan_session_id: None)
    monkeypatch.setattr(service._metadata_service, "run", lambda scan_session_id: None)
    monkeypatch.setattr(
        ScanExecutionService,
        "run_detect_stage",
        lambda self, scan_session_id, runtime_defaults=None, detector=None: DetectStageRunResult(
            claimed_batches=0,
            acked_batches=0,
            interrupted=False,
        ),
    )

    original_attach_face = IncrementalAssignmentService._attach_face
    aborted_once = {"done": False}

    def abort_after_first_attach(self, *, conn, face_observation_id, person_id, cluster_id, assignment_run_id, face_quality_by_id):
        original_attach_face(
            self,
            conn=conn,
            face_observation_id=face_observation_id,
            person_id=person_id,
            cluster_id=cluster_id,
            assignment_run_id=assignment_run_id,
            face_quality_by_id=face_quality_by_id,
        )
        if not aborted_once["done"]:
            ScanSessionRepository(layout.library_db).update_status(
                incremental_session.id,
                status="aborting",
                conn=conn,
            )
            aborted_once["done"] = True

    monkeypatch.setattr(IncrementalAssignmentService, "_attach_face", abort_after_first_attach)

    result = service.run_session(
        scan_session_id=incremental_session.id,
        embedding_calculator=calculator,
    )

    conn = sqlite3.connect(layout.library_db)
    try:
        session_status = str(conn.execute("SELECT status FROM scan_session WHERE id=?", (incremental_session.id,)).fetchone()[0])
        run_status = str(
            conn.execute(
                "SELECT status FROM assignment_run WHERE scan_session_id=? ORDER BY id DESC LIMIT 1",
                (incremental_session.id,),
            ).fetchone()[0]
        )
        assigned_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM person_face_assignment WHERE face_observation_id IN (?, ?) AND active=1",
                tuple(new_face_ids),
            ).fetchone()[0]
        )
    finally:
        conn.close()

    assert result.assignment_run_id == 0
    assert session_status == "interrupted"
    assert run_status == "failed"
    assert assigned_count == 0
