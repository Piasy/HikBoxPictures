from pathlib import Path

from hikbox_pictures.scanner import find_live_photo_video, iter_candidate_photos


def test_iter_candidate_photos_recurses_and_filters_supported_extensions(tmp_path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()

    jpg = tmp_path / "portrait.jpg"
    heic = nested / "IMG_0001.HEIC"
    ignored = tmp_path / "notes.txt"
    mov = nested / ".IMG_0001_123456.MOV"

    jpg.write_bytes(b"jpg")
    heic.write_bytes(b"heic")
    ignored.write_text("ignore me")
    mov.write_bytes(b"mov")

    candidates = list(iter_candidate_photos(tmp_path))

    assert [candidate.path.name for candidate in candidates] == ["IMG_0001.HEIC", "portrait.jpg"]
    assert candidates[0].live_photo_video == mov
    assert candidates[1].live_photo_video is None


def test_find_live_photo_video_ignores_non_matching_hidden_mov(tmp_path) -> None:
    heic = tmp_path / "IMG_0002.HEIC"
    heic.write_bytes(b"heic")
    (tmp_path / ".IMG_9999_987654.MOV").write_bytes(b"wrong")

    assert find_live_photo_video(heic) is None


def test_find_live_photo_video_matches_lowercase_hidden_mov_on_case_sensitive_glob(
    tmp_path, monkeypatch
) -> None:
    heic = tmp_path / "IMG_0003.HEIC"
    mov = tmp_path / ".IMG_0003_123456.mov"
    heic.write_bytes(b"heic")
    mov.write_bytes(b"mov")

    original_glob = Path.glob

    def case_sensitive_glob(self: Path, pattern: str):
        if self == tmp_path and pattern == f".{heic.stem}_*.MOV":
            return []
        return original_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", case_sensitive_glob)

    assert find_live_photo_video(heic) == mov


def test_iter_candidate_photos_does_not_rescan_same_directory_for_each_heic(tmp_path, monkeypatch) -> None:
    heic_a = tmp_path / "IMG_0100.HEIC"
    heic_b = tmp_path / "IMG_0101.HEIC"
    mov_a = tmp_path / ".IMG_0100_123456.MOV"
    mov_b = tmp_path / ".IMG_0101_123456.MOV"

    heic_a.write_bytes(b"heic-a")
    heic_b.write_bytes(b"heic-b")
    mov_a.write_bytes(b"mov-a")
    mov_b.write_bytes(b"mov-b")

    original_iterdir = Path.iterdir
    root_iterdir_calls = 0

    def counting_iterdir(self: Path):
        nonlocal root_iterdir_calls
        if self == tmp_path:
            root_iterdir_calls += 1
            if root_iterdir_calls > 1:
                raise AssertionError("同一目录不应为每张 HEIC 重复扫描")
        yield from original_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", counting_iterdir)

    candidates = list(iter_candidate_photos(tmp_path))

    assert [candidate.path.name for candidate in candidates] == ["IMG_0100.HEIC", "IMG_0101.HEIC"]
    assert candidates[0].live_photo_video == mov_a
    assert candidates[1].live_photo_video == mov_b
