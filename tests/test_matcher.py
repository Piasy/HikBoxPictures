import pytest

from hikbox_pictures.matcher import CandidateDecodeError, evaluate_candidate_photo
from hikbox_pictures.models import CandidatePhoto, MatchBucket


def test_evaluate_candidate_photo_classifies_only_two(monkeypatch, tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "pair.jpg")
    monkeypatch.setattr("hikbox_pictures.matcher.load_rgb_image", lambda _: "image")
    monkeypatch.setattr("hikbox_pictures.matcher.face_recognition.face_locations", lambda _: [1, 2])
    monkeypatch.setattr(
        "hikbox_pictures.matcher.face_recognition.face_encodings",
        lambda image, known_face_locations=None: [["face-1"], ["face-2"]],
    )
    compare_results = iter([[True, False], [False, True]])
    monkeypatch.setattr(
        "hikbox_pictures.matcher.face_recognition.compare_faces",
        lambda encodings, target, tolerance=0.5: next(compare_results),
    )

    evaluation = evaluate_candidate_photo(photo, [0.1], [0.2])

    assert evaluation.detected_face_count == 2
    assert evaluation.bucket is MatchBucket.ONLY_TWO


def test_evaluate_candidate_photo_classifies_group(monkeypatch, tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "group.jpg")
    monkeypatch.setattr("hikbox_pictures.matcher.load_rgb_image", lambda _: "image")
    monkeypatch.setattr("hikbox_pictures.matcher.face_recognition.face_locations", lambda _: [1, 2, 3])
    monkeypatch.setattr(
        "hikbox_pictures.matcher.face_recognition.face_encodings",
        lambda image, known_face_locations=None: [["face-1"], ["face-2"], ["face-3"]],
    )
    compare_results = iter([[True, False, False], [False, True, False]])
    monkeypatch.setattr(
        "hikbox_pictures.matcher.face_recognition.compare_faces",
        lambda encodings, target, tolerance=0.5: next(compare_results),
    )

    evaluation = evaluate_candidate_photo(photo, [0.1], [0.2])

    assert evaluation.detected_face_count == 3
    assert evaluation.bucket is MatchBucket.GROUP


def test_evaluate_candidate_photo_requires_both_people(monkeypatch, tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "solo.jpg")
    monkeypatch.setattr("hikbox_pictures.matcher.load_rgb_image", lambda _: "image")
    monkeypatch.setattr("hikbox_pictures.matcher.face_recognition.face_locations", lambda _: [1, 2])
    monkeypatch.setattr(
        "hikbox_pictures.matcher.face_recognition.face_encodings",
        lambda image, known_face_locations=None: [["face-1"], ["face-2"]],
    )
    compare_results = iter([[True, False], [False, False]])
    monkeypatch.setattr(
        "hikbox_pictures.matcher.face_recognition.compare_faces",
        lambda encodings, target, tolerance=0.5: next(compare_results),
    )

    evaluation = evaluate_candidate_photo(photo, [0.1], [0.2])

    assert evaluation.bucket is None
    assert evaluation.detected_face_count == 2


def test_evaluate_candidate_photo_requires_distinct_matching_faces(monkeypatch, tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "ambiguous.jpg")
    monkeypatch.setattr("hikbox_pictures.matcher.load_rgb_image", lambda _: "image")
    monkeypatch.setattr("hikbox_pictures.matcher.face_recognition.face_locations", lambda _: [1])
    monkeypatch.setattr(
        "hikbox_pictures.matcher.face_recognition.face_encodings",
        lambda image, known_face_locations=None: [["face-1"]],
    )
    compare_results = iter([[True], [True]])
    monkeypatch.setattr(
        "hikbox_pictures.matcher.face_recognition.compare_faces",
        lambda encodings, target, tolerance=0.5: next(compare_results),
    )

    evaluation = evaluate_candidate_photo(photo, [0.1], [0.2])

    assert evaluation.bucket is None
    assert evaluation.detected_face_count == 1


def test_evaluate_candidate_photo_wraps_decode_errors(monkeypatch, tmp_path) -> None:
    photo = CandidatePhoto(path=tmp_path / "broken.jpg")

    def raise_decode_error(_path):
        raise OSError("cannot decode")

    monkeypatch.setattr("hikbox_pictures.matcher.load_rgb_image", raise_decode_error)

    with pytest.raises(CandidateDecodeError, match="cannot decode"):
        evaluate_candidate_photo(photo, [0.1], [0.2])
