from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from hikbox_pictures.cli import main
from hikbox_pictures.db.connection import connect_db
from tests.people_gallery.real_image_helper import copy_group_face_image, copy_raw_face_image


@pytest.mark.real_face_engine
def test_embeddings_are_generated_by_real_deepface_pipeline(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    source_root = tmp_path / "input"
    portrait_path = copy_raw_face_image(source_root / "person-a.jpg", index=0)
    group_path = copy_group_face_image(source_root / "family-group.jpg", index=0)

    assert main(["init", "--workspace", str(workspace)]) == 0
    assert (
        main(
            [
                "source",
                "add",
                "--workspace",
                str(workspace),
                "--name",
                "sample-input",
                "--root-path",
                str(source_root),
            ]
        )
        == 0
    )
    assert main(["scan", "--workspace", str(workspace)]) == 0

    conn = connect_db(workspace / ".hikbox" / "library.db")
    try:
        first_embedding = conn.execute(
            """
            SELECT fe.dimension, fe.model_key, fo.detector_key, fo.detector_version, fo.crop_path
            FROM face_embedding AS fe
            JOIN face_observation AS fo
              ON fo.id = fe.face_observation_id
            ORDER BY fe.id ASC
            LIMIT 1
            """
        ).fetchone()
        assert first_embedding is not None
        assert int(first_embedding["dimension"]) >= 128
        assert str(first_embedding["model_key"]) != "pipeline-stub-v1"
        assert str(first_embedding["model_key"]).startswith("ArcFace@")
        assert str(first_embedding["detector_key"]) in {"retinaface", "yunet", "mtcnn"}
        assert str(first_embedding["detector_version"]) == "ArcFace"
        assert Path(str(first_embedding["crop_path"])).exists()

        portrait_count = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM face_observation AS fo
            JOIN photo_asset AS pa
              ON pa.id = fo.photo_asset_id
            WHERE pa.primary_path = ?
              AND fo.active = 1
            """,
            (str(portrait_path),),
        ).fetchone()
        group_count = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM face_observation AS fo
            JOIN photo_asset AS pa
              ON pa.id = fo.photo_asset_id
            WHERE pa.primary_path = ?
              AND fo.active = 1
            """,
            (str(group_path),),
        ).fetchone()
        assert portrait_count is not None
        assert group_count is not None
        assert int(portrait_count["c"]) >= 1
        assert int(group_count["c"]) >= 2
    finally:
        conn.close()


@pytest.mark.real_face_engine
def test_blank_photo_does_not_create_face_observation(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    source_root = tmp_path / "input"
    blank_path = source_root / "blank.jpg"
    source_root.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (128, 96), color=(255, 255, 255)).save(blank_path, format="JPEG")
    portrait_path = copy_raw_face_image(source_root / "person-a.jpg", index=0)

    assert main(["init", "--workspace", str(workspace)]) == 0
    assert (
        main(
            [
                "source",
                "add",
                "--workspace",
                str(workspace),
                "--name",
                "sample-input",
                "--root-path",
                str(source_root),
            ]
        )
        == 0
    )
    assert main(["scan", "--workspace", str(workspace)]) == 0

    conn = connect_db(workspace / ".hikbox" / "library.db")
    try:
        blank_rows = conn.execute(
            """
            SELECT
              (SELECT COUNT(*)
               FROM face_observation AS fo
               JOIN photo_asset AS pa
                 ON pa.id = fo.photo_asset_id
               WHERE pa.primary_path = ?
                 AND fo.active = 1) AS observation_count,
              (SELECT COUNT(*)
               FROM face_embedding AS fe
               JOIN face_observation AS fo
                 ON fo.id = fe.face_observation_id
               JOIN photo_asset AS pa
                 ON pa.id = fo.photo_asset_id
               WHERE pa.primary_path = ?
                 AND fo.active = 1) AS embedding_count
            """,
            (str(blank_path), str(blank_path)),
        ).fetchone()
        portrait_rows = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM face_observation AS fo
            JOIN photo_asset AS pa
              ON pa.id = fo.photo_asset_id
            WHERE pa.primary_path = ?
              AND fo.active = 1
            """,
            (str(portrait_path),),
        ).fetchone()
        assert blank_rows is not None
        assert portrait_rows is not None
        assert int(blank_rows["observation_count"]) == 0
        assert int(blank_rows["embedding_count"]) == 0
        assert int(portrait_rows["c"]) >= 1
    finally:
        conn.close()
