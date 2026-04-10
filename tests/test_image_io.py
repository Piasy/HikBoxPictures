from __future__ import annotations

from pathlib import Path

from PIL import Image

from hikbox_pictures.image_io import load_rgb_image


def test_load_rgb_image_applies_exif_orientation(tmp_path: Path) -> None:
    photo = tmp_path / "orientation.png"
    image = Image.new("RGB", (3, 2))
    image.putdata(
        [
            (255, 0, 0),
            (0, 255, 0),
            (0, 0, 255),
            (255, 255, 0),
            (255, 0, 255),
            (0, 255, 255),
        ]
    )
    exif = Image.Exif()
    exif[274] = 6
    image.save(photo, exif=exif)

    rgb = load_rgb_image(photo)

    assert rgb.shape == (3, 2, 3)
    assert rgb[0, 0].tolist() == [255, 255, 0]
    assert rgb[0, 1].tolist() == [255, 0, 0]
    assert rgb[-1, 0].tolist() == [0, 255, 255]
    assert rgb[-1, 1].tolist() == [0, 0, 255]
