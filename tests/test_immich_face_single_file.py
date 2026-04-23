from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from hikbox_pictures.immich_face_single_file import BoundingBox
from hikbox_pictures.immich_face_single_file import DetectedFace
from hikbox_pictures.immich_face_single_file import ImmichLikeFaceEngine


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
