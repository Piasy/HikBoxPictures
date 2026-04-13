from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Mapping, Protocol, Sequence, TypeAlias

import numpy as np
import numpy.typing as npt

from hikbox_pictures.image_io import load_rgb_image

try:
    from deepface import DeepFace as _DeepFace
    from deepface.modules import verification as _verification
except ImportError:  # pragma: no cover
    _DeepFace = None
    _verification = None

DeepFace = _DeepFace
verification = _verification

BBoxTLBR: TypeAlias = tuple[int, int, int, int]
EmbeddingArray: TypeAlias = npt.NDArray[np.float32]
EmbeddingLike: TypeAlias = Sequence[float] | npt.NDArray[np.float32]
ThresholdSource: TypeAlias = Literal["deepface-default", "explicit"]


class DeepFaceModuleProtocol(Protocol):
    def represent(
        self,
        *,
        img_path: str,
        model_name: str,
        detector_backend: str,
        align: bool,
        enforce_detection: bool,
    ) -> object: ...


class VerificationModuleProtocol(Protocol):
    def find_threshold(self, model_name: str, distance_metric: str) -> float: ...

    def find_distance(self, lhs: EmbeddingLike, rhs: EmbeddingLike, distance_metric: str) -> float: ...


class DeepFaceInitError(RuntimeError):
    pass


class DeepFaceInferenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class DetectedFace:
    # (上, 右, 下, 左)
    bbox: BBoxTLBR
    embedding: EmbeddingArray


@dataclass
class DeepFaceEngine:
    model_name: str
    detector_backend: str
    distance_metric: str
    align: bool
    distance_threshold: float
    threshold_source: ThresholdSource
    deepface_module: DeepFaceModuleProtocol
    verification_module: VerificationModuleProtocol

    @classmethod
    def create(
        cls,
        *,
        model_name: str = "ArcFace",
        detector_backend: str = "retinaface",
        distance_metric: str = "cosine",
        align: bool = True,
        distance_threshold: float | None = None,
    ) -> DeepFaceEngine:
        try:
            deepface_module = DeepFace
            verification_module = verification
            if deepface_module is None or verification_module is None:
                raise RuntimeError("deepface 未安装或不可用")
            if distance_threshold is None:
                resolved_threshold = float(verification_module.find_threshold(model_name, distance_metric))
                threshold_source: ThresholdSource = "deepface-default"
            else:
                resolved_threshold = float(distance_threshold)
                threshold_source = "explicit"
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            raise DeepFaceInitError(detail) from exc

        return cls(
            model_name=model_name,
            detector_backend=detector_backend,
            distance_metric=distance_metric,
            align=align,
            distance_threshold=resolved_threshold,
            threshold_source=threshold_source,
            deepface_module=deepface_module,
            verification_module=verification_module,
        )

    def detect_faces(self, image_path: Path) -> list[DetectedFace]:
        try:
            loaded_rgb = load_rgb_image(image_path)
            loaded_bgr = loaded_rgb[:, :, ::-1]
            results = self.deepface_module.represent(
                img_path=loaded_bgr,
                model_name=self.model_name,
                detector_backend=self.detector_backend,
                align=self.align,
                enforce_detection=False,
            )
            if isinstance(results, Mapping):
                payloads: list[object] = [results]
            else:
                payloads = list(results)

            faces: list[DetectedFace] = []
            for payload in payloads:
                if not isinstance(payload, Mapping):
                    raise ValueError("结果项类型非法")

                facial_area = payload.get("facial_area")
                if not isinstance(facial_area, Mapping):
                    raise ValueError("facial_area 字段缺失或类型非法")

                x = self._required_int(facial_area, "x")
                y = self._required_int(facial_area, "y")
                w = self._required_int(facial_area, "w")
                h = self._required_int(facial_area, "h")
                bbox = (y, x + w, y + h, x)

                if "embedding" not in payload:
                    raise ValueError("embedding 字段缺失")
                embedding = np.asarray(payload["embedding"], dtype=np.float32)
                if embedding.ndim != 1 or embedding.size == 0:
                    raise ValueError("embedding 为空或维度非法")

                faces.append(DetectedFace(bbox=bbox, embedding=embedding))
            return faces
        except Exception as exc:
            raise DeepFaceInferenceError(f"DeepFace 推理失败: {exc}") from exc

    @staticmethod
    def _required_int(mapping: Mapping[str, object], key: str) -> int:
        if key not in mapping:
            raise ValueError(f"facial_area 缺少 {key}")
        try:
            return int(mapping[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"facial_area.{key} 非法") from exc

    def distance(self, lhs: EmbeddingLike, rhs: EmbeddingLike) -> float:
        return float(self.verification_module.find_distance(lhs, rhs, self.distance_metric))

    def min_distance(self, embedding: EmbeddingLike, references: Sequence[EmbeddingLike] | np.ndarray) -> float:
        reference_iterable = self._iter_references(references)
        return min((self.distance(embedding, reference) for reference in reference_iterable), default=float("inf"))

    @staticmethod
    def _iter_references(references: Sequence[EmbeddingLike] | np.ndarray) -> Iterable[EmbeddingLike]:
        if isinstance(references, np.ndarray):
            if references.size == 0:
                return ()
            if references.ndim == 1:
                return (references,)
            if references.ndim == 2:
                return tuple(references)
            raise ValueError("references 维度非法")
        return references

    def is_match(self, distance: float) -> bool:
        return distance <= self.distance_threshold


def embedding_to_blob(embedding: EmbeddingLike) -> bytes:
    vector = np.asarray(embedding, dtype=np.float32).reshape(-1)
    return vector.tobytes()
