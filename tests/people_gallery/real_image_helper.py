from __future__ import annotations

from pathlib import Path


_DATASET_ROOT = Path(__file__).resolve().parents[1] / "data" / "e2e-face-input"
_RAW_ROOT = _DATASET_ROOT / "raw"
_GROUP_ROOT = _DATASET_ROOT / "groups"


def copy_raw_face_image(target: Path, *, index: int = 0) -> Path:
    samples = sorted(_RAW_ROOT.glob("*.jpg"))
    if not samples:
        raise RuntimeError(f"缺少原始人脸测试图片: {_RAW_ROOT}")
    source = samples[int(index) % len(samples)]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    return target


def copy_group_face_image(target: Path, *, index: int = 0) -> Path:
    samples = sorted(_GROUP_ROOT.glob("*.jpg"))
    if not samples:
        raise RuntimeError(f"缺少多人脸测试图片: {_GROUP_ROOT}")
    source = samples[int(index) % len(samples)]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    return target


def bind_real_source_roots(ws, root: Path) -> None:
    for index, source in enumerate(ws.source_repo.list_sources(active=True), start=1):
        source_root = root / f"source-{index}"
        source_root.mkdir(parents=True, exist_ok=True)
        copy_raw_face_image(source_root / f"{index}.jpg", index=index - 1)
        ws.conn.execute(
            """
            UPDATE library_source
            SET root_path = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (str(source_root.resolve()), int(source["id"])),
        )
    ws.conn.commit()
