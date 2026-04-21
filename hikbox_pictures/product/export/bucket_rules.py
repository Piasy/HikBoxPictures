from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class ExportFaceSample:
    face_observation_id: int
    person_id: int | None
    area: float | None


@dataclass(frozen=True)
class BucketDecision:
    bucket: str
    selected_min_area: float
    threshold: float


def bucket_for_photo(*, selected_person_ids: set[int], faces: Sequence[ExportFaceSample]) -> BucketDecision:
    if not selected_person_ids:
        raise ValueError("selected_person_ids 不能为空")
    if not faces:
        raise ValueError("faces 不能为空")

    selected_faces = [face for face in faces if face.person_id in selected_person_ids]
    selected_present_ids = {int(face.person_id) for face in selected_faces if face.person_id is not None}
    missing_person_ids = sorted(selected_person_ids - selected_present_ids)
    if missing_person_ids:
        raise ValueError(f"照片未命中全部模板人物: missing_person_ids={missing_person_ids}")

    selected_areas: list[float] = []
    for face in selected_faces:
        if face.area is None:
            raise ValueError(f"模板人物面积缺失: face_observation_id={face.face_observation_id}")
        selected_areas.append(float(face.area))

    selected_min_area = min(selected_areas)
    threshold = selected_min_area / 4.0

    for face in faces:
        if face.person_id in selected_person_ids:
            continue
        if face.area is None:
            return BucketDecision(bucket="group", selected_min_area=selected_min_area, threshold=threshold)
        if float(face.area) >= threshold:
            return BucketDecision(bucket="group", selected_min_area=selected_min_area, threshold=threshold)

    return BucketDecision(bucket="only", selected_min_area=selected_min_area, threshold=threshold)


__all__ = [
    "BucketDecision",
    "ExportFaceSample",
    "bucket_for_photo",
]
