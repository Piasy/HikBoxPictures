from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

from hikbox_pictures.image_io import load_rgb_image

try:
    from insightface.app import FaceAnalysis as _InsightFaceAnalysis
except ImportError:  # pragma: no cover
    _InsightFaceAnalysis = None

FaceAnalysis = _InsightFaceAnalysis

BBoxTLBR: TypeAlias = tuple[int, int, int, int]


class InsightFaceInitError(RuntimeError):
    pass


class InsightFaceInferenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class DetectedFace:
    # (top, right, bottom, left)
    bbox: BBoxTLBR
    embedding: Any


@dataclass
class InsightFaceEngine:
    analyzer: Any

    @classmethod
    def create(
        cls,
        *,
        model_name: str = "antelopev2",
        providers: list[str] | None = None,
    ) -> InsightFaceEngine:
        selected_providers = ["CPUExecutionProvider"] if providers is None else providers
        try:
            if FaceAnalysis is None:
                raise RuntimeError("insightface 未安装或不可用")
            analyzer = FaceAnalysis(name=model_name, providers=selected_providers)
            analyzer.prepare(ctx_id=0, det_size=(640, 640))
        except Exception as exc:
            raise InsightFaceInitError(f"InsightFace 初始化失败: {exc}") from exc
        return cls(analyzer=analyzer)

    def detect_faces(self, image_path: Path) -> list[DetectedFace]:
        try:
            rgb_image = load_rgb_image(image_path)
            bgr_image = rgb_image[:, :, ::-1]
            faces = self.analyzer.get(bgr_image)
            return [
                DetectedFace(
                    bbox=(int(face.bbox[1]), int(face.bbox[2]), int(face.bbox[3]), int(face.bbox[0])),
                    embedding=face.embedding,
                )
                for face in faces
            ]
        except Exception as exc:
            raise InsightFaceInferenceError(f"InsightFace 推理失败: {exc}") from exc
