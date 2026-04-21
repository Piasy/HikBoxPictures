from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable

from .param_snapshot import (
    AHC_PASS_2_TIE_BREAK,
    FROZEN_V5_STAGE_SEQUENCE,
    IGNORED_ASSIGNMENT_SOURCES,
    LATE_FUSION_MISSING_SIMILARITY,
    PERSON_CONSENSUS_SIMILARITY_THRESHOLD,
    UNKNOWN_ASSIGNMENT_SOURCE_FALLBACK,
)

ALLOWED_ASSIGNMENT_SOURCES = {
    "hdbscan",
    "person_consensus",
    "recall",
    "merge",
    "undo",
    "noise",
    "low_quality_ignored",
}


@dataclass(frozen=True)
class FrozenV5AssignmentCandidate:
    face_observation_id: int
    person_id: int | None
    assignment_source: str
    sim_main: float
    sim_flip: float
    similarity: float


class FrozenV5Executor:
    def __init__(self) -> None:
        self._stage_handlers = {
            "ahc_pass_1": self._run_ahc_pass_1,
            "ahc_pass_2": self._run_ahc_pass_2,
            "person_consensus": self._run_person_consensus,
            "person_cluster_recall": self._run_person_cluster_recall,
        }

    def execute(
        self,
        candidates: Iterable[dict[str, object]],
    ) -> list[FrozenV5AssignmentCandidate]:
        stage_rows = [dict(row) for row in candidates]
        for stage_name in FROZEN_V5_STAGE_SEQUENCE:
            handler = self._stage_handlers[stage_name]
            stage_rows = handler(stage_rows)

        result: list[FrozenV5AssignmentCandidate] = []
        for row in stage_rows:
            sim_main = _coerce_similarity(row.get("sim_main"))
            sim_flip = _coerce_similarity(row.get("sim_flip"))
            similarity = float(max(sim_main, sim_flip))
            assignment_source = self._normalize_assignment_source(
                str(row.get("assignment_source", UNKNOWN_ASSIGNMENT_SOURCE_FALLBACK))
            )
            face_observation_id = _coerce_strict_int(row.get("face_observation_id"), field_name="face_observation_id")
            person_id = _coerce_person_id(
                row.get("person_id"),
                allow_none=assignment_source in set(IGNORED_ASSIGNMENT_SOURCES),
                assignment_source=assignment_source,
            )
            result.append(
                FrozenV5AssignmentCandidate(
                    face_observation_id=face_observation_id,
                    person_id=person_id,
                    assignment_source=assignment_source,
                    sim_main=sim_main,
                    sim_flip=sim_flip,
                    similarity=similarity,
                )
            )
        return result

    def _run_ahc_pass_1(self, rows: list[dict[str, object]]) -> list[dict[str, object]]:
        normalized_rows: list[dict[str, object]] = []
        for row in rows:
            normalized = dict(row)
            sim_main = _coerce_similarity(normalized.get("sim_main"))
            sim_flip = _coerce_similarity(normalized.get("sim_flip"))
            normalized["sim_main"] = sim_main
            normalized["sim_flip"] = sim_flip
            normalized["similarity"] = float(max(sim_main, sim_flip))
            normalized_rows.append(normalized)
        return normalized_rows

    def _run_ahc_pass_2(self, rows: list[dict[str, object]]) -> list[dict[str, object]]:
        best_by_face: dict[int, dict[str, object]] = {}
        for row in rows:
            face_id = _coerce_strict_int(row.get("face_observation_id"), field_name="face_observation_id")
            current = best_by_face.get(face_id)
            if current is None:
                best_by_face[face_id] = row
                continue

            if _tie_break_key(row) < _tie_break_key(current):
                best_by_face[face_id] = row
        ordered_face_ids = sorted(best_by_face.keys())
        return [best_by_face[face_id] for face_id in ordered_face_ids]

    def _run_person_consensus(self, rows: list[dict[str, object]]) -> list[dict[str, object]]:
        updated_rows: list[dict[str, object]] = []
        for row in rows:
            updated = dict(row)
            source = str(updated.get("assignment_source", "hdbscan"))
            similarity = _coerce_similarity(updated.get("similarity"))
            if source == "person_consensus_candidate":
                if similarity >= PERSON_CONSENSUS_SIMILARITY_THRESHOLD:
                    updated["assignment_source"] = "person_consensus"
                else:
                    updated["assignment_source"] = "hdbscan"
            updated_rows.append(updated)
        return updated_rows

    def _run_person_cluster_recall(self, rows: list[dict[str, object]]) -> list[dict[str, object]]:
        updated_rows: list[dict[str, object]] = []
        for row in rows:
            updated = dict(row)
            source = str(updated.get("assignment_source", UNKNOWN_ASSIGNMENT_SOURCE_FALLBACK))
            if source in {"person_cluster_recall", "recall_candidate"}:
                updated["assignment_source"] = "person_cluster_recall"
            updated_rows.append(updated)
        return updated_rows

    def _normalize_assignment_source(self, source: str) -> str:
        if source in {"person_cluster_recall", "recall_candidate"}:
            return "recall"
        if source in ALLOWED_ASSIGNMENT_SOURCES:
            return source
        return UNKNOWN_ASSIGNMENT_SOURCE_FALLBACK


def _coerce_similarity(value: object) -> float:
    if value is None:
        return float(LATE_FUSION_MISSING_SIMILARITY)
    similarity = float(value)
    if not math.isfinite(similarity):
        raise ValueError(f"similarity 非法: {value!r}")
    return similarity


def _coerce_person_id(value: object, *, allow_none: bool, assignment_source: str) -> int | None:
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"assignment_source={assignment_source} 缺少 person_id")
    return _coerce_strict_int(value, field_name=f"assignment_source={assignment_source} 的 person_id")


def _tie_break_key(row: dict[str, object]) -> tuple[float, int, str]:
    # 二阶段去重规则：相似度降序、person_id 升序、来源字典序升序
    _ = AHC_PASS_2_TIE_BREAK
    similarity = _coerce_similarity(row.get("similarity"))
    source = str(row.get("assignment_source", UNKNOWN_ASSIGNMENT_SOURCE_FALLBACK))
    person_id = _coerce_person_id(
        row.get("person_id"),
        allow_none=True,
        assignment_source=source,
    )
    person_sort = person_id if person_id is not None else 10**18
    return (-similarity, person_sort, source)


def _coerce_strict_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} 非法: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise ValueError(f"{field_name} 非法: {value!r}")
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and (stripped.isdigit() or (stripped[0] in "+-" and stripped[1:].isdigit())):
            return int(stripped)
        raise ValueError(f"{field_name} 非法: {value!r}")
    raise ValueError(f"{field_name} 非法: {value!r}")
