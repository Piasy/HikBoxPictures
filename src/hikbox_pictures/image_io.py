from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image
from pillow_heif import register_heif_opener

register_heif_opener()


def load_rgb_image(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.array(image.convert("RGB"))
