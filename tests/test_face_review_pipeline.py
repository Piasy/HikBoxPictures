from pathlib import Path

from hikbox_pictures.face_review_pipeline import (
    group_faces_by_cluster,
    iter_embedded_faces,
    iter_faces_pending_embedding,
    iter_image_files,
    mark_face_embedded,
    open_pipeline_db,
    render_review_html,
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
                        "preview_relpath": "assets/preview/p.jpg",
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
    assert "assets/preview/p.jpg" in html


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
    assert embedded[1]["embedding"] == [0.3, 0.4]

    conn.close()
