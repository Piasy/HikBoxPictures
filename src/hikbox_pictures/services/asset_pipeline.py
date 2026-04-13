from __future__ import annotations

from typing import Literal

AssetStage = Literal["metadata", "faces", "embeddings", "assignment"]
AssetStatus = Literal[
    "discovered",
    "metadata_done",
    "faces_done",
    "embeddings_done",
    "assignment_done",
]

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
