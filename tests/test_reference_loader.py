from pathlib import Path

import numpy as np
import pytest

from hikbox_pictures.deepface_engine import DetectedFace
from hikbox_pictures.reference_loader import (
    ReferenceImageError,
    load_reference_embedding,
    load_reference_embeddings,
    load_reference_encoding,
)


@pytest.fixture(autouse=True)
def _reset_cached_reference_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hikbox_pictures.reference_loader._CACHED_REFERENCE_ENGINE", None)


class FakeDeepFaceEngine:
    def __init__(self, faces_by_path: dict[Path, object]) -> None:
        self.faces_by_path = faces_by_path
        self.detected_paths: list[Path] = []

    def detect_faces(self, image_path: Path) -> object:
        self.detected_paths.append(image_path)
        return self.faces_by_path.get(image_path, [])


class RaisingDeepFaceEngine(FakeDeepFaceEngine):
    def detect_faces(self, image_path: Path) -> object:
        self.detected_paths.append(image_path)
        raise RuntimeError("engine exploded")


def _make_face(embedding: list[float]) -> DetectedFace:
    return DetectedFace(bbox=(1, 2, 3, 4), embedding=np.asarray(embedding, dtype=np.float32))


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
    engine = FakeDeepFaceEngine(
        {
            img_root: [_make_face([0.1])],
            img_nested: [_make_face([0.2])],
            img_deeper: [_make_face([0.3])],
        }
    )

    embeddings, sources = load_reference_embeddings(tmp_path, engine)

    assert np.allclose(np.stack(embeddings), np.asarray([[0.3], [0.2], [0.1]], dtype=np.float32))
    assert all(isinstance(embedding, np.ndarray) and embedding.dtype == np.float32 for embedding in embeddings)
    assert sources == [img_deeper, img_nested, img_root]
    assert engine.detected_paths == [img_deeper, img_nested, img_root]


def test_load_reference_embeddings_rejects_missing_ref_dir(tmp_path: Path) -> None:
    missing_dir = tmp_path / "not-exists"
    engine = FakeDeepFaceEngine({})

    with pytest.raises(ReferenceImageError, match="参考目录不存在"):
        load_reference_embeddings(missing_dir, engine)


def test_load_reference_embeddings_rejects_non_directory_ref_dir(tmp_path: Path) -> None:
    not_dir = tmp_path / "file.jpg"
    not_dir.write_bytes(b"img")
    engine = FakeDeepFaceEngine({})

    with pytest.raises(ReferenceImageError, match="参考目录不是文件夹"):
        load_reference_embeddings(not_dir, engine)


def test_load_reference_embeddings_rejects_empty_directory(tmp_path: Path) -> None:
    engine = FakeDeepFaceEngine({})

    with pytest.raises(ReferenceImageError, match="未找到支持的参考图片"):
        load_reference_embeddings(tmp_path, engine)


def test_load_reference_embeddings_rejects_zero_faces(tmp_path: Path) -> None:
    photo = tmp_path / "person-a.jpg"
    photo.write_bytes(b"img")
    engine = FakeDeepFaceEngine({photo: []})

    with pytest.raises(ReferenceImageError, match="必须且仅能检测到 1 张人脸"):
        load_reference_embeddings(tmp_path, engine)


def test_load_reference_embeddings_rejects_multiple_faces(tmp_path: Path) -> None:
    photo = tmp_path / "group.jpg"
    photo.write_bytes(b"img")
    engine = FakeDeepFaceEngine({photo: [_make_face([0.1]), _make_face([0.2])]})

    with pytest.raises(ReferenceImageError, match="必须且仅能检测到 1 张人脸"):
        load_reference_embeddings(tmp_path, engine)


def test_load_reference_embeddings_wraps_engine_error_with_source_path(tmp_path: Path) -> None:
    photo = tmp_path / "broken.jpg"
    photo.write_bytes(b"img")
    engine = RaisingDeepFaceEngine({})

    with pytest.raises(ReferenceImageError, match=r"broken\.jpg"):
        load_reference_embeddings(tmp_path, engine)


def test_load_reference_embeddings_rejects_none_faces_with_source_path(tmp_path: Path) -> None:
    photo = tmp_path / "none-faces.jpg"
    photo.write_bytes(b"img")
    engine = FakeDeepFaceEngine({photo: None})

    with pytest.raises(ReferenceImageError) as exc_info:
        load_reference_embeddings(tmp_path, engine)

    assert "none-faces.jpg" in str(exc_info.value)


def test_load_reference_embeddings_rejects_non_sequence_faces_with_source_path(tmp_path: Path) -> None:
    photo = tmp_path / "non-seq-faces.jpg"
    photo.write_bytes(b"img")
    engine = FakeDeepFaceEngine({photo: 123})

    with pytest.raises(ReferenceImageError) as exc_info:
        load_reference_embeddings(tmp_path, engine)

    assert "non-seq-faces.jpg" in str(exc_info.value)


def test_load_reference_embeddings_rejects_face_without_embedding_with_source_path(tmp_path: Path) -> None:
    photo = tmp_path / "no-embedding-face.jpg"
    photo.write_bytes(b"img")

    class _FaceWithoutEmbedding:
        pass

    engine = FakeDeepFaceEngine({photo: [_FaceWithoutEmbedding()]})

    with pytest.raises(ReferenceImageError) as exc_info:
        load_reference_embeddings(tmp_path, engine)

    assert "no-embedding-face.jpg" in str(exc_info.value)


def test_load_reference_embeddings_rejects_non_1d_embedding_with_source_path(tmp_path: Path) -> None:
    photo = tmp_path / "bad-shape.jpg"
    photo.write_bytes(b"img")
    bad_face = DetectedFace(bbox=(1, 2, 3, 4), embedding=np.asarray([[0.1, 0.2]], dtype=np.float32))
    engine = FakeDeepFaceEngine({photo: [bad_face]})

    with pytest.raises(ReferenceImageError) as exc_info:
        load_reference_embeddings(tmp_path, engine)

    assert "bad-shape.jpg" in str(exc_info.value)
    assert "embedding" in str(exc_info.value)


def test_load_reference_embeddings_rejects_non_convertible_embedding_with_source_path(tmp_path: Path) -> None:
    photo = tmp_path / "bad-type.jpg"
    photo.write_bytes(b"img")
    bad_face = DetectedFace(bbox=(1, 2, 3, 4), embedding="abc")
    engine = FakeDeepFaceEngine({photo: [bad_face]})

    with pytest.raises(ReferenceImageError) as exc_info:
        load_reference_embeddings(tmp_path, engine)

    assert "bad-type.jpg" in str(exc_info.value)
    assert "embedding" in str(exc_info.value)


def test_load_reference_embedding_returns_float32_ndarray(tmp_path: Path) -> None:
    photo = tmp_path / "solo.jpg"
    photo.write_bytes(b"img")
    engine = FakeDeepFaceEngine({photo: [_make_face([0.1, 0.2])]})

    embedding = load_reference_embedding(photo, engine)

    assert isinstance(embedding, np.ndarray)
    assert embedding.dtype == np.float32
    assert embedding.tolist() == pytest.approx([0.1, 0.2])


def test_load_reference_encoding_is_backward_compatible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    photo = tmp_path / "legacy.jpg"
    photo.write_bytes(b"img")

    class _CompatEngine:
        def detect_faces(self, image_path: Path) -> list[DetectedFace]:
            assert image_path == photo
            return [_make_face([0.4, 0.5])]

    monkeypatch.setattr("hikbox_pictures.reference_loader.DeepFaceEngine.create", lambda: _CompatEngine())

    encoding = load_reference_encoding(photo)

    assert isinstance(encoding, np.ndarray)
    assert encoding.dtype == np.float32
    assert encoding.tolist() == pytest.approx([0.4, 0.5])


def test_load_reference_encoding_wraps_detect_faces_error_with_source_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    photo = tmp_path / "legacy-detect-failed.jpg"
    photo.write_bytes(b"img")

    class _CompatEngine:
        def detect_faces(self, image_path: Path) -> list[DetectedFace]:
            assert image_path == photo
            raise RuntimeError("detect failed")

    monkeypatch.setattr("hikbox_pictures.reference_loader.DeepFaceEngine.create", lambda: _CompatEngine())

    with pytest.raises(ReferenceImageError) as exc_info:
        load_reference_encoding(photo)

    assert "legacy-detect-failed.jpg" in str(exc_info.value)
    assert "检测参考图片人脸失败" in str(exc_info.value)


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

    monkeypatch.setattr("hikbox_pictures.reference_loader.DeepFaceEngine.create", _create_engine)

    assert load_reference_encoding(photo_a).tolist() == pytest.approx([0.1])
    assert load_reference_encoding(photo_b).tolist() == pytest.approx([0.2])
    assert create_calls == 1


def test_load_reference_encoding_wraps_create_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    photo = tmp_path / "legacy-create-failed.jpg"
    photo.write_bytes(b"img")

    def _raise_create_error() -> object:
        raise RuntimeError("init failed")

    monkeypatch.setattr("hikbox_pictures.reference_loader.DeepFaceEngine.create", _raise_create_error)

    with pytest.raises(ReferenceImageError, match="legacy-create-failed.jpg"):
        load_reference_encoding(photo)
