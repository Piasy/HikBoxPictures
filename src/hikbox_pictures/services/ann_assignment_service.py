from __future__ import annotations

from typing import Sequence

import numpy as np

from hikbox_pictures.ann import AnnIndexStore
from hikbox_pictures.services.asset_pipeline import (
    DEFAULT_AUTO_ASSIGN_THRESHOLD,
    DEFAULT_REVIEW_THRESHOLD,
    AssignmentDecision,
    classify_assignment_by_distance,
)


class AnnAssignmentService:
    def __init__(
        self,
        ann_index_store: AnnIndexStore,
        *,
        auto_assign_threshold: float = DEFAULT_AUTO_ASSIGN_THRESHOLD,
        review_threshold: float = DEFAULT_REVIEW_THRESHOLD,
    ) -> None:
        if float(auto_assign_threshold) > float(review_threshold):
            raise ValueError("auto_assign_threshold 不能大于 review_threshold")
        self.ann_index_store = ann_index_store
        self.auto_assign_threshold = float(auto_assign_threshold)
        self.review_threshold = float(review_threshold)

    def recall_person_candidates(
        self,
        observation_embedding: Sequence[float] | np.ndarray,
        *,
        top_k: int = 5,
    ) -> list[dict[str, float | int]]:
        target = max(0, int(top_k))
        if target == 0:
            return []

        # 同一 person 可能存在多个 prototype，采用逐步扩窗避免唯一 person 不足。
        search_limit = min(max(target, 1), self.ann_index_store.size)
        best_distance_by_person: dict[int, float] = {}
        while search_limit > 0:
            raw = self.ann_index_store.search(observation_embedding, search_limit)
            for person_id, distance in raw:
                current = best_distance_by_person.get(int(person_id))
                if current is None or float(distance) < current:
                    best_distance_by_person[int(person_id)] = float(distance)

            if len(best_distance_by_person) >= target or search_limit >= self.ann_index_store.size:
                break
            next_limit = min(self.ann_index_store.size, max(search_limit * 2, search_limit + 1))
            if next_limit == search_limit:
                break
            search_limit = next_limit

        ordered = sorted(best_distance_by_person.items(), key=lambda item: (item[1], item[0]))
        return [
            {"person_id": int(person_id), "distance": float(distance)}
            for person_id, distance in ordered[:target]
        ]

    def classify_distance(self, distance: float) -> AssignmentDecision:
        return classify_assignment_by_distance(
            float(distance),
            auto_assign_threshold=self.auto_assign_threshold,
            review_threshold=self.review_threshold,
        )
