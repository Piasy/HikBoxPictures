from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


def write_number_jpeg(path: Path, *, text: str, size: tuple[int, int] = (512, 512)) -> Path:
    """按给定文本写入一张简易 JPEG 数字图。"""
    width, height = size
    image = Image.new("RGB", (int(width), int(height)), color=(246, 247, 250))
    draw = ImageDraw.Draw(image)

    pad = 24
    draw.rectangle((pad, pad, width - pad, height - pad), outline=(42, 48, 61), width=6)
    draw.text((width // 2 - 36, height // 2 - 22), str(text), fill=(33, 37, 48))

    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="JPEG", quality=92)
    return path
