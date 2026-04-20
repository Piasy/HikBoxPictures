import json
from pathlib import Path

from hikbox_pictures.face_review_person_diff import (
    build_person_added_diff_payload,
    write_person_added_diff_review,
)


def _member(face_id: str, quality_score: float = 0.95) -> dict[str, object]:
    return {
        "face_id": face_id,
        "crop_relpath": f"assets/crops/{face_id}.jpg",
        "context_relpath": f"assets/context/{face_id}.jpg",
        "quality_score": quality_score,
        "magface_quality": 25.0,
        "cluster_probability": 1.0,
    }


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_build_person_added_diff_payload_extracts_added_members_by_person_label(tmp_path: Path) -> None:
    base_manifest = tmp_path / "base" / "manifest.json"
    candidate_manifest = tmp_path / "candidate" / "manifest.json"
    output_html = tmp_path / "diff" / "review.html"

    f1 = _member("f1")
    f2 = _member("f2")
    f3 = _member("f3")
    f4 = _member("f4")
    f5 = _member("f5")

    _write_manifest(
        base_manifest,
        {
            "meta": {"clusterer": "HDBSCAN"},
            "clusters": [
                {"cluster_key": "cluster_7", "cluster_label": 7, "members": [f1, f2]},
                {"cluster_key": "cluster_8", "cluster_label": 8, "members": [f3]},
                {"cluster_key": "noise", "cluster_label": -1, "members": [f4]},
            ],
            "persons": [
                {
                    "person_label": 0,
                    "person_key": "person_0",
                    "person_cluster_count": 1,
                    "person_face_count": 2,
                    "clusters": [
                        {"cluster_key": "cluster_7", "cluster_label": 7, "member_count": 2, "members": [f1, f2]},
                    ],
                },
                {
                    "person_label": 1,
                    "person_key": "person_1",
                    "person_cluster_count": 1,
                    "person_face_count": 1,
                    "clusters": [
                        {"cluster_key": "cluster_8", "cluster_label": 8, "member_count": 1, "members": [f3]},
                    ],
                },
            ],
        },
    )
    _write_manifest(
        candidate_manifest,
        {
            "meta": {"clusterer": "HDBSCAN"},
            "clusters": [
                {"cluster_key": "cluster_70", "cluster_label": 70, "members": [f1, f2, f4]},
                {"cluster_key": "cluster_80", "cluster_label": 80, "members": [f3, f5]},
            ],
            "persons": [
                {
                    "person_label": 0,
                    "person_key": "person_0",
                    "person_cluster_count": 1,
                    "person_face_count": 3,
                    "clusters": [
                        {"cluster_key": "cluster_70", "cluster_label": 70, "member_count": 3, "members": [f1, f2, f4]},
                    ],
                },
                {
                    "person_label": 1,
                    "person_key": "person_1",
                    "person_cluster_count": 1,
                    "person_face_count": 2,
                    "clusters": [
                        {"cluster_key": "cluster_80", "cluster_label": 80, "member_count": 2, "members": [f3, f5]},
                    ],
                },
                {
                    "person_label": 2,
                    "person_key": "person_2",
                    "person_cluster_count": 1,
                    "person_face_count": 1,
                    "clusters": [
                        {"cluster_key": "cluster_90", "cluster_label": 90, "member_count": 1, "members": [_member("f6")]},
                    ],
                },
            ],
        },
    )

    payload = build_person_added_diff_payload(
        base_manifest_path=base_manifest,
        candidate_manifest_path=candidate_manifest,
        output_html_path=output_html,
        person_labels=[0, 1],
    )

    assert payload["meta"]["target_person_count"] == 2
    assert payload["meta"]["person_with_additions_count"] == 2
    assert payload["meta"]["total_added_face_count"] == 2

    person0 = payload["persons"][0]
    person1 = payload["persons"][1]
    assert person0["person_label"] == 0
    assert person0["base_member_count"] == 2
    assert [m["face_id"] for m in person0["added_members"]] == ["f4"]
    assert person0["source_summary"][0]["cluster_key"] == "noise"

    assert person1["person_label"] == 1
    assert person1["base_member_count"] == 1
    assert [m["face_id"] for m in person1["added_members"]] == ["f5"]
    assert person1["source_summary"][0]["cluster_key"] == "missing"


def test_write_person_added_diff_review_outputs_html(tmp_path: Path) -> None:
    base_manifest = tmp_path / "base" / "manifest.json"
    candidate_manifest = tmp_path / "candidate" / "manifest.json"
    output_html = tmp_path / "diff" / "person_added.html"

    f1 = _member("f1")
    f2 = _member("f2", quality_score=0.80)
    f3 = _member("f3", quality_score=0.99)

    _write_manifest(
        base_manifest,
        {
            "meta": {"clusterer": "HDBSCAN"},
            "clusters": [
                {"cluster_key": "cluster_7", "cluster_label": 7, "members": [f1]},
            ],
            "persons": [
                {
                    "person_label": 0,
                    "person_key": "person_0",
                    "person_cluster_count": 1,
                    "person_face_count": 1,
                    "clusters": [
                        {"cluster_key": "cluster_7", "cluster_label": 7, "member_count": 1, "members": [f1]},
                    ],
                },
            ],
        },
    )
    _write_manifest(
        candidate_manifest,
        {
            "meta": {"clusterer": "HDBSCAN"},
            "clusters": [
                {"cluster_key": "cluster_70", "cluster_label": 70, "members": [f1, f2, f3]},
            ],
            "persons": [
                {
                    "person_label": 0,
                    "person_key": "person_0",
                    "person_cluster_count": 1,
                    "person_face_count": 3,
                    "clusters": [
                        {"cluster_key": "cluster_70", "cluster_label": 70, "member_count": 3, "members": [f1, f2, f3]},
                    ],
                },
            ],
        },
    )

    payload = write_person_added_diff_review(
        base_manifest_path=base_manifest,
        candidate_manifest_path=candidate_manifest,
        output_html_path=output_html,
        person_labels=[0],
    )

    assert payload["meta"]["total_added_face_count"] == 2
    assert [m["face_id"] for m in payload["persons"][0]["added_members"]] == ["f3", "f2"]
    html = output_html.read_text(encoding="utf-8")
    assert "人物新增样本对比" in html
    assert "person 0" in html
    assert "新增样本 2" in html
    assert html.index("<strong>f3</strong>") < html.index("<strong>f2</strong>")
    assert "f3" in html
