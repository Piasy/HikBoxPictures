from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image

from hikbox_pictures.immich_face_single_file import BoundingBox
from hikbox_pictures.immich_face_single_file import DetectedFace
from hikbox_pictures.immich_face_single_file import ImmichLikeFaceEngine
from hikbox_pictures.immich_face_single_file import InsightFaceImmichBackend
from hikbox_pictures.immich_face_single_file import load_rgb_image_with_exif


class FakeBackend:
    def __init__(self, faces_by_path: dict[str, list[DetectedFace]]) -> None:
        self._faces_by_path = faces_by_path
        self.calls: list[str] = []

    def detect_faces(self, image_path: Path, *, min_score: float) -> tuple[int, int, list[DetectedFace]]:
        self.calls.append(str(image_path))
        return 480, 640, list(self._faces_by_path[str(image_path)])


class SequenceBackend:
    def __init__(self, responses_by_path: dict[str, list[list[DetectedFace]]]) -> None:
        self._responses_by_path = {key: list(value) for key, value in responses_by_path.items()}
        self.calls: list[str] = []

    def detect_faces(self, image_path: Path, *, min_score: float) -> tuple[int, int, list[DetectedFace]]:
        self.calls.append(str(image_path))
        queue = self._responses_by_path[str(image_path)]
        return 480, 640, list(queue.pop(0))


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


def test_detect_stage_runs_ml_once_and_assignment_reuses_stored_embedding(tmp_path: Path) -> None:
    image_path = tmp_path / "asset-1.jpg"
    image_path.write_bytes(b"fake")
    backend = FakeBackend(
        {
            str(image_path): [
                DetectedFace(
                    bounding_box=BoundingBox(x1=10.0, y1=20.0, x2=110.0, y2=180.0),
                    embedding=_unit_vector(1),
                    score=0.99,
                )
            ]
        }
    )
    engine = ImmichLikeFaceEngine(backend=backend, min_faces=1, max_distance=0.5)
    engine.add_asset(asset_id="asset-1", image_path=image_path)

    detect_result = engine.detect_asset_faces("asset-1")
    face_id = detect_result.new_face_ids[0]
    recognize_result = engine.recognize_face(face_id)

    assert backend.calls == [str(image_path)]
    assert detect_result.new_face_ids == [face_id]
    assert detect_result.removed_face_ids == []
    assert recognize_result.status == "assigned"
    assert recognize_result.person_id is not None
    assert engine.faces[face_id].person_id == recognize_result.person_id
    assert engine.face_search.count == 1


def test_redetect_reuses_face_id_by_iou_without_refreshing_old_embedding_or_person(tmp_path: Path) -> None:
    image_path = tmp_path / "asset-2.jpg"
    image_path.write_bytes(b"fake")
    original_embedding = _unit_vector(10)
    changed_embedding = _unit_vector(11)
    newcomer_embedding = _unit_vector(12)
    backend = SequenceBackend(
        {
            str(image_path): [
                [
                    DetectedFace(
                        bounding_box=BoundingBox(x1=20.0, y1=20.0, x2=120.0, y2=180.0),
                        embedding=original_embedding,
                        score=0.98,
                    ),
                    DetectedFace(
                        bounding_box=BoundingBox(x1=220.0, y1=25.0, x2=320.0, y2=185.0),
                        embedding=_unit_vector(20),
                        score=0.97,
                    ),
                ],
                [
                    DetectedFace(
                        bounding_box=BoundingBox(x1=21.0, y1=21.0, x2=121.0, y2=181.0),
                        embedding=changed_embedding,
                        score=0.99,
                    ),
                    DetectedFace(
                        bounding_box=BoundingBox(x1=380.0, y1=30.0, x2=460.0, y2=170.0),
                        embedding=newcomer_embedding,
                        score=0.96,
                    ),
                ],
            ]
        }
    )
    engine = ImmichLikeFaceEngine(backend=backend, min_faces=1, max_distance=0.5)
    engine.add_asset(asset_id="asset-2", image_path=image_path)

    first_detect = engine.detect_asset_faces("asset-2")
    reused_face_id, removed_later_face_id = first_detect.new_face_ids
    first_person_id = engine.recognize_face(reused_face_id).person_id

    redetect = engine.detect_asset_faces("asset-2")
    newcomer_face_id = redetect.new_face_ids[0]

    assert redetect.matched_face_ids == [reused_face_id]
    assert redetect.removed_face_ids == [removed_later_face_id]
    assert newcomer_face_id not in {reused_face_id, removed_later_face_id}
    assert engine.faces[reused_face_id].person_id == first_person_id
    assert np.allclose(engine.faces[reused_face_id].embedding, original_embedding)
    assert not np.allclose(engine.faces[reused_face_id].embedding, changed_embedding)
    assert np.allclose(engine.faces[newcomer_face_id].embedding, newcomer_embedding)
    assert removed_later_face_id not in engine.faces
    assert engine.face_search.count == 2


def test_detect_stage_only_queues_new_faces_for_recognition(tmp_path: Path) -> None:
    image_path = tmp_path / "asset-3.jpg"
    image_path.write_bytes(b"fake")
    backend = SequenceBackend(
        {
            str(image_path): [
                [
                    DetectedFace(
                        bounding_box=BoundingBox(x1=20.0, y1=20.0, x2=120.0, y2=180.0),
                        embedding=_unit_vector(30),
                        score=0.98,
                    ),
                ],
                [
                    DetectedFace(
                        bounding_box=BoundingBox(x1=21.0, y1=21.0, x2=121.0, y2=181.0),
                        embedding=_unit_vector(31),
                        score=0.99,
                    ),
                    DetectedFace(
                        bounding_box=BoundingBox(x1=200.0, y1=40.0, x2=280.0, y2=170.0),
                        embedding=_unit_vector(32),
                        score=0.97,
                    ),
                ],
            ]
        }
    )
    engine = ImmichLikeFaceEngine(backend=backend, min_faces=1, max_distance=0.5)
    engine.add_asset(asset_id="asset-3", image_path=image_path)

    first_detect = engine.detect_asset_faces("asset-3")
    engine.pending_recognition_face_ids.clear()
    second_detect = engine.detect_asset_faces("asset-3")

    assert engine.pending_recognition_face_ids == second_detect.new_face_ids
    assert first_detect.new_face_ids[0] not in engine.pending_recognition_face_ids


def test_stream_for_detect_faces_respects_force_flag_and_latest_first(tmp_path: Path) -> None:
    backend = FakeBackend({})
    engine = ImmichLikeFaceEngine(backend=backend, min_faces=1, max_distance=0.5)
    older = engine.add_asset(
        asset_id="asset-old",
        image_path=tmp_path / "asset-old.jpg",
        file_created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    newer = engine.add_asset(
        asset_id="asset-new",
        image_path=tmp_path / "asset-new.jpg",
        file_created_at=datetime(2024, 2, 1, tzinfo=UTC),
    )
    newer.faces_recognized_at = datetime(2024, 2, 2, tzinfo=UTC)

    assert list(engine.stream_assets_for_detect(force=False)) == [older.id]
    assert list(engine.stream_assets_for_detect(force=True)) == [newer.id, older.id]


def test_process_pending_recognition_queue_assigns_similar_faces_to_one_person(tmp_path: Path) -> None:
    image_paths = [tmp_path / f"asset-{index}.jpg" for index in range(3)]
    for image_path in image_paths:
        image_path.write_bytes(b"fake")
    base = _unit_vector(90)
    backend = FakeBackend(
        {
            str(image_paths[0]): [
                DetectedFace(
                    bounding_box=BoundingBox(x1=10.0, y1=20.0, x2=110.0, y2=180.0),
                    embedding=_near_vector(base, 901, weight=0.01),
                    score=0.99,
                )
            ],
            str(image_paths[1]): [
                DetectedFace(
                    bounding_box=BoundingBox(x1=15.0, y1=25.0, x2=115.0, y2=185.0),
                    embedding=_near_vector(base, 902, weight=0.015),
                    score=0.98,
                )
            ],
            str(image_paths[2]): [
                DetectedFace(
                    bounding_box=BoundingBox(x1=20.0, y1=30.0, x2=120.0, y2=190.0),
                    embedding=_near_vector(base, 903, weight=0.02),
                    score=0.97,
                )
            ],
        }
    )
    engine = ImmichLikeFaceEngine(backend=backend, min_faces=3, max_distance=0.05)
    for index, image_path in enumerate(image_paths):
        engine.add_asset(asset_id=f"asset-{index}", image_path=image_path)
        engine.detect_asset_faces(f"asset-{index}")

    results = engine.process_pending_recognition_queue()

    assert len(results) == 3
    assert all(item.status == "assigned" for item in results)
    person_ids = {item.person_id for item in results}
    assert len(person_ids) == 1
    assert None not in person_ids
    assert engine.pending_recognition_face_ids == []
    assert len(engine.people) == 1


def test_load_rgb_image_with_exif_applies_orientation(tmp_path: Path) -> None:
    image_path = tmp_path / "rotated.jpg"
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (100, 60), color=(220, 180, 160)).save(image_path, exif=exif)

    image = load_rgb_image_with_exif(image_path)
    try:
        assert image.size == (60, 100)
    finally:
        image.close()


def test_insightface_backend_detect_faces_does_not_reset_detector_input_size(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from insightface.model_zoo import model_zoo

    class FakeDetector:
        def __init__(self) -> None:
            self.prepare_calls: list[dict[str, object]] = []

        def prepare(self, ctx_id: int, **kwargs: object) -> None:
            self.prepare_calls.append({"ctx_id": ctx_id, **kwargs})

        def detect(self, image: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
            return (
                np.empty((0, 5), dtype=np.float32),
                np.empty((0, 5, 2), dtype=np.float32),
            )

    class FakeRecognizer:
        pass

    detector = FakeDetector()
    recognizer = FakeRecognizer()

    def fake_get_model(path: str, providers: list[str] | None = None) -> object:
        if path.endswith("det_10g.onnx"):
            return detector
        if path.endswith("w600k_r50.onnx"):
            return recognizer
        raise AssertionError(f"unexpected model path: {path}")

    monkeypatch.setattr(model_zoo, "get_model", fake_get_model)

    model_root = tmp_path / ".insightface"
    model_dir = model_root / "models" / "buffalo_l"
    model_dir.mkdir(parents=True)
    (model_dir / "det_10g.onnx").write_bytes(b"fake")
    (model_dir / "w600k_r50.onnx").write_bytes(b"fake")

    image_path = tmp_path / "asset.jpg"
    Image.new("RGB", (32, 24), color=(128, 96, 64)).save(image_path)

    backend = InsightFaceImmichBackend(model_root=model_root, min_score=0.7)
    backend.detect_faces(image_path, min_score=0.8)

    assert detector.prepare_calls == [
        {"ctx_id": 0, "det_thresh": 0.7, "input_size": (640, 640)},
        {"ctx_id": 0, "det_thresh": 0.8},
    ]
