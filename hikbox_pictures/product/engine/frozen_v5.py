"""冻结 v5 归属链路（embed/cluster/assignment）。"""

from __future__ import annotations

from collections import defaultdict

import numpy as np

from hikbox_pictures.face_review_pipeline import (
    _cluster_with_hdbscan,
    attach_micro_clusters_to_existing_persons,
    attach_noise_faces_to_person_consensus,
    demote_low_quality_micro_clusters,
    exclude_low_quality_faces_from_assignment,
    group_faces_by_cluster,
    merge_clusters_to_persons,
)


def late_fusion_similarity(*, sim_main: float, sim_flip: float | None) -> float:
    """晚融合固定语义：max(main, flip)。"""
    if sim_flip is None:
        return float(sim_main)
    return float(max(sim_main, sim_flip))


def run_frozen_v5_assignment(*, faces: list[dict[str, object]], params: dict[str, object]) -> dict[str, object]:
    if not faces:
        return {
            "faces": [],
            "persons": [],
            "stats": {
                "person_count": 0,
                "assignment_count": 0,
                "person_cluster_recall_attach_count": 0,
            },
        }

    vectors = np.asarray([row["embedding_main"] for row in faces], dtype=np.float32)
    labels, probabilities = _cluster_with_hdbscan(
        vectors,
        min_cluster_size=int(params["min_cluster_size"]),
        min_samples=int(params["min_samples"]),
    )

    quality_rows = [{"quality_score": float(row["quality_score"])} for row in faces]
    labels, probabilities, excluded_flags, _ = exclude_low_quality_faces_from_assignment(
        faces=quality_rows,
        labels=labels,
        probabilities=probabilities,
        min_quality_score=float(params["face_min_quality_for_assignment"]),
    )
    labels, probabilities, _, _ = demote_low_quality_micro_clusters(
        faces=quality_rows,
        labels=labels,
        probabilities=probabilities,
        max_cluster_size=int(params["low_quality_micro_cluster_max_size"]),
        top2_weight=float(params["low_quality_micro_cluster_top2_weight"]),
        min_quality_evidence=float(params["low_quality_micro_cluster_min_quality_evidence"]),
    )

    feature_faces = []
    for row, excluded in zip(faces, excluded_flags, strict=True):
        feature_faces.append(
            {
                "face_id": str(row["face_observation_id"]),
                "photo_relpath": str(row["photo_relpath"]),
                "quality_score": float(row["quality_score"]),
                "embedding": row["embedding_main"],
                "embedding_flip": row.get("embedding_flip"),
                "quality_gate_excluded": bool(excluded),
            }
        )

    preliminary_rows = []
    for face, label, prob in zip(feature_faces, labels, probabilities, strict=True):
        item = dict(face)
        item["cluster_label"] = int(label)
        item["cluster_probability"] = None if prob is None else float(prob)
        preliminary_rows.append(item)

    preliminary_clusters = group_faces_by_cluster(preliminary_rows, labels=[int(v) for v in labels])
    preliminary_persons = merge_clusters_to_persons(
        clusters=preliminary_clusters,
        distance_threshold=float(params["person_merge_threshold"]),
        rep_top_k=int(params["person_rep_top_k"]),
        knn_k=int(params["person_knn_k"]),
        linkage=str(params["person_linkage"]),
        enable_same_photo_cannot_link=bool(params["person_enable_same_photo_cannot_link"]),
    )

    labels, probabilities, _ = attach_noise_faces_to_person_consensus(
        faces=preliminary_rows,
        labels=labels,
        probabilities=probabilities,
        persons=preliminary_persons,
        rep_top_k=int(params["person_consensus_rep_top_k"]),
        distance_threshold=float(params["person_consensus_distance_threshold"]),
        margin_threshold=float(params["person_consensus_margin_threshold"]),
    )

    grouped_clusters = group_faces_by_cluster(preliminary_rows, labels=[int(v) for v in labels])
    persons = merge_clusters_to_persons(
        clusters=grouped_clusters,
        distance_threshold=float(params["person_merge_threshold"]),
        rep_top_k=int(params["person_rep_top_k"]),
        knn_k=int(params["person_knn_k"]),
        linkage=str(params["person_linkage"]),
        enable_same_photo_cannot_link=bool(params["person_enable_same_photo_cannot_link"]),
    )

    persons, _, recall_attach_count = attach_micro_clusters_to_existing_persons(
        persons=persons,
        source_max_cluster_size=int(params["person_cluster_recall_source_max_cluster_size"]),
        source_max_person_face_count=int(params["person_cluster_recall_source_max_person_faces"]),
        target_min_person_face_count=int(params["person_cluster_recall_target_min_person_faces"]),
        knn_top_n=int(params["person_cluster_recall_top_n"]),
        min_votes=int(params["person_cluster_recall_min_votes"]),
        distance_threshold=float(params["person_cluster_recall_distance_threshold"]),
        margin_threshold=float(params["person_cluster_recall_margin_threshold"]),
        max_rounds=int(params["person_cluster_recall_max_rounds"]),
    )

    cluster_owner: dict[int, str] = {}
    person_faces: dict[str, list[int]] = defaultdict(list)
    for person in persons:
        person_temp_key = f"p{int(person.get('person_label', 0))}"
        for cluster in person.get("clusters", []):
            cluster_label = int(cluster.get("cluster_label", -1))
            if cluster_label != -1:
                cluster_owner[cluster_label] = person_temp_key
            for member in cluster.get("members", []):
                face_id = int(member.get("face_id", 0))
                if face_id <= 0:
                    continue
                person_faces[person_temp_key].append(face_id)

    result_faces: list[dict[str, object]] = []
    for face, label, prob, excluded in zip(faces, labels, probabilities, excluded_flags, strict=True):
        cluster_label = int(label)
        if cluster_label == -1:
            assignment_source = "low_quality_ignored" if bool(excluded) else "noise"
            person_temp_key = None
        elif prob is None:
            assignment_source = "person_consensus"
            person_temp_key = cluster_owner.get(cluster_label)
        else:
            assignment_source = "hdbscan"
            person_temp_key = cluster_owner.get(cluster_label)

        result_faces.append(
            {
                "face_observation_id": int(face["face_observation_id"]),
                "cluster_label": cluster_label,
                "person_temp_key": person_temp_key,
                "assignment_source": assignment_source,
                "probability": None if prob is None else float(prob),
            }
        )

    result_persons = []
    for person_temp_key, observation_ids in sorted(person_faces.items(), key=lambda item: item[0]):
        result_persons.append(
            {
                "person_temp_key": person_temp_key,
                "face_observation_ids": sorted(set(int(v) for v in observation_ids if int(v) > 0)),
            }
        )

    assignment_count = sum(
        1
        for row in result_faces
        if row["person_temp_key"] is not None and str(row["assignment_source"]) in {"hdbscan", "person_consensus"}
    )
    return {
        "faces": result_faces,
        "persons": result_persons,
        "stats": {
            "person_count": len(result_persons),
            "assignment_count": int(assignment_count),
            "person_cluster_recall_attach_count": int(recall_attach_count),
        },
    }
