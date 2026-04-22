import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import hikbox_pictures.face_review_pipeline as pipeline_module
from hikbox_pictures.face_review_pipeline import (
    attach_micro_clusters_to_existing_persons,
    attach_noise_faces_to_person_consensus,
    exclude_low_quality_faces_from_assignment,
    group_faces_by_cluster,
    iter_embedded_faces,
    iter_faces_pending_embedding,
    iter_image_files,
    mark_face_embedded,
    merge_embedding_with_optional_flip,
    merge_clusters_to_persons,
    open_pipeline_db,
    render_review_html,
    run_cluster_stage,
    run_detection_stage,
    run_pipeline,
    upsert_detected_face,
    write_person_review_pages,
)


def _list_pending_source_rows(output_dir: Path, source_key: str, batch_size: int) -> list[dict[str, str]]:
    conn = open_pipeline_db(output_dir / "cache" / "pipeline.db")
    try:
        rows = conn.execute(
            """
            SELECT source_key, photo_relpath
            FROM source_images
            WHERE source_key=?
              AND detect_status='pending'
            ORDER BY photo_relpath
            LIMIT ?
            """,
            (source_key, batch_size),
        ).fetchall()
        return [{"source_key": str(row["source_key"]), "photo_relpath": str(row["photo_relpath"])} for row in rows]
    finally:
        conn.close()


def test_merge_embedding_with_optional_flip_blends_and_normalizes() -> None:
    merged = merge_embedding_with_optional_flip(
        main_embedding=[1.0, 0.0],
        flip_embedding=[0.0, 1.0],
        flip_weight=1.0,
    )

    assert len(merged) == 2
    assert abs(merged[0] - 0.70710678) < 1e-6
    assert abs(merged[1] - 0.70710678) < 1e-6


def test_iter_image_files_filters_hidden_and_non_images(tmp_path: Path) -> None:
    src = tmp_path / "album"
    src.mkdir()
    (src / "A.JPG").write_bytes(b"x")
    (src / "b.heic").write_bytes(b"x")
    (src / "c.png").write_bytes(b"x")
    (src / "d.mov").write_bytes(b"x")
    (src / ".hidden.jpg").write_bytes(b"x")
    (src / "sub").mkdir()
    (src / "sub" / "e.JPEG").write_bytes(b"x")

    results = [p.relative_to(src).as_posix() for p in iter_image_files(src)]

    assert results == ["A.JPG", "b.heic", "c.png", "sub/e.JPEG"]


def test_run_detection_stage_batch_mode_uses_subprocess_worker(tmp_path: Path, monkeypatch, capsys) -> None:
    source_dir = tmp_path / "album"
    source_dir.mkdir()
    for idx in range(3):
        Image.new("RGB", (16, 16), (idx, idx, idx)).save(source_dir / f"img_{idx}.jpg")

    output_dir = tmp_path / "out"
    worker_batches: list[list[str]] = []

    def fake_detection_subprocess_worker(
        *,
        output_dir: Path,
        source_key: str,
        batch_size: int,
        **kwargs,  # noqa: ANN003, ARG001
    ) -> int:
        pending_rows = _list_pending_source_rows(output_dir=output_dir, source_key=source_key, batch_size=batch_size)
        worker_batches.append([row["photo_relpath"] for row in pending_rows])
        conn = open_pipeline_db(output_dir / "cache" / "pipeline.db")
        try:
            for row in pending_rows:
                pipeline_module._mark_source_image_detect_result(
                    conn,
                    source_key=row["source_key"],
                    photo_relpath=row["photo_relpath"],
                    status="done",
                )
            pipeline_module._refresh_source_detect_stage(conn, source_key=source_key)
        finally:
            conn.close()
        return len(pending_rows)

    monkeypatch.setattr(
        pipeline_module,
        "_run_detection_subprocess_worker",
        fake_detection_subprocess_worker,
        raising=False,
    )
    monkeypatch.setattr(
        "hikbox_pictures.face_review_pipeline._init_detection_model",
        lambda **kwargs: pytest.fail("detect 不应在父进程里直接初始化 detector"),  # noqa: ARG005
    )

    summary = run_detection_stage(
        source_dir=source_dir,
        output_dir=output_dir,
        insightface_root=tmp_path / "insightface",
        detector_model_name="buffalo_l",
        det_size=640,
        preview_max_side=480,
        detect_restart_interval=2,
    )
    stdout = capsys.readouterr().out

    assert summary["processed_image_count"] == 3
    assert summary["remaining_image_count"] == 0
    assert "阶段 detect：启动子进程处理批次 source=__root__ batch=2 总体进度=0/3" in stdout
    assert "阶段 detect：启动子进程处理批次 source=__root__ batch=1 总体进度=2/3" in stdout
    assert worker_batches == [["img_0.jpg", "img_1.jpg"], ["img_2.jpg"]]


def test_run_detection_stage_only_refreshes_discovery_when_explicitly_requested(tmp_path: Path, monkeypatch) -> None:
    source_dir = tmp_path / "album"
    source_dir.mkdir()
    (source_dir / "family").mkdir()
    Image.new("RGB", (16, 16), (1, 1, 1)).save(source_dir / "root.jpg")
    Image.new("RGB", (16, 16), (2, 2, 2)).save(source_dir / "family" / "img_0.jpg")

    output_dir = tmp_path / "out"
    worker_calls: list[tuple[str, list[str]]] = []

    def fake_detection_subprocess_worker(
        *,
        output_dir: Path,
        source_key: str,
        batch_size: int,
        **kwargs,  # noqa: ANN003, ARG001
    ) -> int:
        pending_rows = _list_pending_source_rows(output_dir=output_dir, source_key=source_key, batch_size=batch_size)
        worker_calls.append((source_key, [row["photo_relpath"] for row in pending_rows]))
        conn = open_pipeline_db(output_dir / "cache" / "pipeline.db")
        try:
            for row in pending_rows:
                pipeline_module._mark_source_image_detect_result(
                    conn,
                    source_key=row["source_key"],
                    photo_relpath=row["photo_relpath"],
                    status="done",
                )
            pipeline_module._refresh_source_detect_stage(conn, source_key=source_key)
        finally:
            conn.close()
        return len(pending_rows)

    monkeypatch.setattr(
        pipeline_module,
        "_run_detection_subprocess_worker",
        fake_detection_subprocess_worker,
        raising=False,
    )
    monkeypatch.setattr(
        "hikbox_pictures.face_review_pipeline._init_detection_model",
        lambda **kwargs: pytest.fail("detect 不应在父进程里直接初始化 detector"),  # noqa: ARG005
    )

    first = run_detection_stage(
        source_dir=source_dir,
        output_dir=output_dir,
        insightface_root=tmp_path / "insightface",
        detector_model_name="buffalo_l",
        det_size=640,
        preview_max_side=480,
        detect_restart_interval=999,
    )
    Image.new("RGB", (16, 16), (3, 3, 3)).save(source_dir / "family" / "img_1.jpg")
    second = run_detection_stage(
        source_dir=source_dir,
        output_dir=output_dir,
        insightface_root=tmp_path / "insightface",
        detector_model_name="buffalo_l",
        det_size=640,
        preview_max_side=480,
        detect_restart_interval=999,
    )
    third = run_detection_stage(
        source_dir=source_dir,
        output_dir=output_dir,
        insightface_root=tmp_path / "insightface",
        detector_model_name="buffalo_l",
        det_size=640,
        preview_max_side=480,
        detect_restart_interval=999,
        refresh_discover=True,
    )

    conn = open_pipeline_db(output_dir / "cache" / "pipeline.db")
    sources = [
        (row["source_key"], row["source_relpath"], row["discover_status"], row["detect_status"])
        for row in conn.execute(
            """
            SELECT source_key, source_relpath, discover_status, detect_status
            FROM pipeline_sources
            ORDER BY source_key
            """
        ).fetchall()
    ]
    source_images = [
        (row["photo_relpath"], row["source_key"], row["detect_status"])
        for row in conn.execute(
            """
            SELECT photo_relpath, source_key, detect_status
            FROM source_images
            ORDER BY photo_relpath
            """
        ).fetchall()
    ]
    conn.close()

    assert first["discovered_image_count"] == 2
    assert first["processed_image_count"] == 2
    assert second["pending_image_count"] == 0
    assert second["processed_image_count"] == 0
    assert second["discovered_image_count"] == 2
    assert third["discovered_image_count"] == 3
    assert third["processed_image_count"] == 1
    assert worker_calls == [
        ("__root__", ["root.jpg"]),
        ("family", ["family/img_0.jpg"]),
        ("family", ["family/img_1.jpg"]),
    ]
    assert sources == [
        ("__root__", ".", "done", "done"),
        ("family", "family", "done", "done"),
    ]
    assert source_images == [
        ("family/img_0.jpg", "family", "done"),
        ("family/img_1.jpg", "family", "done"),
        ("root.jpg", "__root__", "done"),
    ]


def test_run_pipeline_second_start_skips_completed_detect_embed_cluster(tmp_path: Path, monkeypatch) -> None:
    source_dir = tmp_path / "album"
    source_dir.mkdir()
    Image.new("RGB", (32, 32), (128, 128, 128)).save(source_dir / "img_0.jpg")

    output_dir = tmp_path / "out"
    detect_worker_call_count = 0
    embedder_init_count = 0
    cluster_call_count = 0

    class _FakeEmbedder:
        def __init__(self, checkpoint_path: Path, device: str = "cpu") -> None:  # noqa: ARG002
            nonlocal embedder_init_count
            embedder_init_count += 1

        def embed(self, aligned_face_bgr_112):  # noqa: ANN001
            return [1.0, 0.0], 12.3

    def fake_detection_subprocess_worker(
        *,
        output_dir: Path,
        source_key: str,
        batch_size: int,
        **kwargs,  # noqa: ANN003, ARG001
    ) -> int:
        nonlocal detect_worker_call_count
        pending_rows = _list_pending_source_rows(output_dir=output_dir, source_key=source_key, batch_size=batch_size)
        if not pending_rows:
            return 0
        detect_worker_call_count += 1
        aligned_dir = output_dir / "assets" / "aligned"
        crop_dir = output_dir / "assets" / "crops"
        context_dir = output_dir / "assets" / "context"
        conn = open_pipeline_db(output_dir / "cache" / "pipeline.db")
        try:
            for row in pending_rows:
                relpath = row["photo_relpath"]
                face_id = f"{hashlib.sha1(relpath.encode('utf-8')).hexdigest()[:16]}_000"
                Image.new("RGB", (32, 32), (200, 200, 200)).save(crop_dir / f"{face_id}.jpg")
                Image.new("RGB", (32, 32), (180, 180, 180)).save(context_dir / f"{face_id}.jpg")
                Image.new("RGB", (112, 112), (128, 128, 128)).save(aligned_dir / f"{face_id}.png")
                upsert_detected_face(
                    conn,
                    {
                        "face_id": face_id,
                        "photo_relpath": relpath,
                        "crop_relpath": f"assets/crops/{face_id}.jpg",
                        "context_relpath": f"assets/context/{face_id}.jpg",
                        "preview_relpath": "",
                        "aligned_relpath": f"assets/aligned/{face_id}.png",
                        "bbox": [4, 4, 24, 24],
                        "detector_confidence": 0.99,
                        "face_area_ratio": 0.4,
                    },
                )
                pipeline_module._mark_source_image_detect_result(
                    conn,
                    source_key=row["source_key"],
                    photo_relpath=relpath,
                    status="done",
                )
            pipeline_module._refresh_source_detect_stage(conn, source_key=source_key)
        finally:
            conn.close()
        return len(pending_rows)

    def fake_cluster(embeddings, min_cluster_size, min_samples):  # noqa: ANN001
        nonlocal cluster_call_count
        cluster_call_count += 1
        assert len(embeddings) == 1
        return [0], [0.99]

    monkeypatch.setattr(
        pipeline_module,
        "_run_detection_subprocess_worker",
        fake_detection_subprocess_worker,
        raising=False,
    )
    monkeypatch.setattr(
        "hikbox_pictures.face_review_pipeline._init_detection_model",
        lambda **kwargs: pytest.fail("detect 不应在父进程里直接初始化 detector"),  # noqa: ARG005
    )
    monkeypatch.setattr("hikbox_pictures.face_review_pipeline.MagFaceEmbedder", _FakeEmbedder)
    monkeypatch.setattr("hikbox_pictures.face_review_pipeline._cluster_with_hdbscan", fake_cluster)

    first = run_pipeline(
        source_dir=source_dir,
        output_dir=output_dir,
        magface_checkpoint=tmp_path / "magface.pth",
        insightface_root=tmp_path / "insightface",
        detector_model_name="buffalo_l",
        det_size=640,
        embedding_enable_flip=False,
        embedding_flip_weight=1.0,
        detect_restart_interval=999,
        min_cluster_size=2,
        min_samples=1,
        person_merge_threshold=0.26,
        person_rep_top_k=3,
        person_knn_k=8,
        person_linkage="single",
        person_enable_same_photo_cannot_link=False,
        preview_max_side=480,
        person_consensus_distance_threshold=0.24,
        person_consensus_margin_threshold=0.04,
        person_consensus_rep_top_k=3,
        low_quality_micro_cluster_max_size=3,
        low_quality_micro_cluster_top2_weight=0.5,
        low_quality_micro_cluster_min_quality_evidence=0.72,
        face_min_quality_for_assignment=0.25,
        person_cluster_recall_distance_threshold=0.32,
        person_cluster_recall_margin_threshold=0.04,
        person_cluster_recall_top_n=5,
        person_cluster_recall_min_votes=3,
        person_cluster_recall_source_max_cluster_size=20,
        person_cluster_recall_source_max_person_faces=8,
        person_cluster_recall_target_min_person_faces=40,
        person_cluster_recall_max_rounds=2,
    )
    second = run_pipeline(
        source_dir=source_dir,
        output_dir=output_dir,
        magface_checkpoint=tmp_path / "magface.pth",
        insightface_root=tmp_path / "insightface",
        detector_model_name="buffalo_l",
        det_size=640,
        embedding_enable_flip=False,
        embedding_flip_weight=1.0,
        detect_restart_interval=999,
        min_cluster_size=2,
        min_samples=1,
        person_merge_threshold=0.26,
        person_rep_top_k=3,
        person_knn_k=8,
        person_linkage="single",
        person_enable_same_photo_cannot_link=False,
        preview_max_side=480,
        person_consensus_distance_threshold=0.24,
        person_consensus_margin_threshold=0.04,
        person_consensus_rep_top_k=3,
        low_quality_micro_cluster_max_size=3,
        low_quality_micro_cluster_top2_weight=0.5,
        low_quality_micro_cluster_min_quality_evidence=0.72,
        face_min_quality_for_assignment=0.25,
        person_cluster_recall_distance_threshold=0.32,
        person_cluster_recall_margin_threshold=0.04,
        person_cluster_recall_top_n=5,
        person_cluster_recall_min_votes=3,
        person_cluster_recall_source_max_cluster_size=20,
        person_cluster_recall_source_max_person_faces=8,
        person_cluster_recall_target_min_person_faces=40,
        person_cluster_recall_max_rounds=2,
    )

    assert first["meta"]["detect_summary"]["processed_image_count"] == 1
    assert second["meta"]["detect_summary"]["processed_image_count"] == 0
    assert second["meta"]["embed_summary"]["pending_face_count"] == 0
    assert detect_worker_call_count == 1
    assert embedder_init_count == 1
    assert cluster_call_count == 1
    assert second["meta"]["person_count"] == 1


def test_group_faces_by_cluster_keeps_noise_separate() -> None:
    faces = [
        {"face_id": "f1"},
        {"face_id": "f2"},
        {"face_id": "f3"},
        {"face_id": "f4"},
        {"face_id": "f5"},
    ]
    labels = [1, -1, 0, 1, -1]

    grouped = group_faces_by_cluster(faces=faces, labels=labels)

    assert [c["cluster_key"] for c in grouped] == ["cluster_1", "cluster_0", "noise"]
    assert [len(c["members"]) for c in grouped] == [2, 1, 2]


def test_render_review_html_contains_local_assets() -> None:
    payload = {
        "meta": {"model": "MagFace", "clusterer": "HDBSCAN"},
        "clusters": [
            {
                "cluster_key": "cluster_0",
                "members": [
                    {
                        "face_id": "x",
                        "crop_relpath": "assets/crops/x.jpg",
                        "context_relpath": "assets/context/x.jpg",
                    }
                ],
            }
        ],
    }

    html = render_review_html(payload)

    assert "MagFace" in html
    assert "HDBSCAN" in html
    assert "assets/crops/x.jpg" in html
    assert "assets/context/x.jpg" in html
    assert "assets/preview/" not in html


def test_merge_clusters_to_persons_merges_close_clusters() -> None:
    faces = [
        {
            "face_id": "a0",
            "embedding": [1.0, 0.0],
            "quality_score": 0.95,
        },
        {
            "face_id": "a1",
            "embedding": [0.997, 0.077],
            "quality_score": 0.92,
        },
        {
            "face_id": "b0",
            "embedding": [0.996, 0.089],
            "quality_score": 0.90,
        },
        {
            "face_id": "b1",
            "embedding": [0.992, 0.123],
            "quality_score": 0.88,
        },
        {
            "face_id": "c0",
            "embedding": [-1.0, 0.0],
            "quality_score": 0.93,
        },
        {
            "face_id": "c1",
            "embedding": [-0.995, -0.100],
            "quality_score": 0.89,
        },
    ]
    labels = [0, 0, 1, 1, 2, 2]
    clusters = group_faces_by_cluster(faces=faces, labels=labels)

    persons = merge_clusters_to_persons(
        clusters=clusters,
        distance_threshold=0.03,
        rep_top_k=2,
        knn_k=2,
        linkage="average",
    )

    assert len(persons) == 2
    assert persons[0]["person_face_count"] == 4
    assert persons[0]["person_cluster_count"] == 2
    assert {item["cluster_label"] for item in persons[0]["clusters"]} == {0, 1}
    assert persons[1]["person_cluster_count"] == 1
    assert persons[1]["clusters"][0]["cluster_label"] == 2


def test_merge_clusters_to_persons_supports_single_linkage_chain_merge() -> None:
    faces = [
        {"face_id": "a0", "embedding": [1.0, 0.0], "quality_score": 0.95},
        {"face_id": "a1", "embedding": [0.999, 0.03], "quality_score": 0.91},
        {"face_id": "b0", "embedding": [0.819, 0.574], "quality_score": 0.94},
        {"face_id": "b1", "embedding": [0.800, 0.600], "quality_score": 0.90},
        {"face_id": "c0", "embedding": [0.342, 0.940], "quality_score": 0.93},
        {"face_id": "c1", "embedding": [0.360, 0.933], "quality_score": 0.89},
    ]
    labels = [0, 0, 1, 1, 2, 2]
    clusters = group_faces_by_cluster(faces=faces, labels=labels)

    persons_average = merge_clusters_to_persons(
        clusters=clusters,
        distance_threshold=0.2,
        rep_top_k=2,
        knn_k=2,
        linkage="average",
    )
    persons_single = merge_clusters_to_persons(
        clusters=clusters,
        distance_threshold=0.2,
        rep_top_k=2,
        knn_k=2,
        linkage="single",
    )

    assert len(persons_average) == 2
    assert len(persons_single) == 1
    assert persons_single[0]["person_cluster_count"] == 3


def test_merge_clusters_to_persons_same_photo_cannot_link_is_optional() -> None:
    faces = [
        {
            "face_id": "a0",
            "embedding": [1.0, 0.0],
            "quality_score": 0.95,
            "photo_relpath": "album/p1.jpg",
        },
        {
            "face_id": "a1",
            "embedding": [0.999, 0.02],
            "quality_score": 0.92,
            "photo_relpath": "album/p2.jpg",
        },
        {
            "face_id": "b0",
            "embedding": [1.0, 0.0],
            "quality_score": 0.94,
            "photo_relpath": "album/p1.jpg",
        },
        {
            "face_id": "b1",
            "embedding": [0.998, 0.03],
            "quality_score": 0.90,
            "photo_relpath": "album/p3.jpg",
        },
    ]
    labels = [0, 0, 1, 1]
    clusters = group_faces_by_cluster(faces=faces, labels=labels)

    persons_disabled = merge_clusters_to_persons(
        clusters=clusters,
        distance_threshold=0.2,
        rep_top_k=2,
        knn_k=2,
        linkage="single",
    )
    persons_enabled = merge_clusters_to_persons(
        clusters=clusters,
        distance_threshold=0.2,
        rep_top_k=2,
        knn_k=2,
        linkage="single",
        enable_same_photo_cannot_link=True,
    )

    assert len(persons_disabled) == 1
    assert persons_disabled[0]["person_cluster_count"] == 2
    assert len(persons_enabled) == 2
    assert all(item["person_cluster_count"] == 1 for item in persons_enabled)


def test_attach_noise_faces_to_person_consensus_attaches_when_sibling_micro_clusters_share_person() -> None:
    faces = [
        {"face_id": "a0", "embedding": [1.0, 0.0], "quality_score": 0.95},
        {"face_id": "a1", "embedding": [0.998, 0.06], "quality_score": 0.90},
        {"face_id": "b0", "embedding": [0.978, 0.208], "quality_score": 0.94},
        {"face_id": "b1", "embedding": [0.965, 0.262], "quality_score": 0.91},
        {"face_id": "c0", "embedding": [0.0, 1.0], "quality_score": 0.93},
        {"face_id": "c1", "embedding": [0.06, 0.998], "quality_score": 0.89},
        {"face_id": "n0", "embedding": [0.992, 0.122], "quality_score": 0.88},
    ]
    labels = [0, 0, 1, 1, 2, 2, -1]
    probabilities = [0.99, 0.98, 0.97, 0.96, 0.95, 0.94, 0.0]
    clusters = group_faces_by_cluster(faces=faces, labels=labels)
    persons = merge_clusters_to_persons(
        clusters=clusters,
        distance_threshold=0.03,
        rep_top_k=2,
        knn_k=2,
        linkage="average",
    )

    attached_labels, attached_probabilities, attached_count = attach_noise_faces_to_person_consensus(
        faces=faces,
        labels=labels,
        probabilities=probabilities,
        persons=persons,
        rep_top_k=2,
        distance_threshold=0.20,
        margin_threshold=0.04,
    )

    assert attached_count == 1
    assert attached_labels == [0, 0, 1, 1, 2, 2, 0]
    assert attached_probabilities[-1] is None


def test_attach_noise_faces_to_person_consensus_keeps_cross_person_ambiguous_noise_unassigned() -> None:
    faces = [
        {"face_id": "a0", "embedding": [1.0, 0.0], "quality_score": 0.95},
        {"face_id": "a1", "embedding": [0.998, 0.06], "quality_score": 0.90},
        {"face_id": "b0", "embedding": [0.978, 0.208], "quality_score": 0.94},
        {"face_id": "b1", "embedding": [0.965, 0.262], "quality_score": 0.91},
        {"face_id": "c0", "embedding": [0.906, 0.423], "quality_score": 0.93},
        {"face_id": "c1", "embedding": [0.879, 0.476], "quality_score": 0.89},
        {"face_id": "d0", "embedding": [0.0, 1.0], "quality_score": 0.92},
        {"face_id": "d1", "embedding": [0.06, 0.998], "quality_score": 0.88},
        {"face_id": "n0", "embedding": [0.944, 0.331], "quality_score": 0.88},
    ]
    labels = [0, 0, 1, 1, 2, 2, 3, 3, -1]
    probabilities = [0.99, 0.98, 0.97, 0.96, 0.95, 0.94, 0.93, 0.92, 0.0]
    clusters = group_faces_by_cluster(faces=faces, labels=labels)
    persons = merge_clusters_to_persons(
        clusters=clusters,
        distance_threshold=0.03,
        rep_top_k=2,
        knn_k=3,
        linkage="average",
    )

    attached_labels, attached_probabilities, attached_count = attach_noise_faces_to_person_consensus(
        faces=faces,
        labels=labels,
        probabilities=probabilities,
        persons=persons,
        rep_top_k=2,
        distance_threshold=0.20,
        margin_threshold=0.04,
    )

    assert attached_count == 0
    assert attached_labels == labels
    assert attached_probabilities == probabilities


def test_attach_noise_faces_to_person_consensus_can_use_flip_embedding_as_supplement() -> None:
    faces = [
        {"face_id": "a0", "embedding": [1.0, 0.0], "embedding_flip": [0.0, 1.0], "quality_score": 0.95},
        {"face_id": "a1", "embedding": [0.998, 0.06], "embedding_flip": [0.0, 1.0], "quality_score": 0.90},
        {"face_id": "b0", "embedding": [-1.0, 0.0], "embedding_flip": [0.0, -1.0], "quality_score": 0.94},
        {"face_id": "b1", "embedding": [-0.998, -0.06], "embedding_flip": [0.0, -1.0], "quality_score": 0.91},
        {"face_id": "n0", "embedding": [0.0, 1.0], "embedding_flip": [0.0, 1.0], "quality_score": 0.88},
    ]
    labels = [0, 0, 1, 1, -1]
    probabilities: list[float | None] = [0.99, 0.98, 0.97, 0.96, 0.0]
    persons = [
        {"person_label": 0, "clusters": [{"cluster_label": 0}]},
        {"person_label": 1, "clusters": [{"cluster_label": 1}]},
    ]

    attached_labels, attached_probabilities, attached_count = attach_noise_faces_to_person_consensus(
        faces=faces,
        labels=labels,
        probabilities=probabilities,
        persons=persons,
        rep_top_k=2,
        distance_threshold=0.20,
        margin_threshold=0.04,
    )

    assert attached_count == 1
    assert attached_labels[-1] == 0
    assert attached_probabilities[-1] is None


def test_exclude_low_quality_faces_from_assignment_marks_noise_with_counts() -> None:
    faces = [
        {"face_id": "h0", "quality_score": 0.95},
        {"face_id": "l0", "quality_score": 0.22},
        {"face_id": "l1", "quality_score": 0.19},
        {"face_id": "n0", "quality_score": 0.30},
    ]
    labels = [0, 1, -1, -1]
    probabilities = [0.99, 0.98, 0.0, 0.0]

    updated_labels, updated_probabilities, excluded_flags, excluded_count = exclude_low_quality_faces_from_assignment(
        faces=faces,
        labels=labels,
        probabilities=probabilities,
        min_quality_score=0.25,
    )

    assert excluded_count == 2
    assert updated_labels == [0, -1, -1, -1]
    assert updated_probabilities == [0.99, 0.0, 0.0, 0.0]
    assert excluded_flags == [False, True, True, False]


def test_attach_micro_clusters_to_existing_persons_moves_small_cluster_to_anchor_person() -> None:
    def _member(face_id: str, emb: list[float], quality: float = 1.0) -> dict:
        return {
            "face_id": face_id,
            "embedding": emb,
            "quality_score": quality,
            "photo_relpath": f"album/{face_id}.jpg",
            "crop_relpath": f"assets/crops/{face_id}.jpg",
            "context_relpath": f"assets/context/{face_id}.jpg",
            "cluster_probability": 0.99,
            "cluster_assignment_source": "hdbscan",
            "bbox": [1, 2, 3, 4],
        }

    persons = [
        {
            "person_label": 0,
            "person_key": "person_0",
            "person_face_count": 10,
            "person_cluster_count": 2,
            "clusters": [
                {
                    "cluster_key": "cluster_0",
                    "cluster_label": 0,
                    "member_count": 5,
                    "members": [
                        _member("p0_a0", [1.0, 0.0]),
                        _member("p0_a1", [0.998, 0.060]),
                        _member("p0_a2", [0.996, 0.089]),
                        _member("p0_a3", [0.992, 0.123]),
                        _member("p0_a4", [0.989, 0.145]),
                    ],
                },
                {
                    "cluster_key": "cluster_1",
                    "cluster_label": 1,
                    "member_count": 5,
                    "members": [
                        _member("p0_b0", [0.984, 0.177]),
                        _member("p0_b1", [0.978, 0.208]),
                        _member("p0_b2", [0.971, 0.239]),
                        _member("p0_b3", [0.965, 0.262]),
                        _member("p0_b4", [0.957, 0.289]),
                    ],
                },
            ],
        },
        {
            "person_label": 1,
            "person_key": "person_1",
            "person_face_count": 2,
            "person_cluster_count": 1,
            "clusters": [
                {
                    "cluster_key": "cluster_2",
                    "cluster_label": 2,
                    "member_count": 2,
                    "members": [
                        _member("cand_0", [0.993, 0.118]),
                        _member("cand_1", [0.988, 0.154]),
                    ],
                }
            ],
        },
    ]

    updated_persons, events, moved_count = attach_micro_clusters_to_existing_persons(
        persons=persons,
        source_max_cluster_size=3,
        source_max_person_face_count=8,
        target_min_person_face_count=8,
        knn_top_n=5,
        min_votes=3,
        distance_threshold=0.30,
        margin_threshold=0.04,
        max_rounds=2,
    )

    assert moved_count == 1
    assert len(events) == 1
    assert events[0]["cluster_label"] == 2
    assert events[0]["to_person_label_before_reindex"] == 0
    merged_person = updated_persons[0]
    assert merged_person["person_face_count"] == 12
    assert {cluster["cluster_label"] for cluster in merged_person["clusters"]} == {0, 1, 2}


def test_attach_micro_clusters_to_existing_persons_keeps_ambiguous_cluster_unmoved() -> None:
    def _member(face_id: str, emb: list[float], quality: float = 1.0) -> dict:
        return {
            "face_id": face_id,
            "embedding": emb,
            "quality_score": quality,
            "photo_relpath": f"album/{face_id}.jpg",
            "crop_relpath": f"assets/crops/{face_id}.jpg",
            "context_relpath": f"assets/context/{face_id}.jpg",
            "cluster_probability": 0.99,
            "cluster_assignment_source": "hdbscan",
            "bbox": [1, 2, 3, 4],
        }

    persons = [
        {
            "person_label": 0,
            "person_key": "person_0",
            "person_face_count": 8,
            "person_cluster_count": 1,
            "clusters": [
                {
                    "cluster_key": "cluster_0",
                    "cluster_label": 0,
                    "member_count": 8,
                    "members": [
                        _member("p0_0", [1.0, 0.0]),
                        _member("p0_1", [0.998, 0.055]),
                        _member("p0_2", [0.995, 0.098]),
                        _member("p0_3", [0.989, 0.145]),
                        _member("p0_4", [0.978, 0.208]),
                        _member("p0_5", [0.971, 0.239]),
                        _member("p0_6", [0.965, 0.262]),
                        _member("p0_7", [0.957, 0.289]),
                    ],
                }
            ],
        },
        {
            "person_label": 1,
            "person_key": "person_1",
            "person_face_count": 8,
            "person_cluster_count": 1,
            "clusters": [
                {
                    "cluster_key": "cluster_1",
                    "cluster_label": 1,
                    "member_count": 8,
                    "members": [
                        _member("p1_0", [0.0, 1.0]),
                        _member("p1_1", [0.055, 0.998]),
                        _member("p1_2", [0.098, 0.995]),
                        _member("p1_3", [0.145, 0.989]),
                        _member("p1_4", [0.208, 0.978]),
                        _member("p1_5", [0.239, 0.971]),
                        _member("p1_6", [0.262, 0.965]),
                        _member("p1_7", [0.289, 0.957]),
                    ],
                }
            ],
        },
        {
            "person_label": 2,
            "person_key": "person_2",
            "person_face_count": 2,
            "person_cluster_count": 1,
            "clusters": [
                {
                    "cluster_key": "cluster_2",
                    "cluster_label": 2,
                    "member_count": 2,
                    "members": [
                        _member("cand_0", [0.706, 0.708]),
                        _member("cand_1", [0.707, 0.707]),
                    ],
                }
            ],
        },
    ]

    updated_persons, events, moved_count = attach_micro_clusters_to_existing_persons(
        persons=persons,
        source_max_cluster_size=3,
        source_max_person_face_count=8,
        target_min_person_face_count=8,
        knn_top_n=5,
        min_votes=3,
        distance_threshold=0.35,
        margin_threshold=0.04,
        max_rounds=2,
    )

    assert moved_count == 0
    assert events == []
    assert len(updated_persons) == 3
    trailing_person = next(person for person in updated_persons if any(c["cluster_label"] == 2 for c in person["clusters"]))
    assert trailing_person["person_face_count"] == 2


def test_render_review_html_has_two_stage_collapsible_sections() -> None:
    payload = {
        "meta": {"model": "MagFace", "clusterer": "HDBSCAN", "person_clusterer": "AHC", "person_count": 1},
        "persons": [
            {
                "person_key": "person_0",
                "person_face_count": 2,
                "person_cluster_count": 2,
                "clusters": [
                    {
                        "cluster_key": "cluster_0",
                        "cluster_label": 0,
                        "member_count": 1,
                        "members": [
                            {
                                "face_id": "x",
                                "crop_relpath": "assets/crops/x.jpg",
                                "context_relpath": "assets/context/x.jpg",
                            }
                        ],
                    },
                    {
                        "cluster_key": "cluster_3",
                        "cluster_label": 3,
                        "member_count": 1,
                        "members": [
                            {
                                "face_id": "y",
                                "crop_relpath": "assets/crops/y.jpg",
                                "context_relpath": "assets/context/y.jpg",
                            }
                        ],
                    },
                ],
            }
        ],
        "clusters": [
            {
                "cluster_key": "cluster_0",
                "cluster_label": 0,
                "members": [
                    {
                        "face_id": "x",
                        "crop_relpath": "assets/crops/x.jpg",
                        "context_relpath": "assets/context/x.jpg",
                    }
                ],
            }
        ],
    }

    html = render_review_html(payload)

    assert "第二阶段 人物聚合（AHC）" in html
    assert "第一阶段 微簇（HDBSCAN）" in html
    assert html.index("第二阶段 人物聚合（AHC）") < html.index("第一阶段 微簇（HDBSCAN）")
    assert "person_0" in html
    assert "cluster_0" in html
    assert 'class="person-cluster panel-subitem" open' in html
    assert 'class="person-toggle-all"' in html
    assert "data-person-toggle-all" in html
    assert 'class="person-cluster-toggle"' in html
    assert 'data-person-cluster-toggle' in html
    assert "展开全部 person" in html
    assert "折叠全部 person" in html


def test_write_person_review_pages_only_outputs_large_person_split_html_files(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "review_person_9999.html").write_text("stale", encoding="utf-8")

    payload = {
        "meta": {"source": ".hikbox", "model": "MagFace", "clusterer": "HDBSCAN", "person_clusterer": "AHC"},
        "persons": [
            {
                "person_label": 0,
                "person_key": "person_0",
                "person_face_count": 120,
                "person_cluster_count": 1,
                "clusters": [
                    {
                        "cluster_key": "cluster_0",
                        "cluster_label": 0,
                        "member_count": 1,
                        "members": [
                            {
                                "face_id": "x",
                                "crop_relpath": "assets/crops/x.jpg",
                                "context_relpath": "assets/context/x.jpg",
                            }
                        ],
                    }
                ],
            },
            {
                "person_label": 1,
                "person_key": "person_1",
                "person_face_count": 130,
                "person_cluster_count": 1,
                "clusters": [
                    {
                        "cluster_key": "cluster_9",
                        "cluster_label": 9,
                        "member_count": 1,
                        "members": [
                            {
                                "face_id": "y",
                                "crop_relpath": "assets/crops/y.jpg",
                                "context_relpath": "assets/context/y.jpg",
                            }
                        ],
                    }
                ],
            },
        ],
        "clusters": [],
    }

    pages = write_person_review_pages(output_dir=output_dir, payload=payload)

    assert len(pages) == 2
    assert pages[0]["html"] == "review_person_0000.html"
    assert pages[1]["html"] == "review_person_0001.html"

    html_first = (output_dir / "review_person_0000.html").read_text(encoding="utf-8")
    assert "person_0" in html_first
    assert "person_1" not in html_first
    assert "第二阶段 人物聚合（AHC）" in html_first
    assert "第一阶段 微簇（HDBSCAN）" in html_first
    assert 'class="person-toggle-all"' in html_first
    assert "assets/crops/x.jpg" in html_first
    assert "assets/crops/y.jpg" not in html_first

    pages_manifest = json.loads((output_dir / "review_person_pages.json").read_text(encoding="utf-8"))
    assert pages_manifest["count"] == 2
    assert pages_manifest["pages"][0]["html"] == "review_person_0000.html"
    assert not (output_dir / "review_person_9999.html").exists()


def test_write_person_review_pages_outputs_large_person_index_page(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "meta": {"source": ".hikbox", "model": "MagFace", "clusterer": "HDBSCAN", "person_clusterer": "AHC"},
        "persons": [
            {
                "person_label": 0,
                "person_key": "person_0",
                "person_face_count": 120,
                "person_cluster_count": 3,
                "clusters": [
                    {
                        "cluster_key": "cluster_0",
                        "cluster_label": 0,
                        "member_count": 120,
                        "members": [
                            {
                                "face_id": "x",
                                "crop_relpath": "assets/crops/x.jpg",
                                "context_relpath": "assets/context/x.jpg",
                            }
                        ],
                    }
                ],
            },
            {
                "person_label": 1,
                "person_key": "person_1",
                "person_face_count": 80,
                "person_cluster_count": 2,
                "clusters": [
                    {
                        "cluster_key": "cluster_9",
                        "cluster_label": 9,
                        "member_count": 80,
                        "members": [
                            {
                                "face_id": "y",
                                "crop_relpath": "assets/crops/y.jpg",
                                "context_relpath": "assets/context/y.jpg",
                            }
                        ],
                    }
                ],
            },
        ],
        "clusters": [],
    }

    write_person_review_pages(output_dir=output_dir, payload=payload)

    large_index_html = (output_dir / "review_person_pages_over_100.html").read_text(encoding="utf-8")
    assert "person_0" in large_index_html
    assert "120" in large_index_html
    assert "review_person_0000.html" in large_index_html
    assert "person_1" not in large_index_html
    assert not (output_dir / "review_person_0001.html").exists()

    large_pages_manifest = json.loads(
        (output_dir / "review_person_pages_over_100.json").read_text(encoding="utf-8")
    )
    assert large_pages_manifest["min_face_count"] == 100
    assert large_pages_manifest["count"] == 1
    assert large_pages_manifest["pages"][0]["person_key"] == "person_0"


def test_run_cluster_stage_can_merge_split_micro_clusters(tmp_path: Path, monkeypatch) -> None:
    output_dir = tmp_path / "out"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "dummy.jpg").write_bytes(b"x")

    db_path = output_dir / "cache" / "pipeline.db"
    conn = open_pipeline_db(db_path)
    rows = [
        {
            "face_id": "a_000",
            "photo_relpath": "album/a.jpg",
            "crop_relpath": "assets/crops/a_000.jpg",
            "context_relpath": "assets/context/a_000.jpg",
            "preview_relpath": "assets/preview/a.jpg",
            "aligned_relpath": "assets/aligned/a_000.png",
            "bbox": [1, 2, 3, 4],
            "detector_confidence": 0.95,
            "face_area_ratio": 0.032,
        },
        {
            "face_id": "a_001",
            "photo_relpath": "album/a.jpg",
            "crop_relpath": "assets/crops/a_001.jpg",
            "context_relpath": "assets/context/a_001.jpg",
            "preview_relpath": "assets/preview/a.jpg",
            "aligned_relpath": "assets/aligned/a_001.png",
            "bbox": [5, 6, 7, 8],
            "detector_confidence": 0.94,
            "face_area_ratio": 0.030,
        },
        {
            "face_id": "b_000",
            "photo_relpath": "album/b.jpg",
            "crop_relpath": "assets/crops/b_000.jpg",
            "context_relpath": "assets/context/b_000.jpg",
            "preview_relpath": "assets/preview/b.jpg",
            "aligned_relpath": "assets/aligned/b_000.png",
            "bbox": [11, 12, 13, 14],
            "detector_confidence": 0.91,
            "face_area_ratio": 0.028,
        },
        {
            "face_id": "b_001",
            "photo_relpath": "album/b.jpg",
            "crop_relpath": "assets/crops/b_001.jpg",
            "context_relpath": "assets/context/b_001.jpg",
            "preview_relpath": "assets/preview/b.jpg",
            "aligned_relpath": "assets/aligned/b_001.png",
            "bbox": [15, 16, 17, 18],
            "detector_confidence": 0.90,
            "face_area_ratio": 0.027,
        },
    ]
    for row in rows:
        upsert_detected_face(conn, row)

    mark_face_embedded(conn, "a_000", embedding=[1.0, 0.0], magface_quality=12.3, quality_score=0.95)
    mark_face_embedded(conn, "a_001", embedding=[0.997, 0.077], magface_quality=12.0, quality_score=0.92)
    mark_face_embedded(conn, "b_000", embedding=[0.996, 0.089], magface_quality=11.8, quality_score=0.90)
    mark_face_embedded(conn, "b_001", embedding=[0.992, 0.123], magface_quality=11.5, quality_score=0.88)
    conn.close()

    def fake_cluster(embeddings, min_cluster_size, min_samples):
        assert len(embeddings) == 4
        return [0, 0, 1, 1], [0.99, 0.98, 0.97, 0.96]

    monkeypatch.setattr("hikbox_pictures.face_review_pipeline._cluster_with_hdbscan", fake_cluster)

    payload = run_cluster_stage(
        source_dir=source_dir,
        output_dir=output_dir,
        detector_model_name="buffalo_l",
        det_size=640,
        min_cluster_size=3,
        min_samples=2,
        person_merge_threshold=0.03,
        person_rep_top_k=2,
        person_knn_k=2,
        person_linkage="average",
        person_enable_same_photo_cannot_link=False,
        preview_max_side=480,
        magface_checkpoint=Path(".cache/magface/magface_iresnet100_ms1mv2.pth"),
    )

    assert payload["meta"]["cluster_count"] == 2
    assert payload["meta"]["person_count"] == 1
    assert payload["persons"][0]["person_cluster_count"] == 2
    assert "members" not in payload["persons"][0]
    assert payload["meta"]["person_review_page_count"] == 0
    assert not (output_dir / "review_person_0000.html").exists()
    pages_manifest = json.loads((output_dir / "review_person_pages.json").read_text(encoding="utf-8"))
    assert pages_manifest["count"] == 0
    for cluster in payload["persons"][0]["clusters"]:
        for member in cluster["members"]:
            assert "embedding" not in member
            assert member["cluster_assignment_source"] == "hdbscan"


def test_run_cluster_stage_can_attach_noise_face_by_person_consensus(tmp_path: Path, monkeypatch) -> None:
    output_dir = tmp_path / "out"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "dummy.jpg").write_bytes(b"x")

    db_path = output_dir / "cache" / "pipeline.db"
    conn = open_pipeline_db(db_path)
    rows = [
        {
            "face_id": "a_000",
            "photo_relpath": "album/a.jpg",
            "crop_relpath": "assets/crops/a_000.jpg",
            "context_relpath": "assets/context/a_000.jpg",
            "preview_relpath": "assets/preview/a.jpg",
            "aligned_relpath": "assets/aligned/a_000.png",
            "bbox": [1, 2, 3, 4],
            "detector_confidence": 0.95,
            "face_area_ratio": 0.032,
        },
        {
            "face_id": "a_001",
            "photo_relpath": "album/a.jpg",
            "crop_relpath": "assets/crops/a_001.jpg",
            "context_relpath": "assets/context/a_001.jpg",
            "preview_relpath": "assets/preview/a.jpg",
            "aligned_relpath": "assets/aligned/a_001.png",
            "bbox": [5, 6, 7, 8],
            "detector_confidence": 0.94,
            "face_area_ratio": 0.030,
        },
        {
            "face_id": "b_000",
            "photo_relpath": "album/b.jpg",
            "crop_relpath": "assets/crops/b_000.jpg",
            "context_relpath": "assets/context/b_000.jpg",
            "preview_relpath": "assets/preview/b.jpg",
            "aligned_relpath": "assets/aligned/b_000.png",
            "bbox": [9, 10, 11, 12],
            "detector_confidence": 0.93,
            "face_area_ratio": 0.029,
        },
        {
            "face_id": "b_001",
            "photo_relpath": "album/b.jpg",
            "crop_relpath": "assets/crops/b_001.jpg",
            "context_relpath": "assets/context/b_001.jpg",
            "preview_relpath": "assets/preview/b.jpg",
            "aligned_relpath": "assets/aligned/b_001.png",
            "bbox": [13, 14, 15, 16],
            "detector_confidence": 0.92,
            "face_area_ratio": 0.028,
        },
        {
            "face_id": "n_000",
            "photo_relpath": "album/n.jpg",
            "crop_relpath": "assets/crops/n_000.jpg",
            "context_relpath": "assets/context/n_000.jpg",
            "preview_relpath": "assets/preview/n.jpg",
            "aligned_relpath": "assets/aligned/n_000.png",
            "bbox": [11, 12, 13, 14],
            "detector_confidence": 0.91,
            "face_area_ratio": 0.028,
        },
    ]
    for row in rows:
        upsert_detected_face(conn, row)

    mark_face_embedded(conn, "a_000", embedding=[1.0, 0.0], magface_quality=12.3, quality_score=0.95)
    mark_face_embedded(conn, "a_001", embedding=[0.997, 0.077], magface_quality=12.0, quality_score=0.92)
    mark_face_embedded(conn, "b_000", embedding=[0.978, 0.208], magface_quality=11.9, quality_score=0.91)
    mark_face_embedded(conn, "b_001", embedding=[0.965, 0.262], magface_quality=11.7, quality_score=0.89)
    mark_face_embedded(conn, "n_000", embedding=[0.992, 0.122], magface_quality=11.8, quality_score=0.90)
    conn.close()

    def fake_cluster(embeddings, min_cluster_size, min_samples):
        assert len(embeddings) == 5
        return [0, 0, 1, 1, -1], [0.99, 0.98, 0.97, 0.96, 0.0]

    monkeypatch.setattr("hikbox_pictures.face_review_pipeline._cluster_with_hdbscan", fake_cluster)

    payload = run_cluster_stage(
        source_dir=source_dir,
        output_dir=output_dir,
        detector_model_name="buffalo_l",
        det_size=640,
        min_cluster_size=3,
        min_samples=1,
        person_merge_threshold=0.03,
        person_rep_top_k=2,
        person_knn_k=2,
        person_linkage="average",
        person_enable_same_photo_cannot_link=False,
        preview_max_side=480,
        magface_checkpoint=Path(".cache/magface/magface_iresnet100_ms1mv2.pth"),
        person_consensus_distance_threshold=0.20,
        person_consensus_margin_threshold=0.04,
        person_consensus_rep_top_k=2,
    )

    assert payload["meta"]["noise_count"] == 0
    assert payload["meta"]["person_consensus_attach_count"] == 1
    assert payload["meta"]["person_consensus_distance_threshold"] == 0.20
    assert payload["clusters"][0]["cluster_label"] == 0
    assert len(payload["clusters"][0]["members"]) == 3
    assert payload["clusters"][0]["members"][-1]["face_id"] == "n_000"
    assert payload["clusters"][0]["members"][-1]["cluster_probability"] is None
    assert payload["clusters"][0]["members"][-1]["cluster_assignment_source"] == "person_consensus"
    assert payload["clusters"][0]["members"][0]["cluster_assignment_source"] == "hdbscan"
    assert all(member["cluster_assignment_source"] == "hdbscan" for member in payload["clusters"][1]["members"])


def test_run_cluster_stage_can_demote_low_quality_micro_clusters_to_noise(tmp_path: Path, monkeypatch) -> None:
    output_dir = tmp_path / "out"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "dummy.jpg").write_bytes(b"x")

    db_path = output_dir / "cache" / "pipeline.db"
    conn = open_pipeline_db(db_path)
    rows = [
        {
            "face_id": "a_000",
            "photo_relpath": "album/a.jpg",
            "crop_relpath": "assets/crops/a_000.jpg",
            "context_relpath": "assets/context/a_000.jpg",
            "preview_relpath": "assets/preview/a.jpg",
            "aligned_relpath": "assets/aligned/a_000.png",
            "bbox": [1, 2, 3, 4],
            "detector_confidence": 0.95,
            "face_area_ratio": 0.032,
        },
        {
            "face_id": "a_001",
            "photo_relpath": "album/a.jpg",
            "crop_relpath": "assets/crops/a_001.jpg",
            "context_relpath": "assets/context/a_001.jpg",
            "preview_relpath": "assets/preview/a.jpg",
            "aligned_relpath": "assets/aligned/a_001.png",
            "bbox": [5, 6, 7, 8],
            "detector_confidence": 0.94,
            "face_area_ratio": 0.030,
        },
        {
            "face_id": "l_000",
            "photo_relpath": "album/l.jpg",
            "crop_relpath": "assets/crops/l_000.jpg",
            "context_relpath": "assets/context/l_000.jpg",
            "preview_relpath": "assets/preview/l.jpg",
            "aligned_relpath": "assets/aligned/l_000.png",
            "bbox": [9, 10, 11, 12],
            "detector_confidence": 0.62,
            "face_area_ratio": 0.001,
        },
        {
            "face_id": "l_001",
            "photo_relpath": "album/l.jpg",
            "crop_relpath": "assets/crops/l_001.jpg",
            "context_relpath": "assets/context/l_001.jpg",
            "preview_relpath": "assets/preview/l.jpg",
            "aligned_relpath": "assets/aligned/l_001.png",
            "bbox": [13, 14, 15, 16],
            "detector_confidence": 0.58,
            "face_area_ratio": 0.001,
        },
        {
            "face_id": "n_000",
            "photo_relpath": "album/n.jpg",
            "crop_relpath": "assets/crops/n_000.jpg",
            "context_relpath": "assets/context/n_000.jpg",
            "preview_relpath": "assets/preview/n.jpg",
            "aligned_relpath": "assets/aligned/n_000.png",
            "bbox": [17, 18, 19, 20],
            "detector_confidence": 0.90,
            "face_area_ratio": 0.020,
        },
    ]
    for row in rows:
        upsert_detected_face(conn, row)

    mark_face_embedded(conn, "a_000", embedding=[1.0, 0.0], magface_quality=12.3, quality_score=0.95)
    mark_face_embedded(conn, "a_001", embedding=[0.997, 0.077], magface_quality=12.0, quality_score=0.88)
    mark_face_embedded(conn, "l_000", embedding=[-1.0, 0.0], magface_quality=11.8, quality_score=0.26)
    mark_face_embedded(conn, "l_001", embedding=[-0.981, -0.194], magface_quality=11.5, quality_score=0.18)
    mark_face_embedded(conn, "n_000", embedding=[0.0, 1.0], magface_quality=11.7, quality_score=0.90)
    conn.close()

    def fake_cluster(embeddings, min_cluster_size, min_samples):
        assert len(embeddings) == 5
        return [0, 0, 1, 1, -1], [0.99, 0.98, 0.97, 0.96, 0.0]

    monkeypatch.setattr("hikbox_pictures.face_review_pipeline._cluster_with_hdbscan", fake_cluster)

    payload = run_cluster_stage(
        source_dir=source_dir,
        output_dir=output_dir,
        detector_model_name="buffalo_l",
        det_size=640,
        min_cluster_size=2,
        min_samples=1,
        person_merge_threshold=0.03,
        person_rep_top_k=2,
        person_knn_k=2,
        person_linkage="average",
        person_enable_same_photo_cannot_link=False,
        preview_max_side=480,
        magface_checkpoint=Path(".cache/magface/magface_iresnet100_ms1mv2.pth"),
        low_quality_micro_cluster_max_size=3,
        low_quality_micro_cluster_top2_weight=0.5,
        low_quality_micro_cluster_min_quality_evidence=0.65,
        face_min_quality_for_assignment=None,
    )

    assert payload["meta"]["cluster_count"] == 1
    assert payload["meta"]["noise_count"] == 3
    assert payload["meta"]["person_count"] == 1
    assert payload["meta"]["low_quality_micro_cluster_max_size"] == 3
    assert payload["meta"]["low_quality_micro_cluster_top2_weight"] == 0.5
    assert payload["meta"]["low_quality_micro_cluster_min_quality_evidence"] == 0.65
    assert payload["meta"]["low_quality_micro_cluster_demoted_cluster_count"] == 1
    assert payload["meta"]["low_quality_micro_cluster_demoted_face_count"] == 2

    noise_cluster = next(cluster for cluster in payload["clusters"] if cluster["cluster_label"] == -1)
    noise_face_ids = {member["face_id"] for member in noise_cluster["members"]}
    assert {"l_000", "l_001", "n_000"}.issubset(noise_face_ids)
    for member in noise_cluster["members"]:
        if member["face_id"] in {"l_000", "l_001"}:
            assert member["cluster_assignment_source"] == "noise"


def test_run_cluster_stage_can_reassign_non_noise_micro_cluster_to_anchor_person(tmp_path: Path, monkeypatch) -> None:
    output_dir = tmp_path / "out"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "dummy.jpg").write_bytes(b"x")

    db_path = output_dir / "cache" / "pipeline.db"
    conn = open_pipeline_db(db_path)
    rows = [
        {
            "face_id": "a_000",
            "photo_relpath": "album/a.jpg",
            "crop_relpath": "assets/crops/a_000.jpg",
            "context_relpath": "assets/context/a_000.jpg",
            "preview_relpath": "assets/preview/a.jpg",
            "aligned_relpath": "assets/aligned/a_000.png",
            "bbox": [1, 2, 3, 4],
            "detector_confidence": 0.95,
            "face_area_ratio": 0.032,
        },
        {
            "face_id": "a_001",
            "photo_relpath": "album/a.jpg",
            "crop_relpath": "assets/crops/a_001.jpg",
            "context_relpath": "assets/context/a_001.jpg",
            "preview_relpath": "assets/preview/a.jpg",
            "aligned_relpath": "assets/aligned/a_001.png",
            "bbox": [5, 6, 7, 8],
            "detector_confidence": 0.94,
            "face_area_ratio": 0.030,
        },
        {
            "face_id": "a_002",
            "photo_relpath": "album/a.jpg",
            "crop_relpath": "assets/crops/a_002.jpg",
            "context_relpath": "assets/context/a_002.jpg",
            "preview_relpath": "assets/preview/a.jpg",
            "aligned_relpath": "assets/aligned/a_002.png",
            "bbox": [9, 10, 11, 12],
            "detector_confidence": 0.93,
            "face_area_ratio": 0.028,
        },
        {
            "face_id": "a_003",
            "photo_relpath": "album/a.jpg",
            "crop_relpath": "assets/crops/a_003.jpg",
            "context_relpath": "assets/context/a_003.jpg",
            "preview_relpath": "assets/preview/a.jpg",
            "aligned_relpath": "assets/aligned/a_003.png",
            "bbox": [13, 14, 15, 16],
            "detector_confidence": 0.92,
            "face_area_ratio": 0.027,
        },
        {
            "face_id": "b_000",
            "photo_relpath": "album/b.jpg",
            "crop_relpath": "assets/crops/b_000.jpg",
            "context_relpath": "assets/context/b_000.jpg",
            "preview_relpath": "assets/preview/b.jpg",
            "aligned_relpath": "assets/aligned/b_000.png",
            "bbox": [17, 18, 19, 20],
            "detector_confidence": 0.91,
            "face_area_ratio": 0.026,
        },
        {
            "face_id": "b_001",
            "photo_relpath": "album/b.jpg",
            "crop_relpath": "assets/crops/b_001.jpg",
            "context_relpath": "assets/context/b_001.jpg",
            "preview_relpath": "assets/preview/b.jpg",
            "aligned_relpath": "assets/aligned/b_001.png",
            "bbox": [21, 22, 23, 24],
            "detector_confidence": 0.90,
            "face_area_ratio": 0.025,
        },
        {
            "face_id": "b_002",
            "photo_relpath": "album/b.jpg",
            "crop_relpath": "assets/crops/b_002.jpg",
            "context_relpath": "assets/context/b_002.jpg",
            "preview_relpath": "assets/preview/b.jpg",
            "aligned_relpath": "assets/aligned/b_002.png",
            "bbox": [25, 26, 27, 28],
            "detector_confidence": 0.89,
            "face_area_ratio": 0.024,
        },
        {
            "face_id": "b_003",
            "photo_relpath": "album/b.jpg",
            "crop_relpath": "assets/crops/b_003.jpg",
            "context_relpath": "assets/context/b_003.jpg",
            "preview_relpath": "assets/preview/b.jpg",
            "aligned_relpath": "assets/aligned/b_003.png",
            "bbox": [29, 30, 31, 32],
            "detector_confidence": 0.88,
            "face_area_ratio": 0.023,
        },
        {
            "face_id": "c_000",
            "photo_relpath": "album/c.jpg",
            "crop_relpath": "assets/crops/c_000.jpg",
            "context_relpath": "assets/context/c_000.jpg",
            "preview_relpath": "assets/preview/c.jpg",
            "aligned_relpath": "assets/aligned/c_000.png",
            "bbox": [33, 34, 35, 36],
            "detector_confidence": 0.93,
            "face_area_ratio": 0.026,
        },
        {
            "face_id": "c_001",
            "photo_relpath": "album/c.jpg",
            "crop_relpath": "assets/crops/c_001.jpg",
            "context_relpath": "assets/context/c_001.jpg",
            "preview_relpath": "assets/preview/c.jpg",
            "aligned_relpath": "assets/aligned/c_001.png",
            "bbox": [37, 38, 39, 40],
            "detector_confidence": 0.92,
            "face_area_ratio": 0.025,
        },
    ]
    for row in rows:
        upsert_detected_face(conn, row)

    mark_face_embedded(conn, "a_000", embedding=[1.0, 0.0], magface_quality=12.3, quality_score=0.95)
    mark_face_embedded(conn, "a_001", embedding=[0.998, 0.060], magface_quality=12.0, quality_score=0.93)
    mark_face_embedded(conn, "a_002", embedding=[0.996, 0.089], magface_quality=11.9, quality_score=0.92)
    mark_face_embedded(conn, "a_003", embedding=[0.992, 0.123], magface_quality=11.8, quality_score=0.90)

    mark_face_embedded(conn, "b_000", embedding=[0.989, 0.145], magface_quality=11.7, quality_score=0.89)
    mark_face_embedded(conn, "b_001", embedding=[0.984, 0.177], magface_quality=11.6, quality_score=0.88)
    mark_face_embedded(conn, "b_002", embedding=[0.978, 0.208], magface_quality=11.5, quality_score=0.87)
    mark_face_embedded(conn, "b_003", embedding=[0.971, 0.239], magface_quality=11.4, quality_score=0.86)

    mark_face_embedded(conn, "c_000", embedding=[0.865, 0.502], magface_quality=11.9, quality_score=0.91)
    mark_face_embedded(conn, "c_001", embedding=[0.842, 0.539], magface_quality=11.8, quality_score=0.90)
    conn.close()

    def fake_cluster(embeddings, min_cluster_size, min_samples):
        assert len(embeddings) == 10
        return [0, 0, 0, 0, 1, 1, 1, 1, 2, 2], [0.99] * 10

    monkeypatch.setattr("hikbox_pictures.face_review_pipeline._cluster_with_hdbscan", fake_cluster)

    payload = run_cluster_stage(
        source_dir=source_dir,
        output_dir=output_dir,
        detector_model_name="buffalo_l",
        det_size=640,
        min_cluster_size=2,
        min_samples=1,
        person_merge_threshold=0.03,
        person_rep_top_k=2,
        person_knn_k=2,
        person_linkage="average",
        person_enable_same_photo_cannot_link=False,
        preview_max_side=480,
        magface_checkpoint=Path(".cache/magface/magface_iresnet100_ms1mv2.pth"),
        person_cluster_recall_distance_threshold=0.30,
        person_cluster_recall_margin_threshold=0.04,
        person_cluster_recall_top_n=5,
        person_cluster_recall_min_votes=3,
        person_cluster_recall_source_max_cluster_size=3,
        person_cluster_recall_source_max_person_faces=8,
        person_cluster_recall_target_min_person_faces=8,
        person_cluster_recall_max_rounds=2,
    )

    assert payload["meta"]["person_cluster_recall_attach_count"] == 1
    assert payload["meta"]["person_cluster_recall_round_count"] >= 1
    assert len(payload["person_cluster_recall_events"]) == 1
    assert payload["person_cluster_recall_events"][0]["cluster_label"] == 2
    assert payload["persons"][0]["person_face_count"] == 10
    assert {cluster["cluster_label"] for cluster in payload["persons"][0]["clusters"]} == {0, 1, 2}


def test_sqlite_roundtrip_for_two_phase_pipeline(tmp_path: Path) -> None:
    db_path = tmp_path / "pipeline.db"
    conn = open_pipeline_db(db_path)
    rows = [
        {
            "face_id": "a_000",
            "photo_relpath": "album/a.jpg",
            "crop_relpath": "assets/crops/a_000.jpg",
            "context_relpath": "assets/context/a_000.jpg",
            "preview_relpath": "assets/preview/a.jpg",
            "aligned_relpath": "assets/aligned/a_000.png",
            "bbox": [1, 2, 3, 4],
            "detector_confidence": 0.95,
            "face_area_ratio": 0.032,
        },
        {
            "face_id": "b_001",
            "photo_relpath": "album/b.jpg",
            "crop_relpath": "assets/crops/b_001.jpg",
            "context_relpath": "assets/context/b_001.jpg",
            "preview_relpath": "assets/preview/b.jpg",
            "aligned_relpath": "assets/aligned/b_001.png",
            "bbox": [10, 20, 30, 40],
            "detector_confidence": 0.85,
            "face_area_ratio": 0.021,
        },
    ]

    for row in rows:
        upsert_detected_face(conn, row)

    pending = list(iter_faces_pending_embedding(conn))
    assert [x["face_id"] for x in pending] == ["a_000", "b_001"]

    mark_face_embedded(conn, "a_000", embedding=[0.1, 0.2], magface_quality=12.3, quality_score=0.81)
    mark_face_embedded(conn, "b_001", embedding=[0.3, 0.4], magface_quality=11.1, quality_score=0.72)

    embedded = list(iter_embedded_faces(conn))
    assert len(embedded) == 2
    assert embedded[0]["bbox"] == [1, 2, 3, 4]
    assert embedded[0]["embedding"] == pytest.approx([0.1, 0.2], rel=1e-6, abs=1e-6)
    assert embedded[0]["cluster_assignment_source"] is None
    assert embedded[1]["embedding"] == pytest.approx([0.3, 0.4], rel=1e-6, abs=1e-6)
    assert embedded[1]["cluster_assignment_source"] is None

    conn.close()


def test_open_pipeline_db_creates_required_source_staged_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "pipeline.db"
    conn = open_pipeline_db(db_path)
    table_names = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    detected_faces_columns = [row[1] for row in conn.execute("PRAGMA table_info(detected_faces)").fetchall()]
    source_images_columns = [row[1] for row in conn.execute("PRAGMA table_info(source_images)").fetchall()]
    source_rows = conn.execute("SELECT COUNT(*) AS c FROM pipeline_sources").fetchone()["c"]
    conn.close()

    assert "embedding_blob" in detected_faces_columns
    assert "embedding_dtype" in detected_faces_columns
    assert "cluster_assignment_source" in detected_faces_columns
    assert "source_key" in source_images_columns
    assert "detect_status" in source_images_columns
    assert source_rows == 0
    assert "processed_images" not in table_names
    assert "failed_images" not in table_names
