from pathlib import Path

import numpy as np
from PIL import Image
import pillow_heif

from hikbox_pictures.product.scan.detect_worker import run_detect_worker


def test_worker_payload_must_include_real_face_details(tmp_path: Path) -> None:
    image_path = tmp_path / "input.jpg"
    Image.new("RGB", (160, 120), color=(220, 220, 220)).save(image_path)

    payload = run_detect_worker(
        {
            "items": [
                {
                    "photo_asset_id": 7,
                    "image_path": str(image_path),
                    "photo_key": "asset-7",
                }
            ],
            "output_root": str(tmp_path / "worker-out"),
        },
        detector=lambda _img: [
            {
                "bbox": np.array([15.0, 20.0, 90.0, 100.0], dtype=np.float32),
                "kps": np.array([[20, 30], [40, 30], [30, 45], [24, 60], [38, 60]], dtype=np.float32),
                "det_score": 0.93,
            }
        ],
    )

    assert "results" in payload
    assert payload["results"][0]["status"] == "done"
    assert "faces" in payload["results"][0]
    assert payload["results"][0]["faces"]
    required = {
        "bbox",
        "detector_confidence",
        "face_area_ratio",
        "crop_relpath",
        "aligned_relpath",
        "context_relpath",
    }
    assert required <= payload["results"][0]["faces"][0].keys()


def test_worker_can_decode_heic_file(tmp_path: Path) -> None:
    image_path = tmp_path / "input.heic"
    heif_file = pillow_heif.from_pillow(Image.new("RGB", (96, 72), color=(120, 130, 140)))
    heif_file.save(image_path)

    payload = run_detect_worker(
        {
            "items": [
                {
                    "photo_asset_id": 8,
                    "image_path": str(image_path),
                    "photo_key": "asset-8",
                }
            ],
            "output_root": str(tmp_path / "worker-out"),
        },
        detector=lambda _img: [],
    )

    assert "results" in payload
    assert payload["results"][0]["status"] == "done"
    assert payload["results"][0]["faces"] == []
