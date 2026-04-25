from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import ImageDraw
import numpy as np

from hikbox_pictures.product.scan_shared import clamp_bbox
from hikbox_pictures.product.scan_shared import load_rgb_image_with_exif
from hikbox_pictures.product.scan_shared import normalize_vector
from hikbox_pictures.product.scan_shared import resize_to_max_edge
from hikbox_pictures.product.scan_shared import utc_now_text


class WorkerError(RuntimeError):
    """批次 worker 执行失败。"""


class FatalWorkerError(WorkerError):
    """必须使整个 worker 失败的错误。"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hikbox_pictures.product.scan_worker")
    parser.add_argument("--input-json", required=True)
    parser.add_argument("--output-json", required=True)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(Path(args.input_json).read_text(encoding="utf-8"))
        result = run_worker(payload)
        Path(args.output_json).write_text(
            json.dumps(result, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"scan worker 失败: {exc}") from exc
    return 0


def run_worker(payload: dict[str, object]) -> dict[str, object]:
    batch_items = payload["items"]
    if not isinstance(batch_items, list) or not batch_items:
        raise WorkerError("worker 输入缺少批次 items。")
    model_root = Path(str(payload["model_root"]))
    staging_dir = Path(str(payload["staging_dir"]))
    backend = _InsightFaceWorkerBackend(model_root=model_root)
    results: list[dict[str, object]] = []
    for item in batch_items:
        results.append(
            _process_item(
                backend=backend,
                item=item,
                staging_dir=staging_dir,
            )
        )
    return {
        "model_root": str(model_root),
        "processed_at": utc_now_text(),
        "items": results,
    }


class _InsightFaceWorkerBackend:
    def __init__(self, *, model_root: Path) -> None:
        from insightface.app import FaceAnalysis

        self._model_root = model_root
        self._app = FaceAnalysis(
            name="buffalo_l",
            root=str(model_root),
            providers=["CPUExecutionProvider"],
        )
        self._app.prepare(ctx_id=0, det_thresh=0.7, det_size=(640, 640))

    def detect(self, image_path: Path) -> tuple[int, int, list[dict[str, object]]]:
        image = load_rgb_image_with_exif(image_path)
        try:
            rgb = np.asarray(image, dtype=np.uint8)
        finally:
            image.close()
        bgr = rgb[:, :, ::-1]
        faces = self._app.get(bgr)
        detections: list[dict[str, object]] = []
        for face in faces:
            embedding = normalize_vector(np.asarray(face.normed_embedding, dtype=np.float32))
            if embedding.shape != (512,):
                raise FatalWorkerError(f"embedding 维度错误：{image_path} -> {embedding.shape}")
            detections.append(
                {
                    "bbox": [float(value) for value in face.bbox.tolist()],
                    "score": float(face.det_score),
                    "embedding": [float(value) for value in embedding.tolist()],
                }
            )
        return int(bgr.shape[1]), int(bgr.shape[0]), detections


def _process_item(
    *,
    backend: _InsightFaceWorkerBackend,
    item: object,
    staging_dir: Path,
) -> dict[str, object]:
    if not isinstance(item, dict):
        raise FatalWorkerError("worker item 格式错误。")
    image_path = Path(str(item["absolute_path"]))
    file_fingerprint = str(item["file_fingerprint"])
    artifact_stem = _artifact_stem_for_item(item=item, file_fingerprint=file_fingerprint)
    try:
        image_width, image_height, detections = backend.detect(image_path)
        artifacts = _generate_artifacts(
            image_path=image_path,
            artifact_stem=artifact_stem,
            detections=detections,
            staging_dir=staging_dir,
        )
        return {
            "absolute_path": str(image_path),
            "status": "succeeded",
            "image_width": image_width,
            "image_height": image_height,
            "face_count": len(detections),
            "detections": detections,
            "artifacts": artifacts,
        }
    except FatalWorkerError:
        raise
    except Exception as exc:  # noqa: BLE001
        return {
            "absolute_path": str(image_path),
            "status": "failed",
            "failure_reason": str(exc),
            "face_count": 0,
            "detections": [],
            "artifacts": [],
        }


def _generate_artifacts(
    *,
    image_path: Path,
    artifact_stem: str,
    detections: list[dict[str, object]],
    staging_dir: Path,
) -> list[dict[str, object]]:
    crop_dir = staging_dir / "crops"
    context_dir = staging_dir / "context"
    crop_dir.mkdir(parents=True, exist_ok=True)
    context_dir.mkdir(parents=True, exist_ok=True)
    image = load_rgb_image_with_exif(image_path)
    try:
        width, height = image.size
        base_context_image, scale = resize_to_max_edge(image, max_edge=480)
        try:
            artifacts: list[dict[str, object]] = []
            for face_index, detection in enumerate(detections):
                bbox = detection["bbox"]
                left, top, right, bottom = clamp_bbox(
                    x1=float(bbox[0]),
                    y1=float(bbox[1]),
                    x2=float(bbox[2]),
                    y2=float(bbox[3]),
                    width=width,
                    height=height,
                )
                crop_path = (crop_dir / f"{artifact_stem}_face_{face_index:02d}.jpg").resolve()
                context_path = (context_dir / f"{artifact_stem}_face_{face_index:02d}.jpg").resolve()
                crop_image = image.crop((left, top, right, bottom))
                try:
                    crop_image.save(crop_path, format="JPEG", quality=90)
                finally:
                    crop_image.close()

                context_image = base_context_image.copy()
                try:
                    draw = ImageDraw.Draw(context_image)
                    outline_width = max(2, min(8, int(max(context_image.size) * 0.008)))
                    draw.rectangle(
                        (
                            int(round(left * scale)),
                            int(round(top * scale)),
                            int(round(right * scale)),
                            int(round(bottom * scale)),
                        ),
                        outline=(225, 48, 48),
                        width=outline_width,
                    )
                    context_image.save(context_path, format="JPEG", quality=90)
                finally:
                    context_image.close()
                artifacts.append(
                    {
                        "crop_path": str(crop_path),
                        "context_path": str(context_path),
                    }
                )
            return artifacts
        finally:
            base_context_image.close()
    finally:
        image.close()


def _artifact_stem_for_item(*, item: dict[str, object], file_fingerprint: str) -> str:
    scan_batch_item_id = item.get("scan_batch_item_id")
    item_index = item.get("item_index")
    if isinstance(scan_batch_item_id, int):
        unique_token = f"item{scan_batch_item_id:06d}"
    elif isinstance(item_index, int):
        unique_token = f"index{item_index:04d}"
    else:
        unique_token = "item000000"
    return f"{unique_token}_{file_fingerprint}"


if __name__ == "__main__":
    raise SystemExit(main())
