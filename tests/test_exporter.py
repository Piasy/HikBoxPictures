from datetime import datetime, timezone

from hikbox_pictures.exporter import build_destination_path, copy_with_metadata, export_match
from hikbox_pictures.models import CandidatePhoto, MatchBucket, PhotoEvaluation


def test_build_destination_path_uses_bucket_and_year_month(tmp_path) -> None:
    source = tmp_path / "source.jpg"
    source.write_bytes(b"image")

    destination = build_destination_path(
        source,
        output_root=tmp_path / "output",
        bucket=MatchBucket.ONLY_TWO,
        year_month="2025-04",
    )

    assert destination == tmp_path / "output" / "only-two" / "2025-04" / "source.jpg"


def test_build_destination_path_avoids_overwriting_existing_files(tmp_path) -> None:
    source = tmp_path / "duplicate.jpg"
    source.write_bytes(b"image")
    occupied = tmp_path / "output" / "group" / "2025-04" / "duplicate.jpg"
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b"existing")

    destination = build_destination_path(
        source,
        output_root=tmp_path / "output",
        bucket=MatchBucket.GROUP,
        year_month="2025-04",
    )

    assert destination.name.startswith("duplicate__")
    assert destination.suffix == ".jpg"


def test_copy_with_metadata_preserves_mtime(monkeypatch, tmp_path) -> None:
    source = tmp_path / "source.jpg"
    destination = tmp_path / "copy.jpg"
    source.write_bytes(b"image")

    monkeypatch.setattr("hikbox_pictures.exporter.set_creation_time", lambda source_path, dest_path: None)

    copy_with_metadata(source, destination)

    assert destination.read_bytes() == b"image"
    assert int(destination.stat().st_mtime) == int(source.stat().st_mtime)


def test_export_match_copies_photo_and_paired_live_photo(monkeypatch, tmp_path) -> None:
    photo_path = tmp_path / "IMG_0001.HEIC"
    mov_path = tmp_path / ".IMG_0001_123456.MOV"
    photo_path.write_bytes(b"photo")
    mov_path.write_bytes(b"movie")

    evaluation = PhotoEvaluation(
        candidate=CandidatePhoto(path=photo_path, live_photo_video=mov_path),
        detected_face_count=2,
        bucket=MatchBucket.ONLY_TWO,
    )
    monkeypatch.setattr("hikbox_pictures.exporter.set_creation_time", lambda source_path, dest_path: None)

    copied = export_match(
        evaluation,
        output_root=tmp_path / "output",
        capture_datetime=datetime(2025, 4, 3, 10, 30, tzinfo=timezone.utc),
    )

    assert [path.name for path in copied] == ["IMG_0001.HEIC", ".IMG_0001_123456.MOV"]
    assert (tmp_path / "output" / "only-two" / "2025-04" / "IMG_0001.HEIC").read_bytes() == b"photo"
    assert (tmp_path / "output" / "only-two" / "2025-04" / ".IMG_0001_123456.MOV").read_bytes() == b"movie"


def test_export_match_skips_missing_live_photo_video(monkeypatch, tmp_path) -> None:
    photo_path = tmp_path / "IMG_0002.HEIC"
    mov_path = tmp_path / ".IMG_0002_123456.MOV"
    photo_path.write_bytes(b"photo")

    evaluation = PhotoEvaluation(
        candidate=CandidatePhoto(path=photo_path, live_photo_video=mov_path),
        detected_face_count=2,
        bucket=MatchBucket.ONLY_TWO,
    )
    monkeypatch.setattr("hikbox_pictures.exporter.set_creation_time", lambda source_path, dest_path: None)

    copied = export_match(
        evaluation,
        output_root=tmp_path / "output",
        capture_datetime=datetime(2025, 4, 3, 10, 30, tzinfo=timezone.utc),
    )

    assert [path.name for path in copied] == ["IMG_0002.HEIC"]
    assert (tmp_path / "output" / "only-two" / "2025-04" / "IMG_0002.HEIC").read_bytes() == b"photo"
    assert not (tmp_path / "output" / "only-two" / "2025-04" / ".IMG_0002_123456.MOV").exists()
