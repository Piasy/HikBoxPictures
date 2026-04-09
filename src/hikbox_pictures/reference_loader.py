from __future__ import annotations

from pathlib import Path
from typing import Sequence

import face_recognition

from hikbox_pictures.image_io import load_rgb_image


class ReferenceImageError(ValueError):
    pass


def load_reference_encoding(image_path: Path) -> Sequence[float]:
    encodings = face_recognition.face_encodings(load_rgb_image(image_path))
    if len(encodings) != 1:
        raise ReferenceImageError(
            f"Reference image {image_path} must contain exactly one face; found {len(encodings)}."
        )
    return encodings[0]
