from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Mapping, Sequence
import warnings

import numpy as np
import numpy.typing as npt

try:
    import hnswlib
except ImportError:  # pragma: no cover - 运行环境不保证总有 hnswlib
    hnswlib = None


Embedding = npt.NDArray[np.float32]


class AnnIndexStore:
    def __init__(self, artifact_path: Path) -> None:
        self.artifact_path = Path(artifact_path)
        self._person_ids: npt.NDArray[np.int64] = np.empty((0,), dtype=np.int64)
        self._vectors: Embedding = np.empty((0, 0), dtype=np.float32)
        self._hnsw_index: hnswlib.Index | None = None  # type: ignore[valid-type]
        self._backend: str = "bruteforce"
        self.load()

    @property
    def size(self) -> int:
        return int(self._person_ids.size)

    @property
    def backend(self) -> str:
        return self._backend

    def rebuild_from_prototypes(self, prototypes: Sequence[Mapping[str, Any]]) -> int:
        person_ids: list[int] = []
        vectors: list[Embedding] = []
        expected_dim: int | None = None

        for row in prototypes:
            person_id = int(row["person_id"])
            vector = self._vector_from_row(row)
            if vector is None:
                continue
            if expected_dim is None:
                expected_dim = int(vector.size)
            if int(vector.size) != expected_dim:
                continue
            person_ids.append(person_id)
            vectors.append(vector.astype(np.float32, copy=False))

        if not vectors:
            self._person_ids = np.empty((0,), dtype=np.int64)
            self._vectors = np.empty((0, 0), dtype=np.float32)
            self._hnsw_index = None
            self._backend = "bruteforce"
            self._save()
            return 0

        self._person_ids = np.asarray(person_ids, dtype=np.int64)
        self._vectors = np.vstack(vectors).astype(np.float32, copy=False)
        self._build_index()
        self._save()
        return self.size

    def upsert_person_prototype(self, prototype: Mapping[str, Any]) -> int:
        person_id = int(prototype["person_id"])
        vector = self._vector_from_row(prototype)
        if vector is None:
            return self.remove_person(person_id)
        return self.upsert_person_vector(person_id, vector)

    def upsert_person_vector(self, person_id: int, vector: Sequence[float] | Embedding) -> int:
        normalized = np.asarray(vector, dtype=np.float32).reshape(-1)
        if normalized.size == 0:
            raise ValueError("person prototype 不能为空向量")

        if self.size > 0 and self._vectors.shape[1] != int(normalized.size):
            raise ValueError(
                f"embedding 维度不匹配: query={int(normalized.size)} index={int(self._vectors.shape[1])}"
            )

        mask = self._person_ids != int(person_id)
        kept_person_ids = self._person_ids[mask]
        kept_vectors = self._vectors[mask] if self.size > 0 else np.empty((0, normalized.size), dtype=np.float32)

        next_person_ids = np.concatenate((kept_person_ids, np.asarray([int(person_id)], dtype=np.int64)))
        next_vectors = (
            np.vstack((kept_vectors, normalized.reshape(1, -1)))
            if kept_vectors.size > 0
            else normalized.reshape(1, -1).astype(np.float32, copy=False)
        )
        order = np.argsort(next_person_ids, kind="stable")
        self._person_ids = next_person_ids[order]
        self._vectors = next_vectors[order]
        self._build_index()
        self._save()
        return self.size

    def remove_person(self, person_id: int) -> int:
        if self.size == 0:
            return 0
        mask = self._person_ids != int(person_id)
        if bool(np.all(mask)):
            return self.size
        self._person_ids = self._person_ids[mask]
        if self._person_ids.size == 0:
            self._vectors = np.empty((0, 0), dtype=np.float32)
        else:
            self._vectors = self._vectors[mask]
        self._build_index()
        self._save()
        return self.size

    def search(self, observation_embedding: Sequence[float] | Embedding, top_k: int) -> list[tuple[int, float]]:
        if top_k <= 0 or self.size == 0:
            return []
        query = np.asarray(observation_embedding, dtype=np.float32).reshape(-1)
        if query.size == 0:
            return []
        if self._vectors.shape[1] != int(query.size):
            raise ValueError(
                f"embedding 维度不匹配: query={int(query.size)} index={int(self._vectors.shape[1])}"
            )

        limit = min(int(top_k), self.size)
        if self._backend == "hnsw" and self._hnsw_index is not None:
            self._hnsw_index.set_ef(max(50, limit))
            labels, distances = self._hnsw_index.knn_query(query, k=limit)
            label_list = labels[0].tolist()
            distance_list = distances[0].tolist()
            return [
                (int(self._person_ids[int(label)]), float(np.sqrt(max(0.0, float(distance)))))
                for label, distance in zip(label_list, distance_list)
            ]

        delta = self._vectors - query.reshape(1, -1)
        distances = np.linalg.norm(delta, axis=1)
        order = np.argsort(distances)[:limit]
        return [(int(self._person_ids[int(idx)]), float(distances[int(idx)])) for idx in order]

    def load(self) -> None:
        if not self.artifact_path.exists():
            return
        try:
            with np.load(self.artifact_path, allow_pickle=False) as payload:
                person_ids = payload["person_ids"].astype(np.int64, copy=False)
                vectors = payload["vectors"].astype(np.float32, copy=False)
        except Exception as exc:
            warnings.warn(f"加载 ANN 索引失败，回退为空索引: {exc}", RuntimeWarning)
            return

        if vectors.ndim != 2 or person_ids.ndim != 1 or vectors.shape[0] != person_ids.size:
            warnings.warn("ANN 索引文件结构无效，回退为空索引", RuntimeWarning)
            return

        self._person_ids = person_ids
        self._vectors = vectors
        self._build_index()

    def verify_prepared_artifact(self, *, artifact_path: Path, expected_checksum: str) -> None:
        path = Path(artifact_path)
        checksum = str(expected_checksum).strip()
        if not path.exists():
            raise ValueError(f"prepared ann artifact 不存在: {path}")
        if not checksum:
            raise ValueError("prepared ann artifact checksum 为空")
        actual = self.calculate_artifact_checksum(path)
        if actual != checksum:
            raise ValueError(f"prepared ann artifact checksum 不匹配: expected={checksum}, actual={actual}")

    def activate_verified_artifact(self, *, artifact_path: Path, expected_checksum: str, source_run_id: int) -> None:
        source = Path(artifact_path)
        self.verify_prepared_artifact(
            artifact_path=source,
            expected_checksum=str(expected_checksum),
        )
        destination = self.artifact_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_equals_destination = source.resolve() == destination.resolve()
        swap_tmp = destination.with_name(f"{destination.name}.swap.tmp")
        backup_tmp = destination.with_name(f"{destination.name}.backup.tmp")
        had_existing_destination = destination.exists()
        try:
            if not source_equals_destination:
                if had_existing_destination:
                    shutil.copy2(destination, backup_tmp)
                shutil.copy2(source, swap_tmp)
                os.replace(swap_tmp, destination)

            meta = {
                "source_run_id": int(source_run_id),
                "artifact_checksum": str(expected_checksum),
                "activated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            self._meta_path().write_text(
                json.dumps(meta, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            self.load()
        except Exception:
            if not source_equals_destination:
                if had_existing_destination and backup_tmp.exists():
                    os.replace(backup_tmp, destination)
                elif (not had_existing_destination) and destination.exists():
                    destination.unlink(missing_ok=True)
            raise
        finally:
            if swap_tmp.exists():
                swap_tmp.unlink()
            if backup_tmp.exists():
                backup_tmp.unlink()

    def get_live_owner_run_id(self) -> int | None:
        meta_path = self._meta_path()
        if not meta_path.exists():
            return None
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        run_id = payload.get("source_run_id")
        if run_id is None:
            return None
        return int(run_id)

    def calculate_artifact_checksum(self, artifact_path: Path | None = None) -> str:
        target = self.artifact_path if artifact_path is None else Path(artifact_path)
        if not target.exists():
            return ""
        return hashlib.sha256(target.read_bytes()).hexdigest()

    def _build_index(self) -> None:
        self._hnsw_index = None
        self._backend = "bruteforce"
        if self.size == 0:
            return
        if hnswlib is None:
            return
        try:
            index = hnswlib.Index(space="l2", dim=int(self._vectors.shape[1]))
            index.init_index(max_elements=self.size, ef_construction=100, M=16)
            labels = np.arange(self.size, dtype=np.int64)
            index.add_items(self._vectors, labels)
            index.set_ef(max(50, min(200, self.size)))
            self._hnsw_index = index
            self._backend = "hnsw"
        except Exception:
            self._hnsw_index = None
            self._backend = "bruteforce"

    def _save(self) -> None:
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.artifact_path.with_name(f"{self.artifact_path.name}.tmp")
        try:
            with tmp_path.open("wb") as fp:
                np.savez_compressed(
                    fp,
                    person_ids=self._person_ids,
                    vectors=self._vectors,
                )
            os.replace(tmp_path, self.artifact_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def _meta_path(self) -> Path:
        return self.artifact_path.with_name(f"{self.artifact_path.name}.meta.json")

    @staticmethod
    def _vector_from_row(row: Mapping[str, Any]) -> Embedding | None:
        vector_blob = row.get("vector_blob")
        if not isinstance(vector_blob, (bytes, bytearray, memoryview)):
            return None
        vector = np.frombuffer(vector_blob, dtype=np.float32).copy()
        if vector.ndim != 1 or vector.size == 0:
            return None
        return vector.astype(np.float32, copy=False)
