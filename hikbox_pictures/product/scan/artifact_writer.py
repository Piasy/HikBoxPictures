"""detect 产物写入器。"""

from __future__ import annotations

import uuid
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageOps

try:
    from insightface.utils import face_align
except Exception:  # pragma: no cover - 运行时依赖异常
    face_align = None


class ArtifactWriter:
    """负责 crop/aligned/context 的原子落盘。"""

    def __init__(self, output_root: Path):
        self._output_root = Path(output_root)
        self._crop_dir = self._output_root / "artifacts" / "crops"
        self._aligned_dir = self._output_root / "artifacts" / "aligned"
        self._context_dir = self._output_root / "artifacts" / "context"
        self._crop_dir.mkdir(parents=True, exist_ok=True)
        self._aligned_dir.mkdir(parents=True, exist_ok=True)
        self._context_dir.mkdir(parents=True, exist_ok=True)

    def write_face_artifacts(
        self,
        *,
        photo_key: str,
        face_index: int,
        rgb_image: Image.Image,
        bgr_image: np.ndarray,
        bbox: tuple[int, int, int, int],
        kps: np.ndarray | None,
        preview_max_side: int,
    ) -> dict[str, str]:
        face_name = f"{photo_key}_{face_index:03d}"
        crop_relpath = f"artifacts/crops/{face_name}.jpg"
        aligned_relpath = f"artifacts/aligned/{face_name}.png"
        context_relpath = f"artifacts/context/{face_name}.jpg"

        crop_img = _make_crop(rgb_image, bbox=bbox)
        _save_pil_atomic(crop_img, self._output_root / crop_relpath, format="JPEG", quality=92)

        context_img = _make_context(rgb_image, bbox=bbox, max_side=preview_max_side)
        _save_pil_atomic(context_img, self._output_root / context_relpath, format="JPEG", quality=88)

        aligned = _make_aligned(bgr_image=bgr_image, bbox=bbox, kps=kps)
        _save_cv2_atomic(aligned, self._output_root / aligned_relpath)

        return {
            "crop_relpath": crop_relpath,
            "aligned_relpath": aligned_relpath,
            "context_relpath": context_relpath,
        }


def _save_pil_atomic(image: Image.Image, final_path: Path, **save_kwargs: object) -> None:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = final_path.with_name(f".{final_path.stem}.{uuid.uuid4().hex}{final_path.suffix}")
    image.save(tmp_path, **save_kwargs)
    tmp_path.replace(final_path)


def _save_cv2_atomic(image: np.ndarray, final_path: Path) -> None:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = final_path.with_name(f".{final_path.stem}.{uuid.uuid4().hex}{final_path.suffix}")
    ok = cv2.imwrite(str(tmp_path), image)
    if not ok:
        raise RuntimeError(f"无法写入图像: {tmp_path}")
    tmp_path.replace(final_path)


def _make_crop(image: Image.Image, *, bbox: tuple[int, int, int, int], pad_ratio: float = 0.25) -> Image.Image:
    x1, y1, x2, y2 = bbox
    width, height = image.size
    bw = x2 - x1
    bh = y2 - y1
    pad_w = int(bw * pad_ratio)
    pad_h = int(bh * pad_ratio)
    cx1 = max(0, x1 - pad_w)
    cy1 = max(0, y1 - pad_h)
    cx2 = min(width, x2 + pad_w)
    cy2 = min(height, y2 + pad_h)
    crop = image.crop((cx1, cy1, cx2, cy2))
    return ImageOps.fit(crop, (256, 256), Image.Resampling.LANCZOS)


def _make_context(image: Image.Image, *, bbox: tuple[int, int, int, int], max_side: int) -> Image.Image:
    width, height = image.size
    scale = min(1.0, float(max_side) / float(max(width, height)))
    if scale >= 1.0:
        canvas = image.copy()
    else:
        canvas = image.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.Resampling.LANCZOS)

    px1 = int(bbox[0] * scale)
    py1 = int(bbox[1] * scale)
    px2 = int(bbox[2] * scale)
    py2 = int(bbox[3] * scale)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((px1, py1, px2, py2), outline="#ff3b30", width=3)
    return canvas


def _make_aligned(
    *,
    bgr_image: np.ndarray,
    bbox: tuple[int, int, int, int],
    kps: np.ndarray | None,
) -> np.ndarray:
    if kps is not None and face_align is not None:
        return face_align.norm_crop(bgr_image, kps, image_size=112)

    x1, y1, x2, y2 = bbox
    crop = bgr_image[y1:y2, x1:x2]
    if crop.size == 0:
        crop = np.zeros((112, 112, 3), dtype=np.uint8)
    return cv2.resize(crop, (112, 112), interpolation=cv2.INTER_LANCZOS4)
