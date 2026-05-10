from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import struct
import subprocess
import tomllib
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "live-photo"
SCRIPT_PATH = REPO_ROOT / "scripts" / "make_live_photo_pair.py"
INSTALL_SCRIPT_PATH = REPO_ROOT / "scripts" / "install.sh"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("make_live_photo_pair", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


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


def _jpeg_scan_payload(path: Path) -> bytes:
    data = path.read_bytes()
    assert data.startswith(b"\xff\xd8")
    index = 2
    while index < len(data):
        assert data[index] == 0xFF
        while index < len(data) and data[index] == 0xFF:
            index += 1
        marker = data[index]
        index += 1
        if marker == 0xDA:
            length = int.from_bytes(data[index:index + 2], "big")
            return data[index + length:]
        if marker == 0xD9:
            return b""
        if marker in {0x01, *range(0xD0, 0xD8)}:
            continue
        length = int.from_bytes(data[index:index + 2], "big")
        index += length
    raise AssertionError(f"找不到 JPEG scan data：{path}")


def test_default_output_paths_use_jpg_and_hidden_mov(tmp_path: Path) -> None:
    module = _load_script_module()
    jpg_path = tmp_path / "IMG_20260424_210239.jpg"
    mp4_path = tmp_path / "IMG_20260424_210239.mp4"

    still_path, mov_path = module.default_output_paths(jpg_path, mp4_path, None, None)

    assert still_path == tmp_path / "IMG_20260424_210239_live.jpg"
    assert mov_path == tmp_path / ".IMG_20260424_210239_live.MOV"


def test_output_stem_controls_jpg_and_hidden_mov_names(tmp_path: Path) -> None:
    module = _load_script_module()
    jpg_path = tmp_path / "IMG_20260424_210239.jpg"
    mp4_path = tmp_path / "IMG_20260424_210239.mp4"
    output_dir = tmp_path / "out"

    still_path, mov_path = module.default_output_paths(
        jpg_path,
        mp4_path,
        output_dir,
        "output2",
    )

    assert still_path == output_dir / "output2.jpg"
    assert mov_path == output_dir / ".output2.MOV"


def test_asset_id_is_normalized_to_rfc4122_uppercase_uuid() -> None:
    module = _load_script_module()

    assert (
        module.normalize_asset_id("b5a2479c-c2da-4c20-8d7c-67a3756259bd")
        == "B5A2479C-C2DA-4C20-8D7C-67A3756259BD"
    )


def test_asset_id_rejects_non_rfc4122_uuid_variant() -> None:
    module = _load_script_module()

    try:
        module.normalize_asset_id("33333333-4444-5555-6666-777777777777")
    except ValueError as exc:
        assert "RFC 4122" in str(exc)
    else:
        raise AssertionError("无效 UUID variant 应该被拒绝")


def test_python_no_longer_owns_xattr_generation() -> None:
    module = _load_script_module()

    assert not hasattr(module, "build_live_photo_xattrs")
    assert not hasattr(module, "write_xattrs")


def test_run_swift_helper_invokes_checked_in_swift_file(monkeypatch, tmp_path: Path) -> None:
    module = _load_script_module()
    still_path = tmp_path / "output.jpg"
    mp4_path = tmp_path / "input.mp4"
    mov_path = tmp_path / ".output.MOV"
    calls: list[list[str]] = []

    monkeypatch.setattr(module, "ensure_tool", lambda name: None)

    def fake_run(args, **kwargs):
        calls.append([str(arg) for arg in args])

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    module.run_swift_helper(
        still_path=still_path,
        mp4_path=mp4_path,
        mov_path=mov_path,
        asset_id="B5A2479C-C2DA-4C20-8D7C-67A3756259BD",
    )

    assert calls == [
        [
            "swift",
            "-suppress-warnings",
            str(module.SWIFT_HELPER_PATH),
            str(still_path),
            str(mp4_path),
            str(mov_path),
            "B5A2479C-C2DA-4C20-8D7C-67A3756259BD",
        ]
    ]


def test_pyproject_declares_pyexiftool_dependency() -> None:
    project = tomllib.loads(PYPROJECT_PATH.read_text())["project"]

    assert any(
        dependency.lower().startswith("pyexiftool")
        for dependency in project["dependencies"]
    )


def test_install_script_installs_exiftool_binary() -> None:
    install_script = INSTALL_SCRIPT_PATH.read_text()

    assert "ensure_exiftool" in install_script
    assert "command -v exiftool" in install_script
    assert "brew install exiftool" in install_script


def test_install_script_checks_swift_binary() -> None:
    install_script = INSTALL_SCRIPT_PATH.read_text()

    assert "ensure_swift" in install_script
    assert "command -v swift" in install_script
    assert "Xcode" in install_script or "Swift" in install_script


def test_install_script_does_not_escalate_privileges() -> None:
    install_script = INSTALL_SCRIPT_PATH.read_text()

    assert "sudo" not in install_script
    assert "run_as_root" not in install_script
    assert "apt-get" not in install_script
    assert "dnf install" not in install_script
    assert "yum install" not in install_script


def test_write_live_photo_still_metadata_uses_exiftool(monkeypatch, tmp_path: Path) -> None:
    module = _load_script_module()
    still_path = tmp_path / "output.jpg"
    still_path.write_bytes((FIXTURE_DIR / "input.jpg").read_bytes())
    calls: list[tuple[str, ...]] = []
    makernote_payloads: list[bytes] = []

    class FakeExifToolHelper:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, *args):
            calls.append(tuple(str(arg) for arg in args))
            for arg in args:
                text = str(arg)
                if text.startswith("-MakerNoteApple<="):
                    makernote_payloads.append(
                        Path(text.removeprefix("-MakerNoteApple<=")).read_bytes()
                    )
            return ""

    monkeypatch.setattr(
        module,
        "exiftool",
        SimpleNamespace(ExifToolHelper=FakeExifToolHelper),
    )

    module.write_live_photo_still_metadata(
        still_path,
        "B5A2479C-C2DA-4C20-8D7C-67A3756259BD",
    )

    assert len(calls) == 1
    assert calls[0][0:2] == ("-overwrite_original", "-P")
    assert calls[0][2].startswith("-MakerNoteApple<=")
    assert makernote_payloads[0].startswith(b"Apple iOS\0")
    assert b"B5A2479C-C2DA-4C20-8D7C-67A3756259BD" in makernote_payloads[0]
    assert calls[0][3] == str(still_path)


def test_convert_pair_generates_live_photo_outputs_from_real_jpg_mp4(tmp_path: Path) -> None:
    module = _load_script_module()
    jpg_path = FIXTURE_DIR / "input.jpg"
    mp4_path = FIXTURE_DIR / "input.mp4"
    output_dir = tmp_path / "out"
    asset_id = "b5a2479c-c2da-4c20-8d7c-67a3756259bd"

    still_path, mov_path, returned_asset_id = module.convert_pair(
        SimpleNamespace(
            jpg=jpg_path,
            mp4=mp4_path,
            output_dir=output_dir,
            output_stem="output2",
            asset_id=asset_id,
            overwrite=False,
        )
    )

    assert returned_asset_id == asset_id.upper()
    assert still_path == output_dir / "output2.jpg"
    assert mov_path == output_dir / ".output2.MOV"
    assert _content_identifier(jpg_path) == ""
    assert _content_identifier(still_path) == asset_id.upper()
    assert _content_identifier(mov_path) == asset_id.upper()
    assert _jpeg_scan_payload(still_path) == _jpeg_scan_payload(jpg_path)
    assert not (output_dir / "output2.HEIC").exists()
    assert _xattr_read(still_path, "livephoto") == b".output2.MOV\0"
    assert _xattr_read(still_path, "fileattr") == b"\x10\x00\x00\x00"
    assert _xattr_read(mov_path, "fileattr") == b"\x01\x00\x00\x00"
    expected_heicsize = struct.pack("<Q", still_path.stat().st_size)
    assert _xattr_read(still_path, "heicsize") == expected_heicsize
    assert _xattr_read(mov_path, "heicsize") == expected_heicsize
    assert _xattr_read(still_path, "md5") == hashlib.md5(still_path.read_bytes()).hexdigest().encode()
