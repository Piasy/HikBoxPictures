from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import uuid

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "live-photo"
SWIFT_HELPER_PATH = REPO_ROOT / "hikbox_pictures" / "product" / "export_live_photo.swift"
LIVE_PHOTO_FIXTURE_CONTENT_IDENTIFIER = "11111111-2222-4333-8444-555555555555"


def _xattr_read(path: Path, name: str) -> bytes:
    result = subprocess.run(
        ["xattr", "-px", name, str(path)],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return bytes.fromhex(result.stdout)


def _content_identifier(path: Path) -> str:
    result = subprocess.run(
        ["exiftool", "-s3", "-ContentIdentifier", str(path)],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def test_live_photo_export_fixture_uses_rfc4122_content_identifier() -> None:
    still_src = FIXTURE_DIR / "input2.HEIC"
    mov_src = FIXTURE_DIR / ".input2.MOV"

    still_content_identifier = _content_identifier(still_src)
    mov_content_identifier = _content_identifier(mov_src)
    parsed = uuid.UUID(still_content_identifier)

    assert still_content_identifier == LIVE_PHOTO_FIXTURE_CONTENT_IDENTIFIER
    assert mov_content_identifier == LIVE_PHOTO_FIXTURE_CONTENT_IDENTIFIER
    assert parsed.variant == uuid.RFC_4122


def test_swift_helper_exports_live_photo_xattrs(tmp_path: Path) -> None:
    """Swift helper 应复制文件和 xattr，并修正 Live Photo 关键 xattr。"""
    if sys.platform != "darwin":
        pytest.skip("Live Photo xattr 验证只在 macOS 上运行")
    if shutil.which("swift") is None:
        pytest.skip("缺少 swift")

    still_src = FIXTURE_DIR / "input2.HEIC"
    mov_src = FIXTURE_DIR / ".input2.MOV"
    still_dst = tmp_path / "exported" / "IMG_0001__iPhone.HEIC"
    mov_dst = tmp_path / "exported" / ".IMG_0001__iPhone.MOV"

    subprocess.run(
        [
            "swift",
            "-suppress-warnings",
            str(SWIFT_HELPER_PATH),
            str(still_src),
            str(mov_src),
            str(still_dst),
            str(mov_dst),
        ],
        check=True,
    )

    assert still_dst.read_bytes() == still_src.read_bytes()
    assert mov_dst.read_bytes() == mov_src.read_bytes()
    assert _content_identifier(still_dst) == LIVE_PHOTO_FIXTURE_CONTENT_IDENTIFIER
    assert _content_identifier(mov_dst) == LIVE_PHOTO_FIXTURE_CONTENT_IDENTIFIER
    assert _xattr_read(still_dst, "address") == _xattr_read(still_src, "address")
    assert _xattr_read(still_dst, "livephoto") == b".IMG_0001__iPhone.MOV\0"
    assert _xattr_read(still_dst, "fileattr") == b"\x10\x00\x00\x00"
    assert _xattr_read(mov_dst, "fileattr") == b"\x01\x00\x00\x00"
    expected_heicsize = struct.pack("<Q", still_dst.stat().st_size)
    assert _xattr_read(still_dst, "heicsize") == expected_heicsize
    assert _xattr_read(mov_dst, "heicsize") == expected_heicsize
    assert _xattr_read(still_dst, "md5") == hashlib.md5(still_dst.read_bytes()).hexdigest().encode()
