from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
from typing import Any, TypeAlias

from hikbox_pictures.image_io import load_rgb_image

try:
    from insightface.app import FaceAnalysis as _InsightFaceAnalysis
except ImportError:  # pragma: no cover
    _InsightFaceAnalysis = None

FaceAnalysis = _InsightFaceAnalysis

BBoxTLBR: TypeAlias = tuple[int, int, int, int]
DEFAULT_INSIGHTFACE_ROOT = Path("~/.insightface").expanduser()
DEFAULT_DET_SIZE = (512, 512)


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
    default_det_size: tuple[int, int] = DEFAULT_DET_SIZE
    prepared_det_size: tuple[int, int] = DEFAULT_DET_SIZE

    @classmethod
    def create(
        cls,
        *,
        model_name: str = "antelopev2",
        providers: list[str] | None = None,
        root: Path = DEFAULT_INSIGHTFACE_ROOT,
        det_size: tuple[int, int] = DEFAULT_DET_SIZE,
    ) -> InsightFaceEngine:
        selected_providers = ["CPUExecutionProvider"] if providers is None else providers
        try:
            if FaceAnalysis is None:
                raise RuntimeError("insightface 未安装或不可用")
            cls._repair_model_cache_layout(model_name=model_name, root=root)
            analyzer = FaceAnalysis(name=model_name, root=str(root), providers=selected_providers)
            analyzer.prepare(ctx_id=0, det_size=det_size)
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            raise InsightFaceInitError(f"InsightFace 初始化失败: {detail}") from exc
        return cls(analyzer=analyzer, default_det_size=det_size, prepared_det_size=det_size)

    @staticmethod
    def _repair_model_cache_layout(*, model_name: str, root: Path) -> None:
        model_dir = root / "models" / model_name
        if not model_dir.is_dir():
            return
        if any(model_dir.glob("*.onnx")):
            return

        nested_dirs = [candidate for candidate in model_dir.iterdir() if candidate.is_dir()]
        if len(nested_dirs) != 1:
            return

        nested_dir = nested_dirs[0]
        nested_models = list(nested_dir.glob("*.onnx"))
        if not nested_models:
            return

        for nested_path in nested_dir.iterdir():
            target_path = model_dir / nested_path.name
            if target_path.exists():
                continue
            shutil.move(str(nested_path), str(target_path))

        try:
            nested_dir.rmdir()
        except OSError:
            pass

    def detect_faces(
        self,
        image_path: Path,
        *,
        det_size: tuple[int, int] | None = None,
    ) -> list[DetectedFace]:
        try:
            target_det_size = det_size or self.default_det_size
            if target_det_size != self.prepared_det_size:
                self.analyzer.prepare(ctx_id=0, det_size=target_det_size)
                self.prepared_det_size = target_det_size
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
