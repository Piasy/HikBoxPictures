"""detect 子进程协议与执行。"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

from hikbox_pictures.product.scan.artifact_writer import ArtifactWriter


def run_detect_worker(
    request: dict[str, object],
    *,
    detector: Callable[[np.ndarray], list[object]] | None = None,
) -> dict[str, object]:
    """执行 worker 批次并返回可被 ack 消费的 payload。"""
    items = request.get("items")
    if not isinstance(items, list):
        raise ValueError("worker request.items 必须是列表")

    output_root = Path(str(request.get("output_root") or "."))
    writer = ArtifactWriter(output_root)
    face_detector = detector or _build_default_detector(
        insightface_root=Path(str(request.get("insightface_root") or ".insightface")),
        detector_model_name=str(request.get("detector_model_name") or "buffalo_l"),
        det_size=int(request.get("det_size") or 640),
    )
    preview_max_side = int(request.get("preview_max_side") or 1280)

    results: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("worker item 格式非法")
        photo_asset_id = int(item["photo_asset_id"])
        image_path = Path(str(item["image_path"]))
        photo_key = str(item.get("photo_key") or f"asset-{photo_asset_id}")

        try:
            rgb_image = _load_rgb_image(image_path)
            rgb_arr = np.asarray(rgb_image)
            bgr_arr = cv2.cvtColor(rgb_arr, cv2.COLOR_RGB2BGR)
            detected_faces = _detect_faces(face_detector, bgr_arr)
            height, width = bgr_arr.shape[:2]

            faces_payload: list[dict[str, object]] = []
            for face_index, face in enumerate(detected_faces):
                kps = _extract_kps(face)
                if kps is None:
                    continue

                bbox = _safe_bbox(_extract_bbox(face), width=width, height=height)
                area_ratio = float((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) / max(1, width * height))
                det_conf = float(_extract_det_score(face))
                artifacts = writer.write_face_artifacts(
                    photo_key=photo_key,
                    face_index=face_index,
                    rgb_image=rgb_image,
                    bgr_image=bgr_arr,
                    bbox=bbox,
                    kps=kps,
                    preview_max_side=preview_max_side,
                )
                magface_quality = float(1.0 + area_ratio * 8.0 + det_conf)
                quality_score = float(magface_quality * max(0.05, det_conf) * np.sqrt(max(area_ratio, 1e-9)))
                faces_payload.append(
                    {
                        "bbox": [float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])],
                        "detector_confidence": det_conf,
                        "face_area_ratio": area_ratio,
                        "crop_relpath": artifacts["crop_relpath"],
                        "aligned_relpath": artifacts["aligned_relpath"],
                        "context_relpath": artifacts["context_relpath"],
                        "magface_quality": magface_quality,
                        "quality_score": quality_score,
                    }
                )

            done_status = "done"
            results.append(
                {
                    "photo_asset_id": photo_asset_id,
                    "status": done_status,
                    "faces": faces_payload,
                }
            )
        except Exception as exc:
            results.append(
                {
                    "photo_asset_id": photo_asset_id,
                    "status": "failed",
                    "error": str(exc),
                    "faces": [],
                }
            )

    return {"results": results}


def _build_default_detector(*, insightface_root: Path, detector_model_name: str, det_size: int) -> object:
    from insightface.app import FaceAnalysis

    detector = FaceAnalysis(name=detector_model_name, root=str(insightface_root), allowed_modules=["detection"])
    detector.prepare(ctx_id=-1, det_size=(det_size, det_size))
    return detector


def _detect_faces(detector: object, bgr: np.ndarray) -> list[object]:
    if hasattr(detector, "get"):
        return list(detector.get(bgr))
    if callable(detector):
        return list(detector(bgr))
    raise TypeError("detector 必须是可调用对象或包含 get 方法")


def _load_rgb_image(path: Path) -> Image.Image:
    with Image.open(path) as image:
        normalized = ImageOps.exif_transpose(image)
        return normalized.convert("RGB")


def _safe_bbox(bbox: np.ndarray, *, width: int, height: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = [int(v) for v in bbox.tolist()]
    x1 = max(0, min(x1, width - 1))
    y1 = max(0, min(y1, height - 1))
    x2 = max(x1 + 1, min(x2, width))
    y2 = max(y1 + 1, min(y2, height))
    return x1, y1, x2, y2


def _extract_bbox(face: object) -> np.ndarray:
    if isinstance(face, dict):
        return np.asarray(face.get("bbox"), dtype=np.float32)
    return np.asarray(getattr(face, "bbox"), dtype=np.float32)


def _extract_kps(face: object) -> np.ndarray | None:
    if isinstance(face, dict):
        raw = face.get("kps")
        if raw is None:
            return None
        return np.asarray(raw, dtype=np.float32)
    raw = getattr(face, "kps", None)
    if raw is None:
        return None
    return np.asarray(raw, dtype=np.float32)


def _extract_det_score(face: object) -> float:
    if isinstance(face, dict):
        return float(face.get("det_score", 0.0))
    return float(getattr(face, "det_score", 0.0))


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="detect worker 子进程入口")
    parser.add_argument("--request-json", required=True, help="输入 request JSON 文件路径")
    parser.add_argument("--response-json", required=True, help="输出 response JSON 文件路径")
    return parser


def _main() -> int:
    args = _build_cli_parser().parse_args()
    request_path = Path(args.request_json)
    response_path = Path(args.response_json)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    payload = run_detect_worker(request)
    response_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = response_path.with_name(f".{response_path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(response_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
