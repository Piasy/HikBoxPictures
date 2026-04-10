from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

register_heif_opener()


def load_rgb_image(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        normalized = ImageOps.exif_transpose(image)
        return np.array(normalized.convert("RGB"))
