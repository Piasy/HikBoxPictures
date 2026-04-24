import json
from pathlib import Path
import subprocess
import sys

from PIL import Image


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "generate_immich_people_review.py"


def _write_rgb_jpg(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), color=color).save(path)


def _make_asset(
    *,
    batch_dir: Path,
    asset_id: str,
    file_name: str,
    face_ids: list[str],
    color_seed: int,
) -> dict[str, object]:
    image_path = batch_dir / file_name
    _write_rgb_jpg(image_path, (color_seed, min(color_seed + 20, 255), min(color_seed + 40, 255)))
    faces: list[dict[str, str]] = []
    for index, face_id in enumerate(face_ids):
        crop_path = batch_dir / "artifacts" / "crops" / f"{face_id}.jpg"
        context_path = batch_dir / "artifacts" / "context" / f"{face_id}.jpg"
        _write_rgb_jpg(crop_path, (min(color_seed + index + 1, 255), 10, 10))
        _write_rgb_jpg(context_path, (10, min(color_seed + index + 1, 255), 10))
        faces.append(
            {
                "face_id": face_id,
                "crop_path": str(crop_path.resolve()),
                "context_path": str(context_path.resolve()),
            }
        )
    return {
        "asset_id": asset_id,
        "file_name": file_name,
        "image_path": str(image_path.resolve()),
        "face_count_in_asset": len(face_ids),
        "extension": "jpg",
        "faces": faces,
    }


def _write_summary(
    *,
    batch_dir: Path,
    input_root: str,
    persons: list[dict[str, object]],
    unassigned_assets: list[dict[str, object]],
    face_count: int,
    image_count: int,
) -> Path:
    summary = {
        "meta": {
            "generated_at": "2026-04-24T00:00:00+00:00",
            "input_root": input_root,
            "image_count": image_count,
            "face_count": face_count,
            "person_count": len(persons),
            "unassigned_face_count": sum(int(asset["face_count_in_asset"]) for asset in unassigned_assets),
            "asset_with_faces_count": sum(len(person["assets"]) for person in persons) + len(unassigned_assets),
            "failed_image_count": 0,
            "model": "insightface/buffalo_l",
            "max_distance": 0.5,
            "min_faces": 3,
            "min_score": 0.7,
            "recognition_first_pass": {},
            "recognition_second_pass": {},
        },
        "persons": persons,
        "unassigned_assets": unassigned_assets,
        "assets": [],
        "errors": [],
    }
    summary_path = batch_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary_path


def test_generate_immich_people_review_script_merges_batch_outputs_without_copying_artifacts(tmp_path: Path) -> None:
    batch_root = tmp_path / "batches"
    batch_one = batch_root / "batch-1"
    batch_two = batch_root / "batch-2"
    output_dir = tmp_path / "merged-review"

    batch_one_person_asset = _make_asset(
        batch_dir=batch_one,
        asset_id="asset-1",
        file_name="a.jpg",
        face_ids=["face-1"],
        color_seed=40,
    )
    batch_two_person_asset = _make_asset(
        batch_dir=batch_two,
        asset_id="asset-2",
        file_name="b.jpg",
        face_ids=["face-2"],
        color_seed=80,
    )
    batch_two_other_person_asset = _make_asset(
        batch_dir=batch_two,
        asset_id="asset-3",
        file_name="c.jpg",
        face_ids=["face-3"],
        color_seed=120,
    )
    batch_two_unassigned_asset = _make_asset(
        batch_dir=batch_two,
        asset_id="asset-4",
        file_name="d.jpg",
        face_ids=["face-4"],
        color_seed=160,
    )

    _write_summary(
        batch_dir=batch_one,
        input_root="/inputs/batch-1",
        persons=[
            {
                "person_id": "person-1",
                "person_label": "人物 01",
                "person_face_count": 1,
                "asset_count": 1,
                "assets": [batch_one_person_asset],
            }
        ],
        unassigned_assets=[],
        face_count=1,
        image_count=1,
    )
    _write_summary(
        batch_dir=batch_two,
        input_root="/inputs/batch-2",
        persons=[
            {
                "person_id": "person-1",
                "person_label": "人物 01",
                "person_face_count": 1,
                "asset_count": 1,
                "assets": [batch_two_person_asset],
            },
            {
                "person_id": "person-2",
                "person_label": "人物 02",
                "person_face_count": 1,
                "asset_count": 1,
                "assets": [batch_two_other_person_asset],
            },
        ],
        unassigned_assets=[batch_two_unassigned_asset],
        face_count=3,
        image_count=3,
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--merge-from-dir",
            str(batch_root),
            "--output-dir",
            str(output_dir),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout

    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    review_html = (output_dir / "review.html").read_text(encoding="utf-8")

    assert summary["meta"]["image_count"] == 4
    assert summary["meta"]["face_count"] == 4
    assert summary["meta"]["person_count"] == 2
    assert summary["meta"]["unassigned_face_count"] == 1
    assert summary["meta"]["source_summary_count"] == 2
    assert len(summary["meta"]["source_input_roots"]) == 2

    persons_by_id = {person["person_id"]: person for person in summary["persons"]}
    assert set(persons_by_id) == {"person-1", "person-2"}
    assert persons_by_id["person-1"]["person_face_count"] == 2
    assert persons_by_id["person-1"]["asset_count"] == 2
    assert [asset["asset_id"] for asset in persons_by_id["person-1"]["assets"]] == ["asset-1", "asset-2"]
    assert (
        persons_by_id["person-1"]["assets"][0]["faces"][0]["crop_path"]
        == batch_one_person_asset["faces"][0]["crop_path"]
    )
    assert (
        persons_by_id["person-1"]["assets"][1]["faces"][0]["context_path"]
        == batch_two_person_asset["faces"][0]["context_path"]
    )

    assert len(summary["unassigned_assets"]) == 1
    assert summary["unassigned_assets"][0]["asset_id"] == "asset-4"
    assert not (output_dir / "artifacts").exists()
    assert "batches/batch-1/artifacts/crops/face-1.jpg" in review_html
    assert "batches/batch-2/artifacts/context/face-4.jpg" in review_html
    assert (output_dir / "manifest.json").read_text(encoding="utf-8") == (output_dir / "summary.json").read_text(
        encoding="utf-8"
    )
