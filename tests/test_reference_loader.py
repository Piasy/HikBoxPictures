from pathlib import Path

import pytest

from hikbox_pictures.insightface_engine import DetectedFace
from hikbox_pictures.reference_loader import (
    ReferenceImageError,
    load_reference_embedding,
    load_reference_embeddings,
    load_reference_encoding,
)


@pytest.fixture(autouse=True)
def _reset_cached_reference_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hikbox_pictures.reference_loader._CACHED_REFERENCE_ENGINE", None)


class FakeInsightFaceEngine:
    def __init__(self, faces_by_path: dict[Path, list[DetectedFace]]) -> None:
        self.faces_by_path = faces_by_path
        self.detected_paths: list[Path] = []

    def detect_faces(self, image_path: Path) -> list[DetectedFace]:
        self.detected_paths.append(image_path)
        return self.faces_by_path.get(image_path, [])


class RaisingInsightFaceEngine(FakeInsightFaceEngine):
    def detect_faces(self, image_path: Path) -> list[DetectedFace]:
        self.detected_paths.append(image_path)
        raise RuntimeError("engine exploded")


def _make_face(embedding: list[float]) -> DetectedFace:
    return DetectedFace(bbox=(1, 2, 3, 4), embedding=embedding)


def test_load_reference_embeddings_recurses_and_filters_extensions(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    deeper = nested / "deeper"
    deeper.mkdir(parents=True)
    ignored = tmp_path / "notes.txt"
    img_root = tmp_path / "root.jpeg"
    img_nested = nested / "inside.HEIC"
    img_deeper = deeper / "last.png"
    ignored.write_text("ignore")
    img_root.write_bytes(b"img")
    img_nested.write_bytes(b"img")
    img_deeper.write_bytes(b"img")
    engine = FakeInsightFaceEngine(
        {
            img_root: [_make_face([0.1])],
            img_nested: [_make_face([0.2])],
            img_deeper: [_make_face([0.3])],
        }
    )

    embeddings, sources = load_reference_embeddings(tmp_path, engine)

    assert embeddings == [[0.3], [0.2], [0.1]]
    assert sources == [img_deeper, img_nested, img_root]
    assert engine.detected_paths == [img_deeper, img_nested, img_root]


def test_load_reference_embeddings_uses_engine_default_det_size(tmp_path: Path) -> None:
    photo = tmp_path / "person-a.jpg"
    photo.write_bytes(b"img")
    seen_calls: list[tuple[Path, tuple[int, int] | None]] = []

    class Engine:
        def detect_faces(self, image_path: Path, *, det_size=None):
            seen_calls.append((image_path, det_size))
            return [_make_face([0.1])]

    embeddings, sources = load_reference_embeddings(tmp_path, Engine())

    assert embeddings == [[0.1]]
    assert sources == [photo]
    assert seen_calls == [(photo, None)]


def test_load_reference_embeddings_rejects_empty_directory(tmp_path: Path) -> None:
    engine = FakeInsightFaceEngine({})

    with pytest.raises(ReferenceImageError, match="No supported reference images"):
        load_reference_embeddings(tmp_path, engine)


def test_load_reference_embeddings_rejects_zero_faces(tmp_path: Path) -> None:
    photo = tmp_path / "person-a.jpg"
    photo.write_bytes(b"img")
    engine = FakeInsightFaceEngine({photo: []})

    with pytest.raises(ReferenceImageError, match="exactly one face"):
        load_reference_embeddings(tmp_path, engine)


def test_load_reference_embeddings_rejects_multiple_faces(tmp_path: Path) -> None:
    photo = tmp_path / "group.jpg"
    photo.write_bytes(b"img")
    engine = FakeInsightFaceEngine({photo: [_make_face([0.1]), _make_face([0.2])]})

    with pytest.raises(ReferenceImageError, match="exactly one face"):
        load_reference_embeddings(tmp_path, engine)


def test_load_reference_embeddings_wraps_engine_error_with_source_path(tmp_path: Path) -> None:
    photo = tmp_path / "broken.jpg"
    photo.write_bytes(b"img")
    engine = RaisingInsightFaceEngine({})

    with pytest.raises(ReferenceImageError, match=r"broken\.jpg"):
        load_reference_embeddings(tmp_path, engine)


def test_load_reference_embedding_returns_single_embedding(tmp_path: Path) -> None:
    photo = tmp_path / "solo.jpg"
    photo.write_bytes(b"img")
    engine = FakeInsightFaceEngine({photo: [_make_face([0.1, 0.2])]})

    embedding = load_reference_embedding(photo, engine)

    assert embedding == [0.1, 0.2]


def test_load_reference_encoding_is_backward_compatible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    photo = tmp_path / "legacy.jpg"
    photo.write_bytes(b"img")

    class _CompatEngine:
        def detect_faces(self, image_path: Path) -> list[DetectedFace]:
            assert image_path == photo
            return [_make_face([0.4, 0.5])]

    monkeypatch.setattr("hikbox_pictures.reference_loader.InsightFaceEngine.create", lambda: _CompatEngine())

    encoding = load_reference_encoding(photo)

    assert encoding == [0.4, 0.5]


def test_load_reference_encoding_reuses_engine_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    photo_a = tmp_path / "legacy-a.jpg"
    photo_b = tmp_path / "legacy-b.jpg"
    photo_a.write_bytes(b"img")
    photo_b.write_bytes(b"img")
    create_calls = 0

    class _CompatEngine:
        def detect_faces(self, image_path: Path) -> list[DetectedFace]:
            if image_path == photo_a:
                return [_make_face([0.1])]
            if image_path == photo_b:
                return [_make_face([0.2])]
            raise AssertionError(f"unexpected path: {image_path}")

    def _create_engine() -> _CompatEngine:
        nonlocal create_calls
        create_calls += 1
        return _CompatEngine()

    monkeypatch.setattr("hikbox_pictures.reference_loader.InsightFaceEngine.create", _create_engine)

    assert load_reference_encoding(photo_a) == [0.1]
    assert load_reference_encoding(photo_b) == [0.2]
    assert create_calls == 1


def test_load_reference_encoding_wraps_create_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    photo = tmp_path / "legacy-create-failed.jpg"
    photo.write_bytes(b"img")

    def _raise_create_error() -> object:
        raise RuntimeError("init failed")

    monkeypatch.setattr("hikbox_pictures.reference_loader.InsightFaceEngine.create", _raise_create_error)

    with pytest.raises(ReferenceImageError, match="legacy-create-failed.jpg"):
        load_reference_encoding(photo)
