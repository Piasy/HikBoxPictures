"""导出 only/group 分桶规则。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FaceBucketInput:
    face_observation_id: int
    area: float | None
    assigned_person_id: int | None
    is_selected_person: bool


def classify_bucket(faces: list[FaceBucketInput]) -> str:
    selected_faces = [face for face in faces if face.is_selected_person]
    if not selected_faces:
        raise ValueError("至少需要一个命中的模板人物人脸")

    selected_areas = [float(face.area) for face in selected_faces if face.area is not None]
    if len(selected_areas) != len(selected_faces):
        return "group"

    threshold = min(selected_areas) / 4.0
    for face in faces:
        if face.is_selected_person:
            continue
        if face.area is None:
            return "group"
        if float(face.area) >= threshold:
            return "group"
    return "only"
