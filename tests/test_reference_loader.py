import pytest

from hikbox_pictures.reference_loader import ReferenceImageError, load_reference_encoding


def test_load_reference_encoding_rejects_zero_faces(monkeypatch, tmp_path) -> None:
    photo = tmp_path / "person-a.jpg"
    photo.write_bytes(b"image")
    monkeypatch.setattr("hikbox_pictures.reference_loader.load_rgb_image", lambda _: "image")
    monkeypatch.setattr("hikbox_pictures.reference_loader.face_recognition.face_encodings", lambda image: [])

    with pytest.raises(ReferenceImageError, match="exactly one face"):
        load_reference_encoding(photo)


def test_load_reference_encoding_returns_single_face(monkeypatch, tmp_path) -> None:
    photo = tmp_path / "person-b.jpg"
    photo.write_bytes(b"image")
    encoding = [0.1, 0.2, 0.3]
    monkeypatch.setattr("hikbox_pictures.reference_loader.load_rgb_image", lambda _: "image")
    monkeypatch.setattr(
        "hikbox_pictures.reference_loader.face_recognition.face_encodings",
        lambda image: [encoding],
    )

    assert load_reference_encoding(photo) == encoding


def test_load_reference_encoding_rejects_multiple_faces(monkeypatch, tmp_path) -> None:
    photo = tmp_path / "group.jpg"
    photo.write_bytes(b"image")
    monkeypatch.setattr("hikbox_pictures.reference_loader.load_rgb_image", lambda _: "image")
    monkeypatch.setattr(
        "hikbox_pictures.reference_loader.face_recognition.face_encodings",
        lambda image: [[0.1], [0.2]],
    )

    with pytest.raises(ReferenceImageError, match="exactly one face"):
        load_reference_encoding(photo)
