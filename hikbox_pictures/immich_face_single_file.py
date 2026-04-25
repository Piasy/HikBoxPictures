"""按 Immich 原理复现的人脸识别与人物归属单文件实现。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Protocol
import uuid

import hnswlib
import numpy as np
from PIL import Image
from PIL import ImageOps


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _normalize_vector(vector: np.ndarray) -> np.ndarray:
    safe = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(safe))
    if norm <= 1e-9:
        return safe
    return safe / norm


def load_rgb_image_with_exif(image_path: Path) -> Image.Image:
    with Image.open(image_path) as image:
        normalized = ImageOps.exif_transpose(image)
        return normalized.convert("RGB")


@dataclass(frozen=True)
class BoundingBox:
    x1: float
    y1: float
    x2: float
    y2: float

    def iou(self, other: "BoundingBox") -> float:
        x1 = max(self.x1, other.x1)
        y1 = max(self.y1, other.y1)
        x2 = min(self.x2, other.x2)
        y2 = min(self.y2, other.y2)
        inter_w = max(0.0, x2 - x1)
        inter_h = max(0.0, y2 - y1)
        intersection = inter_w * inter_h
        if intersection <= 0:
            return 0.0
        self_area = max(0.0, self.x2 - self.x1) * max(0.0, self.y2 - self.y1)
        other_area = max(0.0, other.x2 - other.x1) * max(0.0, other.y2 - other.y1)
        union = self_area + other_area - intersection
        if union <= 0:
            return 0.0
        return intersection / union

    def normalize(self, *, width: int, height: int) -> "BoundingBox":
        safe_width = max(int(width), 1)
        safe_height = max(int(height), 1)
        return BoundingBox(
            x1=self.x1 / safe_width,
            y1=self.y1 / safe_height,
            x2=self.x2 / safe_width,
            y2=self.y2 / safe_height,
        )


@dataclass(frozen=True)
class DetectedFace:
    bounding_box: BoundingBox
    embedding: np.ndarray
    score: float


@dataclass
class AssetRecord:
    id: str
    image_path: Path
    file_created_at: datetime = field(default_factory=_utcnow)
    faces_recognized_at: datetime | None = None
    face_ids: list[str] = field(default_factory=list)


@dataclass
class FaceRecord:
    id: str
    asset_id: str
    bounding_box: BoundingBox
    image_width: int
    image_height: int
    embedding: np.ndarray
    score: float
    person_id: str | None = None
    created_at: datetime = field(default_factory=_utcnow)


@dataclass
class PersonRecord:
    id: str
    face_ids: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class SearchMatch:
    face_id: str
    distance: float


@dataclass(frozen=True)
class DetectFacesResult:
    new_face_ids: list[str]
    removed_face_ids: list[str]
    matched_face_ids: list[str]


@dataclass(frozen=True)
class RecognizeFaceResult:
    status: str
    person_id: str | None = None
    matched_face_ids: list[str] = field(default_factory=list)


class FaceDetectionBackend(Protocol):
    def detect_faces(self, image_path: Path, *, min_score: float) -> tuple[int, int, list[DetectedFace]]:
        """一次返回图像尺寸、bbox 和 ArcFace embedding。"""


class FaceSearchIndex:
    """face_search(vector(512)) + HNSW 的轻量内存实现。"""

    def __init__(self, *, dim: int = 512, ef_construction: int = 300, m: int = 16) -> None:
        self._dim = int(dim)
        self._ef_construction = int(ef_construction)
        self._m = int(m)
        self._capacity = 16
        self._index = hnswlib.Index(space="cosine", dim=self._dim)
        self._index.init_index(max_elements=self._capacity, ef_construction=self._ef_construction, M=self._m)
        self._index.set_ef(max(50, self._m))
        self._next_label = 1
        self._face_id_to_label: dict[str, int] = {}
        self._label_to_face_id: dict[int, str] = {}
        self._vectors: dict[str, np.ndarray] = {}

    @property
    def count(self) -> int:
        return len(self._vectors)

    def upsert(self, face_id: str, embedding: np.ndarray) -> None:
        vector = _normalize_vector(embedding)
        if vector.shape != (self._dim,):
            raise ValueError(f"embedding 维度错误: 期望 {self._dim}，实际 {vector.shape}")
        if face_id in self._face_id_to_label:
            self.delete(face_id)
        if self.count + 1 > self._capacity:
            self._capacity *= 2
            self._index.resize_index(self._capacity)
        label = self._next_label
        self._next_label += 1
        self._index.add_items(vector.reshape(1, -1), ids=np.asarray([label], dtype=np.int64))
        self._face_id_to_label[face_id] = label
        self._label_to_face_id[label] = face_id
        self._vectors[face_id] = vector

    def delete(self, face_id: str) -> None:
        label = self._face_id_to_label.pop(face_id, None)
        self._vectors.pop(face_id, None)
        if label is None:
            return
        self._label_to_face_id.pop(label, None)
        self._index.mark_deleted(label)

    def search(
        self,
        embedding: np.ndarray,
        *,
        num_results: int,
        max_distance: float,
        predicate: Callable[[str], bool] | None = None,
    ) -> list[SearchMatch]:
        if self.count == 0:
            return []
        query = _normalize_vector(embedding)
        k = self.count if predicate is not None else min(max(int(num_results), 1), self.count)
        labels, distances = self._index.knn_query(query.reshape(1, -1), k=k)
        results: list[SearchMatch] = []
        for label, distance in zip(labels[0].tolist(), distances[0].tolist(), strict=False):
            face_id = self._label_to_face_id.get(int(label))
            if not face_id:
                continue
            if predicate is not None and not predicate(face_id):
                continue
            safe_distance = float(distance)
            if safe_distance <= max_distance:
                results.append(SearchMatch(face_id=face_id, distance=safe_distance))
            if len(results) >= num_results:
                break
        return results


class ImmichLikeFaceEngine:
    """模拟 Immich 的检测、增量重检和在线人物归属。"""

    def __init__(
        self,
        *,
        backend: FaceDetectionBackend,
        min_score: float = 0.7,
        max_distance: float = 0.5,
        min_faces: int = 3,
    ) -> None:
        self.backend = backend
        self.min_score = float(min_score)
        self.max_distance = float(max_distance)
        self.min_faces = int(min_faces)
        self.assets: dict[str, AssetRecord] = {}
        self.faces: dict[str, FaceRecord] = {}
        self.people: dict[str, PersonRecord] = {}
        self.face_search = FaceSearchIndex(dim=512)
        self.pending_recognition_face_ids: list[str] = []

    def add_asset(
        self,
        *,
        asset_id: str,
        image_path: Path,
        file_created_at: datetime | None = None,
    ) -> AssetRecord:
        asset = AssetRecord(
            id=str(asset_id),
            image_path=Path(image_path),
            file_created_at=file_created_at or _utcnow(),
        )
        self.assets[asset.id] = asset
        return asset

    def stream_assets_for_detect(self, *, force: bool = False) -> list[str]:
        rows = sorted(
            self.assets.values(),
            key=lambda item: item.file_created_at,
            reverse=True,
        )
        if force:
            return [item.id for item in rows]
        return [item.id for item in rows if item.faces_recognized_at is None]

    def detect_asset_faces(self, asset_id: str) -> DetectFacesResult:
        asset = self.assets[str(asset_id)]
        image_height, image_width, faces = self.backend.detect_faces(asset.image_path, min_score=self.min_score)
        existing_face_ids = list(asset.face_ids)
        unmatched_face_ids = set(existing_face_ids)
        new_face_ids: list[str] = []
        matched_face_ids: list[str] = []
        for face in faces:
            match_id = self._match_existing_face(
                asset=asset,
                image_width=image_width,
                image_height=image_height,
                new_box=face.bounding_box,
            )
            if match_id:
                unmatched_face_ids.discard(match_id)
                matched_face_ids.append(match_id)
                continue
            face_id = str(uuid.uuid4())
            face_record = FaceRecord(
                id=face_id,
                asset_id=asset.id,
                bounding_box=face.bounding_box,
                image_width=image_width,
                image_height=image_height,
                embedding=_normalize_vector(face.embedding),
                score=float(face.score),
            )
            self.faces[face_id] = face_record
            asset.face_ids.append(face_id)
            self.face_search.upsert(face_id, face_record.embedding)
            new_face_ids.append(face_id)
            self.pending_recognition_face_ids.append(face_id)
        removed_face_ids = sorted(unmatched_face_ids)
        for face_id in removed_face_ids:
            self._remove_face(face_id)
        asset.faces_recognized_at = _utcnow()
        return DetectFacesResult(
            new_face_ids=new_face_ids,
            removed_face_ids=removed_face_ids,
            matched_face_ids=matched_face_ids,
        )

    def recognize_face(self, face_id: str, *, deferred: bool = False) -> RecognizeFaceResult:
        face = self.faces[str(face_id)]
        if face.person_id:
            return RecognizeFaceResult(status="skipped", person_id=face.person_id, matched_face_ids=[face.id])
        matches = self.face_search.search(
            face.embedding,
            num_results=max(self.min_faces, 1),
            max_distance=self.max_distance,
        )
        matched_face_ids = [item.face_id for item in matches]
        if self.min_faces > 1 and len(matches) <= 1:
            return RecognizeFaceResult(status="skipped", matched_face_ids=matched_face_ids)
        is_core = len(matches) >= self.min_faces
        if not is_core and not deferred:
            return RecognizeFaceResult(status="deferred", matched_face_ids=matched_face_ids)
        person_id = next((self.faces[item.face_id].person_id for item in matches if self.faces[item.face_id].person_id), None)
        if not person_id:
            match_with_person = self.face_search.search(
                face.embedding,
                num_results=1,
                max_distance=self.max_distance,
                predicate=lambda candidate_face_id: self.faces[candidate_face_id].person_id is not None,
            )
            if match_with_person:
                person_id = self.faces[match_with_person[0].face_id].person_id
        if is_core and not person_id:
            person_id = self._create_person(face.id)
        if person_id:
            self._assign_face_to_person(face_id=face.id, person_id=person_id)
            return RecognizeFaceResult(status="assigned", person_id=person_id, matched_face_ids=matched_face_ids)
        return RecognizeFaceResult(status="skipped", matched_face_ids=matched_face_ids)

    def process_pending_recognition_queue(self, *, deferred: bool = False) -> list[RecognizeFaceResult]:
        queued_face_ids = list(dict.fromkeys(self.pending_recognition_face_ids))
        self.pending_recognition_face_ids = []
        results: list[RecognizeFaceResult] = []
        for face_id in queued_face_ids:
            if face_id not in self.faces:
                continue
            results.append(self.recognize_face(face_id, deferred=deferred))
        return results

    def _create_person(self, face_id: str) -> str:
        person_id = str(uuid.uuid4())
        self.people[person_id] = PersonRecord(id=person_id, face_ids=[face_id])
        return person_id

    def _assign_face_to_person(self, *, face_id: str, person_id: str) -> None:
        face = self.faces[face_id]
        if face.person_id == person_id:
            return
        if face.person_id and face.person_id in self.people:
            old_person = self.people[face.person_id]
            old_person.face_ids = [item for item in old_person.face_ids if item != face_id]
        face.person_id = person_id
        person = self.people.setdefault(person_id, PersonRecord(id=person_id))
        if face_id not in person.face_ids:
            person.face_ids.append(face_id)

    def _remove_face(self, face_id: str) -> None:
        face = self.faces.pop(face_id, None)
        self.face_search.delete(face_id)
        self.pending_recognition_face_ids = [item for item in self.pending_recognition_face_ids if item != face_id]
        if not face:
            return
        asset = self.assets.get(face.asset_id)
        if asset:
            asset.face_ids = [item for item in asset.face_ids if item != face_id]
        if face.person_id and face.person_id in self.people:
            person = self.people[face.person_id]
            person.face_ids = [item for item in person.face_ids if item != face_id]
            if not person.face_ids:
                self.people.pop(face.person_id, None)

    def _match_existing_face(
        self,
        *,
        asset: AssetRecord,
        image_width: int,
        image_height: int,
        new_box: BoundingBox,
    ) -> str | None:
        normalized_new_box = new_box.normalize(width=image_width, height=image_height)
        for face_id in asset.face_ids:
            existing = self.faces[face_id]
            normalized_existing_box = existing.bounding_box.normalize(
                width=existing.image_width,
                height=existing.image_height,
            )
            if normalized_existing_box.iou(normalized_new_box) > 0.5:
                return face_id
        return None


class InsightFaceImmichBackend:
    """使用 RetinaFace + ArcFaceONNX 复现 Immich 的一次请求双阶段推理。"""

    def __init__(
        self,
        *,
        model_root: Path = Path(".insightface"),
        model_name: str = "buffalo_l",
        min_score: float = 0.7,
        providers: list[str] | None = None,
    ) -> None:
        from insightface.model_zoo import model_zoo

        self.min_score = float(min_score)
        base_dir = Path(model_root) / "models" / model_name
        detector_path = base_dir / "det_10g.onnx"
        recognizer_path = base_dir / "w600k_r50.onnx"
        if not detector_path.exists() or not recognizer_path.exists():
            raise FileNotFoundError(
                f"缺少 buffalo_l 模型文件，期望存在 {detector_path} 和 {recognizer_path}"
            )
        self._detector = model_zoo.get_model(detector_path.as_posix(), providers=providers or ["CPUExecutionProvider"])
        self._detector.prepare(ctx_id=0, det_thresh=self.min_score, input_size=(640, 640))
        self._recognizer = model_zoo.get_model(recognizer_path.as_posix(), providers=providers or ["CPUExecutionProvider"])

    def detect_faces(self, image_path: Path, *, min_score: float) -> tuple[int, int, list[DetectedFace]]:
        from insightface.utils.face_align import norm_crop

        self._detector.prepare(ctx_id=0, det_thresh=float(min_score))
        image = load_rgb_image_with_exif(image_path)
        rgb = np.asarray(image, dtype=np.uint8)
        bgr = rgb[:, :, ::-1]
        bboxes, landmarks = self._detector.detect(bgr)
        if bboxes.shape[0] == 0:
            return bgr.shape[0], bgr.shape[1], []
        crops = [norm_crop(bgr, landmark) for landmark in landmarks]
        embeddings = self._recognizer.get_feat(crops)
        faces = [
            DetectedFace(
                bounding_box=BoundingBox(
                    x1=float(box[0]),
                    y1=float(box[1]),
                    x2=float(box[2]),
                    y2=float(box[3]),
                ),
                embedding=_normalize_vector(embedding),
                score=float(box[4]),
            )
            for box, embedding in zip(bboxes, embeddings, strict=False)
        ]
        return bgr.shape[0], bgr.shape[1], faces
