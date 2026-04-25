from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import numpy as np
from PIL import Image
from PIL import UnidentifiedImageError

from hikbox_pictures.immich_face_single_file import BoundingBox
from hikbox_pictures.immich_face_single_file import DetectedFace
from hikbox_pictures import immich_people_review
from hikbox_pictures.immich_people_review import run_people_summary_batch
from hikbox_pictures.immich_people_review import write_people_review_html_from_summary
from hikbox_pictures.immich_people_review import write_people_summary_batched
from hikbox_pictures.immich_people_review import write_people_summary


class FakeBackend:
    def __init__(self, faces_by_name: dict[str, list[DetectedFace]]) -> None:
        self._faces_by_name = faces_by_name
        self.calls: list[str] = []

    def detect_faces(self, image_path: Path, *, min_score: float) -> tuple[int, int, list[DetectedFace]]:
        self.calls.append(image_path.name)
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


def test_write_people_summary_reuses_sqlite_people_across_new_directories(tmp_path: Path) -> None:
    input_root_a = tmp_path / "input-a"
    input_root_b = tmp_path / "input-b"
    input_root_a.mkdir()
    input_root_b.mkdir()
    Image.new("RGB", (320, 240), color=(220, 180, 160)).save(input_root_a / "a.jpg")
    Image.new("RGB", (320, 240), color=(210, 170, 150)).save(input_root_b / "b.jpg")
    summary_json_a = tmp_path / "summary-a.json"
    summary_json_b = tmp_path / "summary-b.json"
    db_path = tmp_path / "people.sqlite3"

    base = _unit_vector(201)
    backend = FakeBackend(
        {
            "a.jpg": [
                DetectedFace(
                    bounding_box=BoundingBox(x1=20.0, y1=20.0, x2=120.0, y2=180.0),
                    embedding=_near_vector(base, 301, weight=0.01),
                    score=0.99,
                )
            ],
            "b.jpg": [
                DetectedFace(
                    bounding_box=BoundingBox(x1=24.0, y1=18.0, x2=124.0, y2=178.0),
                    embedding=_near_vector(base, 302, weight=0.015),
                    score=0.98,
                )
            ],
        }
    )

    write_people_summary(
        input_root=input_root_a,
        summary_json_path=summary_json_a,
        backend=backend,
        db_path=db_path,
        min_faces=1,
        max_distance=0.5,
    )
    write_people_summary(
        input_root=input_root_b,
        summary_json_path=summary_json_b,
        backend=backend,
        db_path=db_path,
        min_faces=1,
        max_distance=0.5,
    )

    summary_a = json.loads(summary_json_a.read_text(encoding="utf-8"))
    summary_b = json.loads(summary_json_b.read_text(encoding="utf-8"))
    assert backend.calls == ["a.jpg", "b.jpg"]
    assert summary_a["meta"]["image_count"] == 1
    assert summary_b["meta"]["image_count"] == 1
    assert summary_a["persons"][0]["person_id"] == summary_b["persons"][0]["person_id"]
    assert summary_b["persons"][0]["assets"][0]["image_path"].endswith("b.jpg")
    assert summary_b["persons"][0]["asset_count"] == 1

    conn = sqlite3.connect(db_path)
    try:
        asset_count = conn.execute("SELECT COUNT(*) FROM immich_people_asset").fetchone()[0]
        face_count = conn.execute("SELECT COUNT(*) FROM immich_people_face").fetchone()[0]
        person_count = conn.execute("SELECT COUNT(*) FROM immich_people_person").fetchone()[0]
        source_count = conn.execute("SELECT COUNT(*) FROM immich_people_source").fetchone()[0]
        face_columns = {row[1] for row in conn.execute("PRAGMA table_info(immich_people_face)")}
    finally:
        conn.close()
    assert asset_count == 2
    assert face_count == 2
    assert person_count == 1
    assert source_count == 2
    assert "source_type" not in face_columns


def test_write_people_summary_generates_context_with_exif_orientation(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    image_path = input_root / "rotated.jpg"
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (100, 60), color=(220, 180, 160)).save(image_path, exif=exif)
    summary_json = tmp_path / "summary.json"

    class FakeSizedBackend:
        def detect_faces(self, image_path: Path, *, min_score: float) -> tuple[int, int, list[DetectedFace]]:
            return (
                100,
                60,
                [
                    DetectedFace(
                        bounding_box=BoundingBox(x1=10.0, y1=20.0, x2=30.0, y2=50.0),
                        embedding=_unit_vector(1201),
                        score=0.99,
                    )
                ],
            )

    result = write_people_summary(
        input_root=input_root,
        summary_json_path=summary_json,
        backend=FakeSizedBackend(),
        min_faces=1,
        max_distance=0.5,
    )

    assert result["image_count"] == 1
    summary = json.loads(summary_json.read_text(encoding="utf-8"))
    face_entry = summary["persons"][0]["assets"][0]["faces"][0]
    context_image = Image.open(face_entry["context_path"])
    try:
        assert context_image.size == (480, 800)
    finally:
        context_image.close()


def test_write_people_summary_batched_reuses_sqlite_state_across_batches(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    for file_name, color in (
        ("a.jpg", (220, 180, 160)),
        ("b.jpg", (210, 170, 150)),
        ("c.jpg", (200, 160, 140)),
    ):
        Image.new("RGB", (320, 240), color=color).save(input_root / file_name)
    summary_json = tmp_path / "summary.json"
    db_path = tmp_path / "people.sqlite3"

    base = _unit_vector(601)
    faces_by_name = {
        "a.jpg": [
            DetectedFace(
                bounding_box=BoundingBox(x1=20.0, y1=20.0, x2=120.0, y2=180.0),
                embedding=_near_vector(base, 701, weight=0.01),
                score=0.99,
            )
        ],
        "b.jpg": [
            DetectedFace(
                bounding_box=BoundingBox(x1=24.0, y1=18.0, x2=124.0, y2=178.0),
                embedding=_near_vector(base, 702, weight=0.015),
                score=0.98,
            )
        ],
        "c.jpg": [
            DetectedFace(
                bounding_box=BoundingBox(x1=22.0, y1=22.0, x2=122.0, y2=182.0),
                embedding=_near_vector(base, 703, weight=0.02),
                score=0.97,
            )
        ],
    }
    backend_instances: list[FakeBackend] = []

    def backend_factory() -> FakeBackend:
        backend = FakeBackend(faces_by_name)
        backend_instances.append(backend)
        return backend

    result = write_people_summary_batched(
        input_root=input_root,
        summary_json_path=summary_json,
        backend_factory=backend_factory,
        db_path=db_path,
        batch_size=1,
        min_faces=1,
        max_distance=0.5,
    )

    summary = json.loads(summary_json.read_text(encoding="utf-8"))
    assert result["image_count"] == 3
    assert result["face_count"] == 3
    assert result["person_count"] == 1
    assert len(backend_instances) == 3
    assert [backend.calls for backend in backend_instances] == [["a.jpg"], ["b.jpg"], ["c.jpg"]]
    assert summary["persons"][0]["asset_count"] == 3
    assert summary["meta"]["failed_image_count"] == 0


def test_run_people_summary_batch_generates_artifacts_when_summary_json_path_provided(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    image_path = input_root / "a.jpg"
    Image.new("RGB", (320, 240), color=(220, 180, 160)).save(image_path)
    summary_json = tmp_path / "summary.json"
    db_path = tmp_path / "people.sqlite3"

    backend = FakeBackend(
        {
            "a.jpg": [
                DetectedFace(
                    bounding_box=BoundingBox(x1=20.0, y1=20.0, x2=120.0, y2=180.0),
                    embedding=_unit_vector(1401),
                    score=0.99,
                )
            ],
        }
    )

    result = run_people_summary_batch(
        input_root=input_root,
        image_paths=[image_path],
        backend=backend,
        db_path=db_path,
        summary_json_path=summary_json,
        min_faces=1,
        max_distance=0.5,
    )

    artifact_by_face_id = result["artifact_by_face_id"]
    assert len(artifact_by_face_id) == 1
    artifact = next(iter(artifact_by_face_id.values()))
    assert Path(str(artifact["crop_path"])).exists()
    assert Path(str(artifact["context_path"])).exists()


def test_run_people_summary_batch_generates_artifacts_per_image(tmp_path: Path, monkeypatch) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    image_a = input_root / "a.jpg"
    image_b = input_root / "b.jpg"
    Image.new("RGB", (320, 240), color=(220, 180, 160)).save(image_a)
    Image.new("RGB", (320, 240), color=(210, 170, 150)).save(image_b)
    summary_json = tmp_path / "summary.json"
    db_path = tmp_path / "people.sqlite3"

    backend = FakeBackend(
        {
            "a.jpg": [
                DetectedFace(
                    bounding_box=BoundingBox(x1=20.0, y1=20.0, x2=120.0, y2=180.0),
                    embedding=_unit_vector(1451),
                    score=0.99,
                )
            ],
            "b.jpg": [
                DetectedFace(
                    bounding_box=BoundingBox(x1=24.0, y1=18.0, x2=124.0, y2=178.0),
                    embedding=_unit_vector(1452),
                    score=0.98,
                )
            ],
        }
    )

    generate_calls: list[int] = []
    real_generate = immich_people_review._generate_face_artifacts

    def tracking_generate_face_artifacts(*, summary_json_path: Path, engine, included_face_ids=None):
        generate_calls.append(len(included_face_ids or []))
        return real_generate(
            summary_json_path=summary_json_path,
            engine=engine,
            included_face_ids=included_face_ids,
        )

    monkeypatch.setattr(immich_people_review, "_generate_face_artifacts", tracking_generate_face_artifacts)

    result = run_people_summary_batch(
        input_root=input_root,
        image_paths=[image_a, image_b],
        backend=backend,
        db_path=db_path,
        summary_json_path=summary_json,
        min_faces=1,
        max_distance=0.5,
    )

    assert len(result["artifact_by_face_id"]) == 2
    assert generate_calls == [1, 1]


def test_write_people_summary_batched_reuses_worker_generated_artifacts(tmp_path: Path, monkeypatch) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    for file_name, color in (
        ("a.jpg", (220, 180, 160)),
        ("b.jpg", (210, 170, 150)),
        ("c.jpg", (200, 160, 140)),
    ):
        Image.new("RGB", (320, 240), color=color).save(input_root / file_name)
    summary_json = tmp_path / "summary.json"
    db_path = tmp_path / "people.sqlite3"

    base = _unit_vector(1501)
    faces_by_name = {
        "a.jpg": [
            DetectedFace(
                bounding_box=BoundingBox(x1=20.0, y1=20.0, x2=120.0, y2=180.0),
                embedding=_near_vector(base, 1601, weight=0.01),
                score=0.99,
            )
        ],
        "b.jpg": [
            DetectedFace(
                bounding_box=BoundingBox(x1=24.0, y1=18.0, x2=124.0, y2=178.0),
                embedding=_near_vector(base, 1602, weight=0.015),
                score=0.98,
            )
        ],
        "c.jpg": [
            DetectedFace(
                bounding_box=BoundingBox(x1=22.0, y1=22.0, x2=122.0, y2=182.0),
                embedding=_near_vector(base, 1603, weight=0.02),
                score=0.97,
            )
        ],
    }

    generate_calls: list[int] = []
    real_generate = immich_people_review._generate_face_artifacts

    def tracking_generate_face_artifacts(*, summary_json_path: Path, engine, included_face_ids=None):
        generate_calls.append(len(included_face_ids or []))
        return real_generate(
            summary_json_path=summary_json_path,
            engine=engine,
            included_face_ids=included_face_ids,
        )

    monkeypatch.setattr(immich_people_review, "_generate_face_artifacts", tracking_generate_face_artifacts)

    def batch_runner(batch_image_paths: list[Path]) -> dict[str, object]:
        return run_people_summary_batch(
            input_root=input_root,
            image_paths=batch_image_paths,
            backend=FakeBackend(faces_by_name),
            db_path=db_path,
            summary_json_path=summary_json,
            min_faces=1,
            max_distance=0.5,
        )

    result = write_people_summary_batched(
        input_root=input_root,
        summary_json_path=summary_json,
        batch_runner=batch_runner,
        db_path=db_path,
        batch_size=1,
        min_faces=1,
        max_distance=0.5,
    )

    assert result["image_count"] == 3
    assert generate_calls == [1, 1, 1]


def test_write_people_summary_registers_heif_opener_for_artifact_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    heic_path = input_root / "a.heic"
    heic_path.write_bytes(b"fake-heic")
    fallback_jpg = tmp_path / "fallback.jpg"
    Image.new("RGB", (320, 240), color=(220, 180, 160)).save(fallback_jpg)
    summary_json = tmp_path / "summary.json"

    base = _unit_vector(801)
    backend = FakeBackend(
        {
            "a.heic": [
                DetectedFace(
                    bounding_box=BoundingBox(x1=20.0, y1=20.0, x2=120.0, y2=180.0),
                    embedding=_near_vector(base, 901, weight=0.01),
                    score=0.99,
                )
            ],
        }
    )
    registered = {"value": False}
    real_open = immich_people_review.Image.open

    def fake_register_heif_opener() -> None:
        registered["value"] = True

    def fake_image_open(path: str | Path, *args, **kwargs):
        path_str = str(path)
        if path_str.endswith(".heic"):
            if not registered["value"]:
                raise UnidentifiedImageError(path_str)
            return real_open(fallback_jpg, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(immich_people_review, "_register_heif_opener", fake_register_heif_opener)
    monkeypatch.setattr(immich_people_review.Image, "open", fake_image_open)

    result = write_people_summary(
        input_root=input_root,
        summary_json_path=summary_json,
        backend=backend,
        min_faces=1,
        max_distance=0.5,
    )

    assert result["image_count"] == 1
    assert json.loads(summary_json.read_text(encoding="utf-8"))["meta"]["face_count"] == 1


def test_write_people_summary_closes_source_images_between_assets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    input_root = tmp_path / "input"
    input_root.mkdir()
    for file_name, color in (
        ("a.jpg", (220, 180, 160)),
        ("b.jpg", (210, 170, 150)),
    ):
        Image.new("RGB", (320, 240), color=color).save(input_root / file_name)
    summary_json = tmp_path / "summary.json"

    base = _unit_vector(1001)
    backend = FakeBackend(
        {
            "a.jpg": [
                DetectedFace(
                    bounding_box=BoundingBox(x1=20.0, y1=20.0, x2=120.0, y2=180.0),
                    embedding=_near_vector(base, 1101, weight=0.01),
                    score=0.99,
                )
            ],
            "b.jpg": [
                DetectedFace(
                    bounding_box=BoundingBox(x1=24.0, y1=18.0, x2=124.0, y2=178.0),
                    embedding=_near_vector(base, 1102, weight=0.015),
                    score=0.98,
                )
            ],
        }
    )
    real_open = immich_people_review.Image.open
    tracker = {"open_count": 0, "max_open_count": 0, "closed": []}

    class TrackingImage:
        def __init__(self, inner: Image.Image, path: str) -> None:
            self._inner = inner
            self._path = path
            tracker["open_count"] += 1
            tracker["max_open_count"] = max(tracker["max_open_count"], tracker["open_count"])

        def convert(self, *args, **kwargs):
            return self._inner.convert(*args, **kwargs)

        def close(self) -> None:
            if self._inner is None:
                return
            self._inner.close()
            self._inner = None
            tracker["open_count"] -= 1
            tracker["closed"].append(self._path)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            self.close()
            return False

        def __getattr__(self, item):
            if self._inner is None:
                raise AttributeError(item)
            return getattr(self._inner, item)

    def fake_image_open(path: str | Path, *args, **kwargs):
        image = real_open(path, *args, **kwargs)
        return TrackingImage(image, Path(path).name)

    monkeypatch.setattr(immich_people_review.Image, "open", fake_image_open)

    result = write_people_summary(
        input_root=input_root,
        summary_json_path=summary_json,
        backend=backend,
        min_faces=1,
        max_distance=0.5,
    )

    assert result["image_count"] == 2
    assert tracker["max_open_count"] == 1
    assert tracker["open_count"] == 0
    assert tracker["closed"] == ["a.jpg", "b.jpg"]
