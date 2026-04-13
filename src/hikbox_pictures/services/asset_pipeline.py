from __future__ import annotations

from typing import Literal

AssetStage = Literal["metadata", "faces", "embeddings", "assignment"]
AssignmentDecision = Literal["auto_assign", "review", "new_person_candidate"]
AssetStatus = Literal[
    "discovered",
    "metadata_done",
    "faces_done",
    "embeddings_done",
    "assignment_done",
]

DEFAULT_AUTO_ASSIGN_THRESHOLD = 0.25
DEFAULT_REVIEW_THRESHOLD = 0.35

STAGE_ORDER: tuple[AssetStage, ...] = ("metadata", "faces", "embeddings", "assignment")
STATUS_ORDER: tuple[AssetStatus, ...] = (
    "discovered",
    "metadata_done",
    "faces_done",
    "embeddings_done",
    "assignment_done",
)

STAGE_PREVIOUS_STATUS: dict[AssetStage, AssetStatus] = {
    "metadata": "discovered",
    "faces": "metadata_done",
    "embeddings": "faces_done",
    "assignment": "embeddings_done",
}

STAGE_DONE_STATUS: dict[AssetStage, AssetStatus] = {
    "metadata": "metadata_done",
    "faces": "faces_done",
    "embeddings": "embeddings_done",
    "assignment": "assignment_done",
}


class AssetPipelineError(ValueError):
    pass


def ensure_stage(stage: str) -> AssetStage:
    if stage not in STAGE_ORDER:
        raise AssetPipelineError(f"不支持的阶段: {stage}")
    return stage


def previous_status_for_stage(stage: AssetStage) -> AssetStatus:
    return STAGE_PREVIOUS_STATUS[stage]


def done_status_for_stage(stage: AssetStage) -> AssetStatus:
    return STAGE_DONE_STATUS[stage]


def statuses_at_or_above(status: AssetStatus) -> tuple[AssetStatus, ...]:
    start = STATUS_ORDER.index(status)
    return STATUS_ORDER[start:]


def classify_assignment_by_distance(
    distance: float,
    *,
    auto_assign_threshold: float = DEFAULT_AUTO_ASSIGN_THRESHOLD,
    review_threshold: float = DEFAULT_REVIEW_THRESHOLD,
) -> AssignmentDecision:
    if float(auto_assign_threshold) > float(review_threshold):
        raise AssetPipelineError("auto_assign_threshold 不能大于 review_threshold")
    if float(distance) <= float(auto_assign_threshold):
        return "auto_assign"
    if float(distance) <= float(review_threshold):
        return "review"
    return "new_person_candidate"
