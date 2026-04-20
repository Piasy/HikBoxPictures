from pathlib import Path

from hikbox_pictures.face_review_pipeline import (
    attach_noise_faces_to_person_consensus,
    group_faces_by_cluster,
    iter_embedded_faces,
    iter_faces_pending_embedding,
    iter_image_files,
    mark_face_embedded,
    merge_clusters_to_persons,
    open_pipeline_db,
    render_review_html,
    run_cluster_stage,
    set_meta,
    upsert_detected_face,
)


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
    set_meta(conn, "max_images", 1)
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
    set_meta(conn, "max_images", 1)
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
    set_meta(conn, "max_images", 1)
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
    assert embedded[0]["embedding"] == [0.1, 0.2]
    assert embedded[0]["cluster_assignment_source"] is None
    assert embedded[1]["embedding"] == [0.3, 0.4]
    assert embedded[1]["cluster_assignment_source"] is None

    conn.close()


def test_open_pipeline_db_migrates_cluster_assignment_source_column(tmp_path: Path) -> None:
    db_path = tmp_path / "pipeline.db"
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE detected_faces (
            face_id TEXT PRIMARY KEY,
            photo_relpath TEXT NOT NULL,
            crop_relpath TEXT NOT NULL,
            context_relpath TEXT NOT NULL,
            preview_relpath TEXT NOT NULL,
            aligned_relpath TEXT NOT NULL,
            bbox_json TEXT NOT NULL,
            detector_confidence REAL NOT NULL,
            face_area_ratio REAL NOT NULL,
            embedding_json TEXT,
            magface_quality REAL,
            quality_score REAL,
            cluster_label INTEGER,
            cluster_probability REAL,
            face_error TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE failed_images (
            photo_relpath TEXT PRIMARY KEY,
            error TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE pipeline_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.commit()
    conn.close()

    migrated = open_pipeline_db(db_path)
    columns = [row[1] for row in migrated.execute("PRAGMA table_info(detected_faces)").fetchall()]
    migrated.close()

    assert "cluster_assignment_source" in columns
