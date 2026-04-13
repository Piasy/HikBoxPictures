from __future__ import annotations

from pathlib import Path

import pytest

from hikbox_pictures.ann import AnnIndexStore
from hikbox_pictures.services.ann_assignment_service import AnnAssignmentService


def test_threshold_layers_route_to_auto_review_or_new_person(tmp_path: Path) -> None:
    service = AnnAssignmentService(
        AnnIndexStore(tmp_path / "prototype_index.npz"),
        auto_assign_threshold=0.25,
        review_threshold=0.35,
    )

    assert service.classify_distance(0.21) == "auto_assign"
    assert service.classify_distance(0.31) == "review"
    assert service.classify_distance(0.45) == "new_person_candidate"


def test_threshold_layers_reject_invalid_configuration(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="不能大于"):
        AnnAssignmentService(
            AnnIndexStore(tmp_path / "prototype_index.npz"),
            auto_assign_threshold=0.36,
            review_threshold=0.35,
        )
