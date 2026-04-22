import json
from pathlib import Path

from hikbox_pictures.face_review_diff import build_cluster_diff_payload, write_cluster_diff_review


def _member(face_id: str) -> dict[str, object]:
    return {
        "face_id": face_id,
        "crop_relpath": f"assets/crops/{face_id}.jpg",
        "context_relpath": f"assets/context/{face_id}.jpg",
        "quality_score": 0.95,
        "magface_quality": 25.0,
        "cluster_probability": 1.0,
    }


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_build_cluster_diff_payload_ignores_label_only_changes(tmp_path: Path) -> None:
    base_manifest = tmp_path / "base" / "manifest.json"
    candidate_manifest = tmp_path / "candidate" / "manifest.json"
    output_html = tmp_path / "diff" / "review.html"

    _write_manifest(
        base_manifest,
        {
            "meta": {"clusterer": "HDBSCAN"},
            "clusters": [
                {"cluster_key": "cluster_0", "cluster_label": 0, "members": [_member("f1"), _member("f2")]},
                {"cluster_key": "cluster_1", "cluster_label": 1, "members": [_member("f3")]},
                {"cluster_key": "noise", "cluster_label": -1, "members": [_member("f4"), _member("f5")]},
            ],
        },
    )
    _write_manifest(
        candidate_manifest,
        {
            "meta": {"clusterer": "HDBSCAN"},
            "clusters": [
                {"cluster_key": "cluster_10", "cluster_label": 10, "members": [_member("f1"), _member("f2"), _member("f4")]},
                {"cluster_key": "cluster_22", "cluster_label": 22, "members": [_member("f3")]},
                {"cluster_key": "noise", "cluster_label": -1, "members": [_member("f5")]},
            ],
        },
    )

    payload = build_cluster_diff_payload(
        base_manifest_path=base_manifest,
        candidate_manifest_path=candidate_manifest,
        output_html_path=output_html,
    )

    assert payload["meta"]["changed_face_count"] == 3
    assert [group["cluster_key"] for group in payload["candidate_groups"]] == ["cluster_10"]
    assert [group["cluster_key"] for group in payload["base_groups"]] == ["cluster_0", "noise"]
    assert [item["face_count"] for item in payload["candidate_groups"][0]["source_groups"]] == [2, 1]
    assert [item["cluster_key"] for item in payload["candidate_groups"][0]["source_groups"]] == ["cluster_0", "noise"]
    assert [member["face_id"] for member in payload["base_groups"][1]["members"]] == ["f4"]
    assert payload["base_groups"][1]["full_member_count"] == 2


def test_write_cluster_diff_review_outputs_changed_group_html(tmp_path: Path) -> None:
    base_manifest = tmp_path / "base" / "manifest.json"
    candidate_manifest = tmp_path / "candidate" / "manifest.json"
    output_html = tmp_path / "diff" / "review.html"

    _write_manifest(
        base_manifest,
        {
            "meta": {"clusterer": "HDBSCAN"},
            "clusters": [
                {"cluster_key": "cluster_0", "cluster_label": 0, "members": [_member("f1"), _member("f2")]},
                {"cluster_key": "noise", "cluster_label": -1, "members": [_member("f4")]},
            ],
        },
    )
    _write_manifest(
        candidate_manifest,
        {
            "meta": {"clusterer": "HDBSCAN"},
            "clusters": [
                {"cluster_key": "cluster_10", "cluster_label": 10, "members": [_member("f1"), _member("f2"), _member("f4")]},
            ],
        },
    )

    payload = write_cluster_diff_review(
        base_manifest_path=base_manifest,
        candidate_manifest_path=candidate_manifest,
        output_html_path=output_html,
    )

    html = output_html.read_text(encoding="utf-8")

    assert payload["meta"]["candidate_changed_group_count"] == 1
    assert "微簇变化对比" in html
    assert "cluster_10" in html
    assert "cluster_0" in html
    assert "变化样本 3" in html
    assert "新版变化分组" in html
    assert "基线相关分组" not in html
