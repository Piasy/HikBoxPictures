from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image

from hikbox_pictures.immich_face_single_file import BoundingBox
from hikbox_pictures.immich_face_single_file import DetectedFace
from hikbox_pictures.immich_people_review import write_people_review_html_from_summary
from hikbox_pictures.immich_people_review import write_people_summary


class FakeBackend:
    def __init__(self, faces_by_name: dict[str, list[DetectedFace]]) -> None:
        self._faces_by_name = faces_by_name

    def detect_faces(self, image_path: Path, *, min_score: float) -> tuple[int, int, list[DetectedFace]]:
        return 240, 320, list(self._faces_by_name[image_path.name])


def _unit_vector(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    vector = rng.normal(size=512).astype(np.float32)
    norm = float(np.linalg.norm(vector))
    if norm > 1e-9:
        vector = vector / norm
    return vector


def _near_vector(base: np.ndarray, noise_seed: int, *, weight: float) -> np.ndarray:
    noise = _unit_vector(noise_seed)
    mixed = ((1.0 - weight) * base) + (weight * noise)
    norm = float(np.linalg.norm(mixed))
    if norm > 1e-9:
        mixed = mixed / norm
    return mixed.astype(np.float32)


def test_write_people_review_groups_original_images_by_person(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    output_dir = tmp_path / "review"
    summary_json = tmp_path / "summary.json"
    input_root.mkdir()
    for file_name, color in (
        ("a.jpg", (220, 180, 160)),
        ("b.jpg", (210, 170, 150)),
        ("c.jpg", (200, 160, 140)),
        ("d.jpg", (190, 150, 130)),
    ):
        Image.new("RGB", (320, 240), color=color).save(input_root / file_name)

    base = _unit_vector(11)
    backend = FakeBackend(
        {
            "a.jpg": [
                DetectedFace(
                    bounding_box=BoundingBox(x1=20.0, y1=20.0, x2=120.0, y2=180.0),
                    embedding=_near_vector(base, 101, weight=0.01),
                    score=0.99,
                )
            ],
            "b.jpg": [
                DetectedFace(
                    bounding_box=BoundingBox(x1=24.0, y1=18.0, x2=124.0, y2=178.0),
                    embedding=_near_vector(base, 102, weight=0.015),
                    score=0.98,
                )
            ],
            "c.jpg": [
                DetectedFace(
                    bounding_box=BoundingBox(x1=22.0, y1=22.0, x2=122.0, y2=182.0),
                    embedding=_near_vector(base, 103, weight=0.02),
                    score=0.97,
                )
            ],
            "d.jpg": [
                DetectedFace(
                    bounding_box=BoundingBox(x1=26.0, y1=16.0, x2=126.0, y2=176.0),
                    embedding=_near_vector(base, 104, weight=0.012),
                    score=0.99,
                )
            ],
        }
    )

    summary_result = write_people_summary(
        input_root=input_root,
        summary_json_path=summary_json,
        backend=backend,
        min_faces=1,
        max_distance=0.5,
    )
    html_result = write_people_review_html_from_summary(
        summary_json_path=summary_json,
        output_dir=output_dir,
    )

    review_html = (output_dir / "review.html").read_text(encoding="utf-8")
    summary = json.loads(summary_json.read_text(encoding="utf-8"))
    meta = json.loads((output_dir / "review_payload_meta.json").read_text(encoding="utf-8"))

    assert summary_result["person_count"] == 1
    assert html_result["person_count"] == 1
    assert summary["meta"]["input_root"] == str(input_root.resolve())
    face_entry = summary["persons"][0]["assets"][0]["faces"][0]
    assert face_entry["crop_path"].endswith(".jpg")
    assert face_entry["context_path"].endswith(".jpg")
    assert Path(face_entry["crop_path"]).exists()
    assert Path(face_entry["context_path"]).exists()
    crop_image = Image.open(face_entry["crop_path"])
    context_image = Image.open(face_entry["context_path"])
    try:
        assert crop_image.size == (100, 160)
        assert context_image.size == (640, 480)
        box_pixel = context_image.getpixel((40, 40))
        assert box_pixel[0] > box_pixel[1]
        assert box_pixel[0] > box_pixel[2]
    finally:
        crop_image.close()
        context_image.close()
    assert meta["image_count"] == 4
    assert meta["face_count"] == 4
    assert meta["person_count"] == 1
    assert summary["persons"][0]["asset_count"] == 4
    assert summary["persons"][0]["assets"][0]["image_path"].endswith("a.jpg")
    assert summary["persons"][0]["assets"][1]["image_path"].endswith("b.jpg")
    assert summary["persons"][0]["assets"][2]["image_path"].endswith("c.jpg")
    assert summary["persons"][0]["assets"][3]["image_path"].endswith("d.jpg")
    assert (output_dir / "manifest.json").read_text(encoding="utf-8") == summary_json.read_text(encoding="utf-8")
    assert "a.jpg" in review_html
    assert "b.jpg" in review_html
    assert "c.jpg" in review_html
    assert "d.jpg" in review_html
    assert Path(face_entry["crop_path"]).name in review_html
    assert Path(face_entry["context_path"]).name in review_html
    assert review_html.count('class="asset-card"') == 4
    assert "grid-template-columns: repeat(4, minmax(0, 1fr));" in review_html
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in review_html
    assert "height: 156px;" in review_html
