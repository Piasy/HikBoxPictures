"""冻结 v5 语义原语。"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import combinations
from typing import Any

import numpy as np

ANN_BUILD_MIN_ITEMS = 2048
ANN_DEFAULT_QUERY_K = 64
ANN_CANDIDATE_PRUNE_MIN_ITEMS = 64


def _coerce_embedding_vector(raw_embedding: Any) -> np.ndarray | None:
    if raw_embedding is None:
        return None
    if isinstance(raw_embedding, np.ndarray):
        vector = raw_embedding.astype(np.float32, copy=False)
    elif isinstance(raw_embedding, (list, tuple)) and raw_embedding:
        vector = np.asarray(raw_embedding, dtype=np.float32)
    else:
        return None
    if vector.ndim != 1 or int(vector.shape[0]) <= 0:
        return None
    return vector

def group_faces_by_cluster(faces: list[dict[str, Any]], labels: list[int]) -> list[dict[str, Any]]:
    if len(faces) != len(labels):
        raise ValueError("faces 与 labels 数量不一致")

    buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for face, label in zip(faces, labels, strict=True):
        buckets[int(label)].append(face)

    normal_labels = [label for label in buckets if label != -1]
    normal_labels.sort(key=lambda label: (-len(buckets[label]), label))

    grouped: list[dict[str, Any]] = []
    for label in normal_labels:
        grouped.append(
            {
                "cluster_key": f"cluster_{label}",
                "cluster_label": label,
                "members": buckets[label],
            }
        )

    if -1 in buckets:
        grouped.append(
            {
                "cluster_key": "noise",
                "cluster_label": -1,
                "members": buckets[-1],
            }
        )

    return grouped


def _normalize_embedding(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-9:
        return vector.astype(np.float32)
    return (vector / norm).astype(np.float32)


def _build_cluster_representative(
    cluster: dict[str, Any],
    rep_top_k: int,
    embedding_key: str = "embedding",
) -> np.ndarray | None:
    members = list(cluster.get("members", []))
    if not members:
        return None

    sorted_members = sorted(members, key=lambda row: -(row.get("quality_score") or 0.0))
    top_k = max(1, int(rep_top_k))

    vectors: list[np.ndarray] = []
    weights: list[float] = []
    dim: int | None = None
    for member in sorted_members:
        vector = _coerce_embedding_vector(member.get(embedding_key))
        if vector is None:
            continue
        if dim is None:
            dim = int(vector.shape[0])
        if int(vector.shape[0]) != dim:
            continue
        vectors.append(_normalize_embedding(vector))
        weights.append(max(float(member.get("quality_score") or 0.0), 1e-3))
        if len(vectors) >= top_k:
            break

    if not vectors:
        return None

    vectors_np = np.stack(vectors, axis=0)
    weights_np = np.asarray(weights, dtype=np.float32)
    weighted_sum = np.sum(vectors_np * weights_np[:, None], axis=0)
    return _normalize_embedding(weighted_sum)


def attach_noise_faces_to_person_consensus(
    faces: list[dict[str, Any]],
    labels: list[int],
    probabilities: list[float | None],
    persons: list[dict[str, Any]],
    rep_top_k: int,
    distance_threshold: float,
    margin_threshold: float,
) -> tuple[list[int], list[float | None], int]:
    if len(faces) != len(labels) or len(faces) != len(probabilities):
        raise ValueError("faces、labels、probabilities 数量不一致")

    if distance_threshold <= 0:
        return list(labels), list(probabilities), 0

    if not persons:
        return list(labels), list(probabilities), 0

    grouped_clusters = group_faces_by_cluster(faces=faces, labels=labels)
    normal_clusters = [cluster for cluster in grouped_clusters if int(cluster.get("cluster_label", -1)) != -1]
    if not normal_clusters:
        return list(labels), list(probabilities), 0

    cluster_representatives: dict[int, np.ndarray] = {}
    cluster_representatives_flip: dict[int, np.ndarray] = {}
    for cluster in normal_clusters:
        representative = _build_cluster_representative(
            cluster,
            rep_top_k=rep_top_k,
            embedding_key="embedding",
        )
        if representative is None:
            representative = None
        representative_flip = _build_cluster_representative(
            cluster,
            rep_top_k=rep_top_k,
            embedding_key="embedding_flip",
        )
        cluster_label = int(cluster.get("cluster_label", -1))
        if representative is not None:
            cluster_representatives[cluster_label] = representative
        if representative_flip is not None:
            cluster_representatives_flip[cluster_label] = representative_flip

    if not cluster_representatives and not cluster_representatives_flip:
        return list(labels), list(probabilities), 0

    person_cluster_labels: list[list[int]] = []
    person_idx_by_cluster_label: dict[int, int] = {}
    for person in persons:
        candidate_labels: list[int] = []
        for cluster_ref in person.get("clusters", []):
            label = int(cluster_ref.get("cluster_label", -1))
            if label in cluster_representatives or label in cluster_representatives_flip:
                candidate_labels.append(label)
        if candidate_labels:
            person_idx = len(person_cluster_labels)
            person_cluster_labels.append(candidate_labels)
            for label in candidate_labels:
                person_idx_by_cluster_label[label] = person_idx

    if not person_cluster_labels:
        return list(labels), list(probabilities), 0

    rep_main_labels = sorted(cluster_representatives.keys())
    rep_flip_labels = sorted(cluster_representatives_flip.keys())
    rep_main_index = (
        _build_vector_search_index(
            vectors=np.stack([cluster_representatives[label] for label in rep_main_labels], axis=0),
            prefer_ann=True,
        )
        if rep_main_labels
        else None
    )
    rep_flip_index = (
        _build_vector_search_index(
            vectors=np.stack([cluster_representatives_flip[label] for label in rep_flip_labels], axis=0),
            prefer_ann=True,
        )
        if rep_flip_labels
        else None
    )
    use_candidate_pruning = max(len(rep_main_labels), len(rep_flip_labels)) >= ANN_CANDIDATE_PRUNE_MIN_ITEMS

    updated_labels = list(labels)
    updated_probabilities = list(probabilities)
    attached_count = 0

    def candidate_cluster_labels_for_face(
        vector_main: np.ndarray,
        vector_flip: np.ndarray | None,
    ) -> list[list[int]]:
        if not use_candidate_pruning:
            return person_cluster_labels

        candidate_person_indices: set[int] = set()
        query_k_main = max(ANN_DEFAULT_QUERY_K, int(rep_top_k) * 8)
        query_k_flip = max(ANN_DEFAULT_QUERY_K, int(rep_top_k) * 8)

        if rep_main_index is not None and rep_main_labels:
            top_k = min(int(query_k_main), len(rep_main_labels))
            main_indices, _ = _query_vector_search_index(rep_main_index, vector_main[None, :], top_k=top_k)
            if main_indices.size > 0:
                for pos in main_indices[0].tolist():
                    cluster_label = int(rep_main_labels[int(pos)])
                    owner = person_idx_by_cluster_label.get(cluster_label)
                    if owner is not None:
                        candidate_person_indices.add(int(owner))

        if rep_flip_index is not None and rep_flip_labels and vector_flip is not None:
            top_k = min(int(query_k_flip), len(rep_flip_labels))
            flip_indices, _ = _query_vector_search_index(rep_flip_index, vector_flip[None, :], top_k=top_k)
            if flip_indices.size > 0:
                for pos in flip_indices[0].tolist():
                    cluster_label = int(rep_flip_labels[int(pos)])
                    owner = person_idx_by_cluster_label.get(cluster_label)
                    if owner is not None:
                        candidate_person_indices.add(int(owner))

        # 需要至少两个候选人物，否则 margin 语义不稳定，回退全量比较。
        if len(candidate_person_indices) < 2:
            return person_cluster_labels
        return [person_cluster_labels[idx] for idx in sorted(candidate_person_indices)]

    def build_person_candidates(
        cluster_labels_by_person: list[list[int]],
        vector_main: np.ndarray,
        vector_flip: np.ndarray | None,
        use_flip_supplement: bool,
    ) -> list[tuple[float, float, int]]:
        person_candidates: list[tuple[float, float, int]] = []
        for cluster_labels in cluster_labels_by_person:
            best_label: int | None = None
            best_sim: float | None = None
            for cluster_label in cluster_labels:
                main_sim: float | None = None
                rep_main = cluster_representatives.get(cluster_label)
                if rep_main is not None:
                    main_sim = float(np.clip(np.dot(rep_main, vector_main), -1.0, 1.0))

                flip_sim: float | None = None
                if use_flip_supplement and vector_flip is not None:
                    rep_flip = cluster_representatives_flip.get(cluster_label)
                    if rep_flip is not None:
                        flip_sim = float(np.clip(np.dot(rep_flip, vector_flip), -1.0, 1.0))

                if main_sim is None and flip_sim is None:
                    continue
                if main_sim is None:
                    current_sim = float(flip_sim)  # type: ignore[arg-type]
                elif flip_sim is None:
                    current_sim = float(main_sim)
                else:
                    current_sim = float(max(main_sim, flip_sim))

                if best_sim is None or current_sim > best_sim:
                    best_sim = current_sim
                    best_label = cluster_label

            if best_label is None or best_sim is None:
                continue
            person_candidates.append((1.0 - best_sim, best_sim, best_label))

        person_candidates.sort(key=lambda item: item[0])
        return person_candidates

    def evaluate_pass(candidates: list[tuple[float, float, int]]) -> tuple[bool, int, float | None, float | None]:
        if not candidates:
            return False, -1, None, None
        best_dist, best_sim, best_label = candidates[0]
        second_sim = candidates[1][1] if len(candidates) >= 2 else -1.0
        margin = best_sim - second_sim
        if best_dist > float(distance_threshold):
            return False, -1, None, None
        if margin < float(margin_threshold):
            return False, -1, None, None
        return True, int(best_label), float(best_sim), float(margin)

    for idx, (face, label) in enumerate(zip(faces, labels, strict=True)):
        if int(label) != -1:
            continue
        if bool(face.get("quality_gate_excluded", False)):
            continue

        vector = _coerce_embedding_vector(face.get("embedding"))
        if vector is None:
            continue
        vector = _normalize_embedding(vector)

        vector_flip: np.ndarray | None = None
        flip = _coerce_embedding_vector(face.get("embedding_flip"))
        if flip is not None:
            vector_flip = _normalize_embedding(flip)

        candidate_cluster_labels = candidate_cluster_labels_for_face(vector_main=vector, vector_flip=vector_flip)

        main_candidates = build_person_candidates(
            cluster_labels_by_person=candidate_cluster_labels,
            vector_main=vector,
            vector_flip=None,
            use_flip_supplement=False,
        )
        supplement_candidates = build_person_candidates(
            cluster_labels_by_person=candidate_cluster_labels,
            vector_main=vector,
            vector_flip=vector_flip,
            use_flip_supplement=True,
        )

        main_pass, main_label, main_confidence, main_margin = evaluate_pass(main_candidates)
        supplement_pass, supplement_label, supplement_confidence, supplement_margin = evaluate_pass(supplement_candidates)

        if main_pass:
            final_label = main_label
            final_confidence = main_confidence
            final_margin = main_margin
        elif supplement_pass:
            final_label = supplement_label
            final_confidence = supplement_confidence
            final_margin = supplement_margin
        else:
            continue

        updated_labels[idx] = final_label
        # 这里的回挂置信不是 HDBSCAN 原生 probability，review 中留空避免误解。
        updated_probabilities[idx] = None
        faces[idx]["assignment_confidence"] = None if final_confidence is None else float(final_confidence)
        faces[idx]["assignment_margin"] = None if final_margin is None else float(final_margin)
        attached_count += 1

    return updated_labels, updated_probabilities, attached_count


def exclude_low_quality_faces_from_assignment(
    faces: list[dict[str, Any]],
    labels: list[int],
    probabilities: list[float | None],
    min_quality_score: float | None,
) -> tuple[list[int], list[float | None], list[bool], int]:
    if len(faces) != len(labels) or len(faces) != len(probabilities):
        raise ValueError("faces、labels、probabilities 数量不一致")

    if min_quality_score is None or float(min_quality_score) <= 0:
        return list(labels), list(probabilities), [False for _ in faces], 0

    updated_labels = list(labels)
    updated_probabilities = list(probabilities)
    excluded_flags = [False for _ in faces]
    excluded_count = 0
    threshold = float(min_quality_score)

    for idx, face in enumerate(faces):
        quality_score = float(face.get("quality_score") or 0.0)
        if quality_score >= threshold:
            continue
        excluded_flags[idx] = True
        excluded_count += 1
        updated_labels[idx] = -1
        updated_probabilities[idx] = 0.0

    return updated_labels, updated_probabilities, excluded_flags, excluded_count


def demote_low_quality_micro_clusters(
    faces: list[dict[str, Any]],
    labels: list[int],
    probabilities: list[float | None],
    max_cluster_size: int,
    top2_weight: float,
    min_quality_evidence: float,
) -> tuple[list[int], list[float | None], int, int]:
    if len(faces) != len(labels) or len(faces) != len(probabilities):
        raise ValueError("faces、labels、probabilities 数量不一致")

    if int(max_cluster_size) <= 0 or float(min_quality_evidence) <= 0:
        return list(labels), list(probabilities), 0, 0

    safe_top2_weight = max(0.0, float(top2_weight))
    updated_labels = list(labels)
    updated_probabilities = list(probabilities)
    demoted_cluster_count = 0
    demoted_face_count = 0

    cluster_to_indices: dict[int, list[int]] = defaultdict(list)
    for idx, label in enumerate(labels):
        cluster_label = int(label)
        if cluster_label == -1:
            continue
        cluster_to_indices[cluster_label].append(idx)

    for indices in cluster_to_indices.values():
        if len(indices) > int(max_cluster_size):
            continue

        quality_values = sorted(
            [float(faces[idx].get("quality_score") or 0.0) for idx in indices],
            reverse=True,
        )
        if not quality_values:
            continue

        top1 = quality_values[0]
        top2 = quality_values[1] if len(quality_values) >= 2 else 0.0
        quality_evidence = top1 + safe_top2_weight * top2
        if quality_evidence >= float(min_quality_evidence):
            continue

        demoted_cluster_count += 1
        demoted_face_count += len(indices)
        for idx in indices:
            updated_labels[idx] = -1
            updated_probabilities[idx] = 0.0

    return updated_labels, updated_probabilities, demoted_cluster_count, demoted_face_count


def _pairwise_cosine_distance(vectors: np.ndarray) -> np.ndarray:
    sims = np.clip(vectors @ vectors.T, -1.0, 1.0)
    dist = 1.0 - sims
    dist = np.maximum(dist, 0.0)
    np.fill_diagonal(dist, 0.0)
    return dist.astype(np.float32)


@dataclass
class _VectorSearchIndex:
    vectors: np.ndarray
    hnsw_index: Any | None = None


def _build_vector_search_index(vectors: np.ndarray, prefer_ann: bool = True) -> _VectorSearchIndex:
    safe_vectors = np.asarray(vectors, dtype=np.float32)
    if safe_vectors.ndim != 2 or safe_vectors.shape[0] <= 0:
        return _VectorSearchIndex(vectors=np.zeros((0, 0), dtype=np.float32), hnsw_index=None)

    hnsw_index: Any | None = None
    if prefer_ann and safe_vectors.shape[0] >= ANN_BUILD_MIN_ITEMS:
        try:
            import hnswlib

            dim = int(safe_vectors.shape[1])
            index = hnswlib.Index(space="cosine", dim=dim)
            index.init_index(max_elements=int(safe_vectors.shape[0]), ef_construction=200, M=16)
            index.add_items(safe_vectors, np.arange(int(safe_vectors.shape[0]), dtype=np.int32))
            index.set_ef(max(64, min(512, ANN_DEFAULT_QUERY_K * 4)))
            hnsw_index = index
        except Exception:
            hnsw_index = None

    return _VectorSearchIndex(vectors=safe_vectors, hnsw_index=hnsw_index)


def _query_vector_search_index(
    index: _VectorSearchIndex,
    queries: np.ndarray,
    top_k: int,
) -> tuple[np.ndarray, np.ndarray]:
    if index.vectors.ndim != 2 or index.vectors.shape[0] <= 0:
        return np.zeros((0, 0), dtype=np.int32), np.zeros((0, 0), dtype=np.float32)

    query_matrix = np.asarray(queries, dtype=np.float32)
    if query_matrix.ndim == 1:
        query_matrix = query_matrix[None, :]
    if query_matrix.ndim != 2 or query_matrix.shape[0] <= 0:
        return np.zeros((0, 0), dtype=np.int32), np.zeros((0, 0), dtype=np.float32)

    effective_k = max(1, min(int(top_k), int(index.vectors.shape[0])))
    if index.hnsw_index is not None:
        try:
            labels, distances = index.hnsw_index.knn_query(query_matrix, k=effective_k)
            return labels.astype(np.int32), distances.astype(np.float32)
        except Exception:
            pass

    sims = np.clip(query_matrix @ index.vectors.T, -1.0, 1.0)
    order = np.argpartition(-sims, kth=effective_k - 1, axis=1)[:, :effective_k]
    picked_sims = np.take_along_axis(sims, order, axis=1)
    picked_order = np.argsort(-picked_sims, axis=1)
    sorted_indices = np.take_along_axis(order, picked_order, axis=1).astype(np.int32)
    sorted_sims = np.take_along_axis(picked_sims, picked_order, axis=1)
    sorted_dist = np.maximum(1.0 - sorted_sims, 0.0).astype(np.float32)
    return sorted_indices, sorted_dist


def _build_cluster_knn_mask(dist_matrix: np.ndarray, knn_k: int) -> np.ndarray:
    size = int(dist_matrix.shape[0])
    mask = np.zeros((size, size), dtype=bool)
    if size <= 0:
        return mask

    np.fill_diagonal(mask, True)
    if size == 1:
        return mask

    effective_k = max(1, min(int(knn_k), size - 1))
    for row_idx in range(size):
        order = np.argsort(dist_matrix[row_idx]).tolist()
        neighbors = [col for col in order if col != row_idx][:effective_k]
        for col_idx in neighbors:
            mask[row_idx, col_idx] = True

    return np.logical_or(mask, mask.T)


def _build_cluster_cannot_link_pairs(clusters: list[dict[str, Any]]) -> set[tuple[int, int]]:
    if len(clusters) <= 1:
        return set()

    photo_to_cluster_indices: dict[str, list[int]] = defaultdict(list)
    for idx, cluster in enumerate(clusters):
        seen_photos: set[str] = set()
        for member in cluster.get("members", []):
            photo_relpath = str(member.get("photo_relpath", ""))
            if photo_relpath:
                seen_photos.add(photo_relpath)
        for photo in seen_photos:
            photo_to_cluster_indices[photo].append(idx)

    cannot_link_pairs: set[tuple[int, int]] = set()
    for indices in photo_to_cluster_indices.values():
        if len(indices) <= 1:
            continue
        sorted_indices = sorted(set(indices))
        for left, right in combinations(sorted_indices, 2):
            cannot_link_pairs.add((int(left), int(right)))
    return cannot_link_pairs


def _build_cluster_cannot_link_mask(clusters: list[dict[str, Any]]) -> np.ndarray:
    size = len(clusters)
    mask = np.zeros((size, size), dtype=bool)
    if size <= 1:
        return mask
    for left, right in _build_cluster_cannot_link_pairs(clusters):
        mask[left, right] = True
        mask[right, left] = True
    return mask


def _cluster_single_linkage_sparse(
    vectors: np.ndarray,
    clusters: list[dict[str, Any]],
    distance_threshold: float,
    knn_k: int,
    enable_same_photo_cannot_link: bool,
) -> list[int]:
    size = int(vectors.shape[0])
    if size <= 1:
        return [0]

    effective_k = max(1, min(int(knn_k), size - 1))
    # 需要 +1 保留 self，随后丢弃 self 项。
    neighbor_indices, _ = _query_vector_search_index(
        _build_vector_search_index(vectors=vectors, prefer_ann=True),
        queries=vectors,
        top_k=effective_k + 1,
    )

    cannot_link_pairs = _build_cluster_cannot_link_pairs(clusters) if enable_same_photo_cannot_link else set()

    parent = list(range(size))
    rank = [0 for _ in range(size)]

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        root_x = find(x)
        root_y = find(y)
        if root_x == root_y:
            return
        if rank[root_x] < rank[root_y]:
            parent[root_x] = root_y
        elif rank[root_x] > rank[root_y]:
            parent[root_y] = root_x
        else:
            parent[root_y] = root_x
            rank[root_x] += 1

    candidate_edges: set[tuple[int, int]] = set()
    for row_idx in range(size):
        row_neighbors = neighbor_indices[row_idx].tolist() if row_idx < neighbor_indices.shape[0] else []
        for col_idx in row_neighbors:
            col = int(col_idx)
            if col == row_idx:
                continue
            left, right = (row_idx, col) if row_idx < col else (col, row_idx)
            candidate_edges.add((left, right))

    threshold = float(distance_threshold)
    for left, right in candidate_edges:
        if (left, right) in cannot_link_pairs:
            continue
        sim = float(np.clip(np.dot(vectors[left], vectors[right]), -1.0, 1.0))
        dist = max(1.0 - sim, 0.0)
        if dist <= threshold:
            union(left, right)

    root_to_label: dict[int, int] = {}
    labels: list[int] = []
    next_label = 0
    for idx in range(size):
        root = find(idx)
        if root not in root_to_label:
            root_to_label[root] = next_label
            next_label += 1
        labels.append(root_to_label[root])
    return labels


def _cluster_by_ahc_distance_matrix(
    dist_matrix: np.ndarray,
    distance_threshold: float,
    linkage: str,
) -> list[int]:
    if dist_matrix.shape[0] <= 1:
        return [0]

    from sklearn.cluster import AgglomerativeClustering

    if linkage not in {"average", "single", "complete"}:
        raise ValueError(f"不支持的人物合并 linkage: {linkage}")

    # sklearn 在不同版本里参数名从 affinity 演进到 metric，这里做兼容。
    try:
        model = AgglomerativeClustering(
            n_clusters=None,
            metric="precomputed",
            linkage=linkage,
            distance_threshold=float(distance_threshold),
        )
    except TypeError:
        model = AgglomerativeClustering(
            n_clusters=None,
            affinity="precomputed",
            linkage=linkage,
            distance_threshold=float(distance_threshold),
        )
    return model.fit_predict(dist_matrix).astype(int).tolist()


def _build_person_uuid(cluster_refs: list[dict[str, Any]]) -> str:
    face_ids: list[str] = []
    for cluster in cluster_refs:
        for member in cluster.get("members", []):
            face_id = str(member.get("face_id", ""))
            if face_id:
                face_ids.append(face_id)

    if face_ids:
        seed = "|".join(sorted(face_ids))
    else:
        cluster_labels = [str(int(cluster.get("cluster_label", -1))) for cluster in cluster_refs]
        seed = "|".join(sorted(cluster_labels))
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
    return f"person_{digest}"


def _materialize_persons_from_cluster_buckets(buckets: dict[int, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    merged_persons: list[dict[str, Any]] = []
    for source_label, person_clusters in buckets.items():
        sorted_clusters = sorted(
            person_clusters,
            key=lambda row: (-len(row.get("members", [])), int(row.get("cluster_label", 0))),
        )
        cluster_refs: list[dict[str, Any]] = []
        person_face_count = 0
        for cluster in sorted_clusters:
            cluster_members = sorted(
                list(cluster.get("members", [])),
                key=lambda row: -(row.get("quality_score") or 0.0),
            )
            person_face_count += len(cluster_members)
            cluster_refs.append(
                {
                    "cluster_key": str(cluster.get("cluster_key", "")),
                    "cluster_label": int(cluster.get("cluster_label", -1)),
                    "member_count": len(cluster_members),
                    "members": cluster_members,
                }
            )

        if not cluster_refs:
            continue

        merged_persons.append(
            {
                "source_person_label": int(source_label),
                "clusters": cluster_refs,
                "person_face_count": person_face_count,
                "person_cluster_count": len(sorted_clusters),
                "min_cluster_label": min(item["cluster_label"] for item in cluster_refs),
            }
        )

    merged_persons.sort(
        key=lambda row: (
            -int(row.get("person_face_count", 0)),
            -int(row.get("person_cluster_count", 0)),
            int(row.get("min_cluster_label", 0)),
        )
    )

    for idx, person in enumerate(merged_persons):
        person["person_label"] = idx
        person["person_key"] = f"person_{idx}"
        person["person_uuid"] = _build_person_uuid(list(person.get("clusters", [])))
        person.pop("min_cluster_label", None)
        person.pop("source_person_label", None)

    return merged_persons


def merge_clusters_to_persons(
    clusters: list[dict[str, Any]],
    distance_threshold: float,
    rep_top_k: int,
    knn_k: int,
    linkage: str = "average",
    enable_same_photo_cannot_link: bool = False,
) -> list[dict[str, Any]]:
    normal_clusters = [cluster for cluster in clusters if int(cluster.get("cluster_label", -1)) != -1]
    if not normal_clusters:
        return []

    indexed_clusters: list[dict[str, Any]] = []
    representatives: list[np.ndarray] = []
    fallback_clusters: list[dict[str, Any]] = []
    for cluster in normal_clusters:
        representative = _build_cluster_representative(cluster, rep_top_k=rep_top_k)
        if representative is None:
            fallback_clusters.append(cluster)
            continue
        indexed_clusters.append(cluster)
        representatives.append(representative)

    if indexed_clusters:
        if len(indexed_clusters) == 1:
            ahc_labels = [0]
        else:
            vector_matrix = np.stack(representatives, axis=0)
            if linkage == "single":
                ahc_labels = _cluster_single_linkage_sparse(
                    vectors=vector_matrix,
                    clusters=indexed_clusters,
                    distance_threshold=float(distance_threshold),
                    knn_k=int(knn_k),
                    enable_same_photo_cannot_link=bool(enable_same_photo_cannot_link),
                )
            else:
                dist_matrix = _pairwise_cosine_distance(vector_matrix)
                mask = _build_cluster_knn_mask(dist_matrix, knn_k=knn_k)
                large_distance = max(float(distance_threshold) + 1.0, 2.0)
                constrained_dist = dist_matrix.copy()
                constrained_dist[~mask] = large_distance
                if enable_same_photo_cannot_link:
                    cannot_link_mask = _build_cluster_cannot_link_mask(indexed_clusters)
                    constrained_dist[cannot_link_mask] = large_distance
                np.fill_diagonal(constrained_dist, 0.0)
                ahc_labels = _cluster_by_ahc_distance_matrix(
                    constrained_dist,
                    distance_threshold=distance_threshold,
                    linkage=linkage,
                )
    else:
        ahc_labels = []

    buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for cluster, label in zip(indexed_clusters, ahc_labels, strict=True):
        buckets[int(label)].append(cluster)

    next_label = (max(buckets.keys()) + 1) if buckets else 0
    for cluster in fallback_clusters:
        buckets[next_label].append(cluster)
        next_label += 1

    return _materialize_persons_from_cluster_buckets(buckets)


def _collect_normalized_member_embeddings(cluster: dict[str, Any]) -> np.ndarray | None:
    vectors: list[np.ndarray] = []
    dim: int | None = None
    for member in cluster.get("members", []):
        vector = _coerce_embedding_vector(member.get("embedding"))
        if vector is None:
            continue
        if dim is None:
            dim = int(vector.shape[0])
        if int(vector.shape[0]) != dim:
            continue
        vectors.append(_normalize_embedding(vector))
    if not vectors:
        return None
    return np.stack(vectors, axis=0)


def _person_face_vector_matrix(person: dict[str, Any]) -> np.ndarray | None:
    vectors: list[np.ndarray] = []
    dim: int | None = None
    for cluster in person.get("clusters", []):
        matrix = _collect_normalized_member_embeddings(cluster)
        if matrix is None or matrix.size <= 0:
            continue
        if dim is None:
            dim = int(matrix.shape[1])
        if int(matrix.shape[1]) != dim:
            continue
        vectors.append(matrix)
    if not vectors:
        return None
    return np.concatenate(vectors, axis=0)


def attach_micro_clusters_to_existing_persons(
    persons: list[dict[str, Any]],
    source_max_cluster_size: int,
    source_max_person_face_count: int,
    target_min_person_face_count: int,
    knn_top_n: int,
    min_votes: int,
    distance_threshold: float,
    margin_threshold: float,
    max_rounds: int = 2,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    if not persons:
        return [], [], 0

    if (
        int(source_max_cluster_size) <= 0
        or int(source_max_person_face_count) <= 0
        or int(target_min_person_face_count) <= 0
        or int(knn_top_n) <= 0
        or int(min_votes) <= 0
        or float(distance_threshold) <= 0
        or int(max_rounds) <= 0
    ):
        return list(persons), [], 0

    clusters_by_label: dict[int, dict[str, Any]] = {}
    owner_by_cluster: dict[int, int] = {}
    for person in persons:
        person_label = int(person.get("person_label", -1))
        for cluster in person.get("clusters", []):
            cluster_label = int(cluster.get("cluster_label", -1))
            if cluster_label == -1:
                continue
            clusters_by_label[cluster_label] = cluster
            owner_by_cluster[cluster_label] = person_label

    if not clusters_by_label:
        return list(persons), [], 0

    moved_count = 0
    events: list[dict[str, Any]] = []

    for round_idx in range(1, int(max_rounds) + 1):
        buckets_by_owner: dict[int, list[int]] = defaultdict(list)
        for cluster_label, owner in owner_by_cluster.items():
            buckets_by_owner[int(owner)].append(cluster_label)

        person_face_count: dict[int, int] = {}
        for owner, cluster_labels in buckets_by_owner.items():
            person_face_count[int(owner)] = sum(len(clusters_by_label[label].get("members", [])) for label in cluster_labels)

        target_person_labels = [
            owner for owner, count in person_face_count.items() if int(count) >= int(target_min_person_face_count)
        ]
        if not target_person_labels:
            break

        target_vectors: dict[int, np.ndarray] = {}
        for owner in target_person_labels:
            person_payload = {
                "clusters": [clusters_by_label[label] for label in buckets_by_owner.get(owner, [])],
            }
            matrix = _person_face_vector_matrix(person_payload)
            if matrix is not None and matrix.size > 0:
                target_vectors[int(owner)] = matrix
        if not target_vectors:
            break

        target_anchor_vectors: dict[int, np.ndarray] = {}
        for owner, matrix in target_vectors.items():
            target_anchor_vectors[int(owner)] = _normalize_embedding(np.mean(matrix, axis=0))
        target_anchor_labels = sorted(target_anchor_vectors.keys())
        target_anchor_index = (
            _build_vector_search_index(
                vectors=np.stack([target_anchor_vectors[label] for label in target_anchor_labels], axis=0),
                prefer_ann=True,
            )
            if target_anchor_labels
            else None
        )
        use_target_candidate_pruning = len(target_anchor_labels) >= ANN_CANDIDATE_PRUNE_MIN_ITEMS

        candidate_cluster_labels: list[int] = []
        for cluster_label, owner in owner_by_cluster.items():
            cluster_size = len(clusters_by_label[cluster_label].get("members", []))
            owner_face_count = int(person_face_count.get(owner, 0))

            if owner_face_count >= int(target_min_person_face_count) and owner_face_count > int(source_max_person_face_count):
                continue
            if cluster_size > int(source_max_cluster_size) and owner_face_count > int(source_max_person_face_count):
                continue
            candidate_cluster_labels.append(cluster_label)

        candidate_cluster_labels.sort(
            key=lambda label: (
                -max(float(member.get("quality_score") or 0.0) for member in clusters_by_label[label].get("members", [{}])),
                label,
            )
        )

        moved_in_round = 0
        for cluster_label in candidate_cluster_labels:
            current_owner = int(owner_by_cluster.get(cluster_label, -1))
            if current_owner == -1:
                continue

            cluster_vectors = _collect_normalized_member_embeddings(clusters_by_label[cluster_label])
            if cluster_vectors is None or cluster_vectors.size <= 0:
                continue

            candidate_targets = [owner for owner in target_person_labels if owner != current_owner and owner in target_vectors]
            if not candidate_targets:
                continue
            if use_target_candidate_pruning and target_anchor_index is not None:
                cluster_anchor = _normalize_embedding(np.mean(cluster_vectors, axis=0))
                query_k = min(max(int(knn_top_n) * 4, ANN_DEFAULT_QUERY_K), len(target_anchor_labels))
                ann_indices, _ = _query_vector_search_index(target_anchor_index, cluster_anchor[None, :], top_k=query_k)
                ann_targets = [
                    int(target_anchor_labels[int(pos)])
                    for pos in ann_indices[0].tolist()
                    if int(target_anchor_labels[int(pos)]) != current_owner and int(target_anchor_labels[int(pos)]) in target_vectors
                ]
                # 至少两个候选目标时再裁剪，避免 margin 语义受损。
                if len(ann_targets) >= 2:
                    candidate_targets = sorted(set(ann_targets))

            person_scores: list[tuple[int, float, float, int]] = []
            global_pairs: list[tuple[float, int]] = []
            for target_owner in candidate_targets:
                matrix = target_vectors[target_owner]
                sims = np.clip(cluster_vectors @ matrix.T, -1.0, 1.0)
                dist = np.maximum(1.0 - sims, 0.0)
                flat = np.sort(dist.reshape(-1))
                if flat.size <= 0:
                    continue

                score_k = max(1, min(int(min_votes), int(flat.size)))
                score_mean = float(np.mean(flat[:score_k]))
                best_dist = float(flat[0])
                person_scores.append((int(target_owner), score_mean, best_dist, score_k))

                pair_k = max(1, min(int(knn_top_n), int(flat.size)))
                for value in flat[:pair_k]:
                    global_pairs.append((float(value), int(target_owner)))

            if not person_scores:
                continue
            person_scores.sort(key=lambda item: item[1])
            best_owner, best_score, best_dist, best_score_k = person_scores[0]
            second_score = float(person_scores[1][1]) if len(person_scores) >= 2 else float("inf")
            margin = second_score - best_score if second_score != float("inf") else float("inf")

            global_pairs.sort(key=lambda item: item[0])
            top_global_pairs = global_pairs[: max(1, int(knn_top_n))]
            votes = Counter(label for _, label in top_global_pairs)
            best_votes = int(votes.get(best_owner, 0))

            if best_votes < int(min_votes):
                continue
            if best_score > float(distance_threshold):
                continue
            if margin < float(margin_threshold):
                continue

            owner_by_cluster[cluster_label] = int(best_owner)
            moved_count += 1
            moved_in_round += 1
            events.append(
                {
                    "round": int(round_idx),
                    "cluster_label": int(cluster_label),
                    "cluster_size": len(clusters_by_label[cluster_label].get("members", [])),
                    "from_person_label_before_reindex": int(current_owner),
                    "to_person_label_before_reindex": int(best_owner),
                    "best_topk_mean_distance": float(best_score),
                    "best_top1_distance": float(best_dist),
                    "best_score_k": int(best_score_k),
                    "second_best_topk_mean_distance": None if second_score == float("inf") else float(second_score),
                    "margin": None if margin == float("inf") else float(margin),
                    "best_votes": int(best_votes),
                    "knn_top_n": int(knn_top_n),
                    "min_votes": int(min_votes),
                }
            )

        if moved_in_round <= 0:
            break

    rebuilt_buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for cluster_label, owner in owner_by_cluster.items():
        rebuilt_buckets[int(owner)].append(clusters_by_label[cluster_label])
    return _materialize_persons_from_cluster_buckets(rebuilt_buckets), events, moved_count

def _cluster_with_hdbscan(
    embeddings: list[list[float]] | np.ndarray,
    min_cluster_size: int,
    min_samples: int,
) -> tuple[list[int], list[float]]:
    import hdbscan

    if isinstance(embeddings, np.ndarray):
        vectors = np.asarray(embeddings, dtype=np.float32)
    else:
        if not embeddings:
            return [], []
        vectors = np.asarray(embeddings, dtype=np.float32)

    if vectors.ndim != 2 or int(vectors.shape[0]) <= 0:
        return [], []
    if int(vectors.shape[0]) < max(2, min_cluster_size, min_samples):
        return [-1 for _ in range(int(vectors.shape[0]))], [0.0 for _ in range(int(vectors.shape[0]))]

    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=max(2, int(min_cluster_size)),
        min_samples=max(1, int(min_samples)),
        metric="euclidean",
        cluster_selection_method="eom",
        prediction_data=False,
    )
    labels = clusterer.fit_predict(vectors).tolist()
    probabilities = clusterer.probabilities_.astype(float).tolist()
    return labels, probabilities
